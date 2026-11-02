"""Preserve downloaded bytes independently of parsing and snapshot storage.

The spool must live on persistent disk. CI checkpoints it as a recovery artifact;
that artifact is temporary recovery storage, not the permanent public archive.
Only a confirmed Internet Archive upload permits deleting a queued original.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEADERS = ("Content-Type", "Content-Encoding", "Content-Length", "Content-Disposition",
           "ETag", "Last-Modified")


def enabled():
    return os.environ.get("PRESERVE_ORIGINALS") == "1"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as handle:
        tmp = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)


def spool(root=ROOT):
    return Path(os.environ.get("ORIGINAL_SPOOL_DIR", Path(root) / ".archive-spool"))


def validate(record):
    if (record.get("schema_version") != 1
            or not re.fullmatch(r"[a-z0-9-]+", record.get("slug", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("download_sha256", ""))
            or record.get("status") not in {"pending", "archived"}):
        raise ValueError("Invalid original-download receipt")
    for field in ("snapshot_sha256", "payload_sha256"):
        if field in record and not re.fullmatch(r"[0-9a-f]{64}", record[field]):
            raise ValueError(f"Invalid {field} in original-download receipt")
    if record["status"] == "archived":
        archive = record.get("archive") or {}
        if (not archive.get("url", "").startswith("https://archive.org/details/")
                or not archive.get("download_url", "").startswith("https://archive.org/download/")
                or not re.fullmatch(r"[0-9a-f]{64}", archive.get("compressed_sha256", ""))):
            raise ValueError("Archived receipt lacks archive identity/checksum")
    return record


def receipt_path(data, record):
    validate(record)
    return Path(data) / record["slug"] / "originals" / (record["download_sha256"] + ".json")


def save(record, queue, data):
    validate(record)
    # Queue first: recovery remains possible if a data commit/write fails.
    write_json(queue / "record.json", record)
    write_json(receipt_path(data, record), record)


def upload(queue, record):
    """Upload a checked compressed copy; injectable in offline tests."""
    raw, compressed = queue / "original", queue / "original.zst"
    if not compressed.exists():
        if digest(raw) != record["download_sha256"]:
            raise ValueError("Queued original checksum mismatch")
        part = queue / "original.zst.part"
        with part.open("wb") as handle:
            subprocess.run(["zstd", "-q", "-T0", "-6", "-c", str(raw)],
                           stdout=handle, check=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, compressed)
    # Detect corruption of a restored artifact before acknowledging preservation.
    h = hashlib.sha256()
    with subprocess.Popen(["zstd", "-q", "-d", "-c", str(compressed)],
                          stdout=subprocess.PIPE) as process:
        for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
            h.update(block)
        if process.wait() or h.hexdigest() != record["download_sha256"]:
            raise ValueError("Compressed original checksum mismatch")
    # Compressed bytes now suffice for retries, reducing the pending disk footprint.
    raw.unlink(missing_ok=True)
    compressed_sha = digest(compressed)
    item = "hospital-price-original-" + record["download_sha256"]
    # Different zstd versions may encode identical originals differently. Name
    # the compressed object by its own hash so another host cannot invalidate
    # an older receipt's compressed checksum by overwriting the same filename.
    filename = f"original-{compressed_sha}.zst"
    remote_file = queue / filename
    remote_file.unlink(missing_ok=True)
    os.link(compressed, remote_file)
    try:
        subprocess.run([os.environ.get("IA_BIN", "ia"), "upload", item, str(remote_file),
                        "--no-derive", "--checksum", "--retries", "3", "--sleep", "20",
                        "--metadata", "mediatype:data",
                        "--metadata", "title:Hospital price file original " + record["download_sha256"],
                        "--metadata", "description:Original downloaded bytes; SHA-256 " + record["download_sha256"]],
                       check=True, stdout=subprocess.DEVNULL)
    finally:
        remote_file.unlink(missing_ok=True)
    return {"url": f"https://archive.org/details/{item}",
            "download_url": f"https://archive.org/download/{item}/{filename}",
            "compressed_sha256": compressed_sha, "compressed_bytes": compressed.stat().st_size,
            "compression": "zstd", "archived_at": now()}


def attempt(record, queue, data, uploader=None):
    if record["status"] == "archived":
        save(record, queue, data)
        (queue / "original").unlink(missing_ok=True)
        (queue / "original.zst").unlink(missing_ok=True)
        return record
    record["last_attempt_at"] = now()
    try:
        archive = (uploader or upload)(queue, record)
    except Exception as exc:
        from collection_health import error_text
        record["last_error"] = error_text(exc)
        save(record, queue, data)
        print(f"{record['slug']}: original retained for retry: {record['last_error']}", flush=True)
        return record
    record.update(status="archived", archive=archive)
    record.pop("last_error", None)
    save(record, queue, data)
    # Never remove the last local copy until both receipts are durably written.
    (queue / "original").unlink(missing_ok=True)
    (queue / "original.zst").unlink(missing_ok=True)
    return record


def capture(path, slug, source_url, filename, headers=None, *, data=None, root=ROOT,
            uploader=None, upload_now=True, artifact_kind="download"):
    """Retain a completed download BEFORE unwrapping, normalization or parsing."""
    data = Path(data) if data is not None else Path(root) / "data"
    sha = digest(path)
    with Path(path).open("rb") as handle:
        magic = handle.read(4)
    record = {"schema_version": 1, "slug": slug, "download_sha256": sha,
              "download_bytes": Path(path).stat().st_size, "source_url": source_url,
              "source_filename": filename, "captured_at": now(), "status": "pending",
              "artifact_kind": artifact_kind,
              "collector": os.environ.get("COLLECTION_HOST", "manual"),
              "container": "zip" if magic.startswith(b"PK\x03\x04") else
                           "gzip" if magic.startswith(b"\x1f\x8b") else "plain",
              "response_headers": {k: headers.get(k) for k in HEADERS if headers and headers.get(k)}}
    rp = receipt_path(data, record)
    queue = spool(root) / slug / sha
    previous = queue / "record.json"
    if previous.exists() or rp.exists():
        record = validate(json.loads((previous if previous.exists() else rp).read_text()))
    queue.mkdir(parents=True, exist_ok=True)
    if record["status"] != "archived" and not (queue / "original.zst").exists():
        raw = queue / "original"
        if not raw.exists():
            # A hard link survives scratch cleanup without duplicating large files.
            part = queue / "original.part"
            part.unlink(missing_ok=True)
            try:
                if artifact_kind == "local_input":
                    # Operator-owned files can be edited in place after ingestion.
                    shutil.copyfile(path, part)
                else:
                    os.link(path, part)
            except OSError:
                shutil.copyfile(path, part)
            os.replace(part, raw)
    save(record, queue, data)
    return attempt(record, queue, data, uploader) if upload_now else record


def bind(record, snapshot_sha256, payload_sha256, *, data=None, root=ROOT):
    """Associate a receipt with a normalized state without conflating its hashes."""
    data = Path(data) if data is not None else Path(root) / "data"
    record.update(snapshot_sha256=snapshot_sha256, payload_sha256=payload_sha256)
    save(record, spool(root) / record["slug"] / record["download_sha256"], data)
    return record


def apply_to_meta(meta, record):
    meta["original_download"] = {k: record[k] for k in
                                 ("download_sha256", "status", "captured_at", "artifact_kind")}
    if record.get("archive"):
        meta["original_download"]["archive"] = record["archive"]
    # Preserve the existing archive API only when the archived bytes ARE the MRF,
    # not its ZIP/gzip wrapper. Existing historical IDs and source links stay valid.
    if (record["status"] == "archived" and record.get("snapshot_sha256") == meta.get("sha256")
            and record.get("payload_sha256") == record["download_sha256"]):
        archive = record["archive"]
        meta["cold_storage"] = {**archive, "sha256": meta["sha256"],
                                "file_sha256": record["download_sha256"],
                                "date": archive["archived_at"][:10]}
        meta.pop("cold_storage_attempt", None)


def retry(*, data=None, root=ROOT, uploader=None):
    data = Path(data) if data is not None else Path(root) / "data"
    for path in sorted(spool(root).glob("*/*/record.json")):
        record = validate(json.loads(path.read_text()))
        public = receipt_path(data, record)
        if public.exists():
            published = validate(json.loads(public.read_text()))
            if published["status"] == "archived":
                record = {**record, **published}  # retain bindings absent from older published receipts
        record = attempt(record, path.parent, data, uploader)
        mp = data / record["slug"] / "meta.json"
        if mp.exists():
            meta = json.loads(mp.read_text())
            if (meta.get("original_download") or {}).get("download_sha256") == record["download_sha256"]:
                apply_to_meta(meta, record)
                write_json(mp, meta)
    return pending(root)


def pending(root=ROOT):
    records = [validate(json.loads(p.read_text())) for p in sorted(spool(root).glob("*/*/record.json"))]
    return [record for record in records if record["status"] == "pending"]


def discovery(hospital, raw, headers, *, data):
    """Keep each distinct discovery document, including a disappeared listing."""
    sha = hashlib.sha256(raw).hexdigest()
    target = Path(data) / hospital["slug"] / "discovery"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{sha}.bin").write_bytes(raw)
    receipt = target / f"{sha}.json"
    if not receipt.exists():
        write_json(receipt, {"schema_version": 1, "sha256": sha, "bytes": len(raw),
                            "source_url": hospital["hpt_txt"], "first_observed_at": now(),
                            "response_headers": {k: headers.get(k) for k in HEADERS if headers and headers.get(k)}})
    return sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["retry", "check"])
    args = parser.parse_args()
    records = retry() if args.command == "retry" else pending()
    if records:
        print(f"{len(records)} original(s) await archive upload; retained in {spool()}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
