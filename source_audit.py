"""Validate and apply reviewed historical source-link repairs.

Site builds use committed evidence and never fetch large originals. Operators
can repeat the raw-tree checks and full archived-file hashes with this CLI.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIT_PATH = "source_audit.json"
RESET_FIELDS = {"sha256", "source_last_modified", "source_etag", "transfer_fingerprint", "raw_commit"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def pinned(repo, revision, path):
    return f"{repo.replace('https://github.com/', 'https://raw.githubusercontent.com/', 1)}/{revision}/{path}"


def require(condition, message):
    if not condition:
        raise ValueError("Invalid source audit: " + message)


def digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value)


def load_audit(root, revision):
    if not git(root, "ls-tree", "--name-only", revision, "--", AUDIT_PATH):
        return {"schema_version": 1, "records": []}
    audit = json.loads(git(root, "show", f"{revision}:{AUDIT_PATH}"))
    require(audit.get("schema_version") == 1, "unsupported schema")
    return audit


def apply_audit(root, revision, histories, sources, repo, raw_repo, audit=None):
    audit = load_audit(root, revision) if audit is None else audit
    seen = set()
    for record in audit["records"]:
        slug, commit = record["slug"], record["revision"]
        require(re.fullmatch(r"[a-z0-9-]+", slug) and digest(commit, 40), "invalid snapshot identity")
        key = f"{slug}:{commit}"
        require(key not in seen, "duplicate snapshot " + key)
        seen.add(key)
        if slug not in histories:
            continue  # a caller can request a subset of hospitals
        snapshot = next((s for s in histories[slug] if s["snapshot_id"] == key), None)
        require(snapshot is not None, "reviewed snapshot disappeared: " + key)
        path = f"data/{slug}/meta.json"
        require(git(root, "rev-parse", f"{commit}:{path}") == record["metadata_blob"], "metadata blob mismatch")
        meta = json.loads(git(root, "show", f"{commit}:{path}"))
        require(meta.get("sha256") == record["recorded_source_sha256"] == snapshot["source_sha256"],
                "source identity mismatch")
        method = record["method"]
        evidence = record["evidence"]
        source = None
        if method == "metadata_hash_reset":
            require(record["status"] == "linked" and meta.get("sha256") is None, "not a missing-hash state")
            parent = git(root, "rev-parse", commit + "^")
            require(parent == evidence["prior_revision"], "reset evidence is not the immediate parent")
            before = json.loads(git(root, "show", f"{parent}:{path}"))
            sha = evidence["source_sha256"]
            require(digest(sha) and before.get("sha256") == sha, "prior identity mismatch")
            require(git(root, "rev-parse", f"{parent}:{path}") == evidence["prior_metadata_blob"],
                    "prior metadata blob mismatch")
            changed = {k for k in before.keys() | meta.keys() if before.get(k) != meta.get(k)}
            require(changed <= RESET_FIELDS, "source details changed alongside checksum removal")
            require(sorted(changed) == evidence["changed_fields"], "reset fields differ from reviewed changes")
            require(all(not meta.get(k) for k in changed - {"raw_commit"}), "validator fields were replaced, not cleared")
            if "raw_commit" in changed:
                require(meta.get("raw_commit") == evidence.get("introduced_raw_revision"), "unreviewed raw pointer")
            source = sources.get((slug, sha))
            require(source is not None, "no independently recorded source for restored identity")
            # Preserve the original null hash and snapshot ID. This is explicit
            # identity evidence for the retained state, not a new source fetch.
            snapshot["source_identity"] = {"sha256": sha, "basis": method,
                                           "metadata_url": pinned(repo, parent, path)}
        elif method == "relocated_shards":
            require(record["status"] == "linked" and meta.get("status") == "sharded"
                    and digest(meta.get("sha256")), "invalid shard source identity")
            require(digest(evidence["raw_revision"], 40) and digest(evidence["header_blob"], 40)
                    and digest(evidence["shards_tree"], 40), "invalid Git evidence")
            require(evidence["original_metadata_blob"] == record["metadata_blob"], "original metadata mismatch")
            source = {"kind": "source_shards", "url": f"{raw_repo}/tree/{evidence['raw_revision']}/data/{slug}",
                      "revision": evidence["raw_revision"], "metadata_url": pinned(repo, commit, path),
                      "header_git_blob": evidence["header_blob"], "shards_git_tree": evidence["shards_tree"]}
        elif method in {"crlf_to_lf", "verified_prefix"}:
            require(record["status"] == "linked", "archive repair is not linked")
            if method == "crlf_to_lf":
                require(evidence["normalized_sha256"] == meta.get("sha256")
                        and evidence["normalized_size_bytes"] == meta.get("size_bytes"), "normalized identity mismatch")
            else:
                require(evidence["prefix_sha256"] == meta.get("sha256")
                        and evidence["prefix_bytes"] == meta.get("size_bytes")
                        and 0 < evidence["prefix_bytes"] < evidence["file_size_bytes"], "prefix identity mismatch")
            require(digest(evidence["file_sha256"]) and digest(evidence["compressed_sha256"]), "invalid archive hash")
            archive_revision = evidence["archive_metadata_revision"]
            require(digest(archive_revision, 40), "invalid archive metadata revision")
            require(git(root, "merge-base", archive_revision, revision) == archive_revision,
                    "archive metadata is outside published mainline history")
            archived = json.loads(git(root, "show", f"{archive_revision}:{path}"))["cold_storage"]
            require(archived["url"] == "https://archive.org/details/" + evidence["item"]
                    and archived["file_sha256"] == evidence["file_sha256"], "archive provenance mismatch")
            require(evidence["download_url"] == archive_download_url(evidence), "unexpected archive download URL")
            source = {"kind": "archived_original", "url": archived["url"],
                      "metadata_url": pinned(repo, archive_revision, path), "file_sha256": evidence["file_sha256"],
                      "snapshot_hash_transform": "crlf_to_lf" if method == "crlf_to_lf" else "prefix",
                      "download_url": evidence["download_url"],
                      "compressed_sha256": evidence["compressed_sha256"]}
            if method == "verified_prefix":
                source["snapshot_prefix_bytes"] = evidence["prefix_bytes"]
                snapshot["source_warning"] = {
                    "code": "truncated_capture",
                    "detail": "This historical capture is an exact byte prefix of the surviving full file. "
                              "Its summary describes an incomplete capture; absent entries do not establish price removals."}
        elif method == "not_located":
            require(record["status"] == "unavailable", "invalid unresolved status")
            require(snapshot["source"] is None, "previously unresolved source now has a link; update its audit")
        else:
            raise ValueError("Invalid source audit: unknown method " + method)
        if source:
            snapshot["source"] = dict(source)
        snapshot["source_audit"] = {"status": record["status"], "method": method,
                                    "explanation": record["explanation"],
                                    "audit_url": pinned(repo, revision, AUDIT_PATH)}
    return audit


def archive_download_url(evidence):
    require(re.fullmatch(r"hospital-price-history-[a-z0-9-]+", evidence["item"]), "invalid archive item")
    require(Path(evidence["file"]).name == evidence["file"], "invalid archive filename")
    return "https://archive.org/download/" + evidence["item"] + "/" + urllib.parse.quote(evidence["file"], safe="")


def hash_stream(handle, block_size=8 * 1024 * 1024, prefix_bytes=None):
    """Hash original and historical CSV normalization with bounded memory.

    Retain a trailing CR across block boundaries so CRLF is normalized exactly
    as bytes.replace(b'\r\n', b'\n') over the complete file would be.
    """
    raw, normalized = hashlib.sha256(), hashlib.sha256()
    prefix = hashlib.sha256()
    if prefix_bytes is not None:
        require(isinstance(prefix_bytes, int) and prefix_bytes >= 0, "invalid prefix length")
    raw_size = normalized_size = 0
    carry = b""
    while block := handle.read(block_size):
        if prefix_bytes is not None and raw_size < prefix_bytes:
            prefix.update(block[:prefix_bytes - raw_size])
        raw.update(block)
        raw_size += len(block)
        data = carry + block
        carry = b"\r" if data.endswith(b"\r") else b""
        if carry:
            data = data[:-1]
        data = data.replace(b"\r\n", b"\n")
        normalized.update(data)
        normalized_size += len(data)
    normalized.update(carry)
    result = {"file_sha256": raw.hexdigest(), "file_size_bytes": raw_size,
              "normalized_sha256": normalized.hexdigest(), "normalized_size_bytes": normalized_size + len(carry)}
    if prefix_bytes is not None:
        require(raw_size >= prefix_bytes, "archive ends before the recorded prefix")
        result.update(prefix_sha256=prefix.hexdigest(), prefix_bytes=prefix_bytes)
    return result


def verify_archive(record, cache):
    evidence = record["evidence"]
    path = cache / (record["slug"] + ".zst")
    if not path.exists():
        partial = path.with_suffix(".partial")
        try:
            with urllib.request.urlopen(archive_download_url(evidence), timeout=60) as response, partial.open("xb") as out:
                received = 0
                while block := response.read(8 * 1024 * 1024):
                    received += len(block)
                    require(received <= evidence["compressed_bytes"], "archive exceeds its recorded size")
                    out.write(block)
            partial.rename(path)
        finally:
            partial.unlink(missing_ok=True)
    with path.open("rb") as handle:
        actual = hash_stream(handle)
    require(actual["file_sha256"] == evidence["compressed_sha256"]
            and actual["file_size_bytes"] == evidence["compressed_bytes"], "compressed archive mismatch")
    with subprocess.Popen(["zstd", "-qdc", "--long=31", str(path)], stdout=subprocess.PIPE) as decoder:
        actual = hash_stream(decoder.stdout, prefix_bytes=evidence.get("prefix_bytes"))
        require(decoder.wait() == 0, "archive decompression failed")
    keys = ["file_sha256", "file_size_bytes"] + (
        ["prefix_sha256", "prefix_bytes"] if record["method"] == "verified_prefix"
        else ["normalized_sha256", "normalized_size_bytes"])
    for key in keys:
        require(actual[key] == evidence[key], f"archive {key} mismatch")


def verify_raw(record, raw_root, main_root):
    evidence, slug = record["evidence"], record["slug"]
    raw_revision = evidence["raw_revision"]
    require(git(raw_root, "merge-base", raw_revision, "origin/main") == raw_revision,
            "recovered shards are not reachable from published raw history")
    for name, key in (("_header.json", "header_blob"), ("shards", "shards_tree")):
        require(git(raw_root, "rev-parse", f"{raw_revision}:data/{slug}/{name}") == evidence[key], "raw tree mismatch")
    # A retained pre-rewrite branch supplied the original trees. This local
    # check is optional on fresh clones; their recorded content IDs remain
    # independently checkable in the published raw repository.
    original = evidence["original_main_revision"]
    exists = subprocess.run(["git", "-C", str(main_root), "cat-file", "-e", original],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists:
        require(git(main_root, "rev-parse", f"{original}:data/{slug}/meta.json") == record["metadata_blob"],
                "original metadata mismatch")
        for name, key in (("_header.json", "header_blob"), ("shards", "shards_tree")):
            require(git(main_root, "rev-parse", f"{original}:data/{slug}/{name}") == evidence[key],
                    "original content tree mismatch")


def audit_totals(audit):
    return {"reviewed_states": len(audit["records"]),
            "linked_states": sum(r["status"] == "linked" for r in audit["records"]),
            "unavailable_states": sum(r["status"] == "unavailable" for r in audit["records"]),
            "methods": dict(sorted(Counter(r["method"] for r in audit["records"]).items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-repo", type=Path, required=True)
    parser.add_argument("--verify-downloads", action="store_true", help="Download and hash the four reviewed originals (requires zstd)")
    parser.add_argument("--cache", type=Path, help="Optional directory for compressed originals")
    args = parser.parse_args()
    import data_catalog
    roster = json.loads((ROOT / "hospitals.json").read_text())
    audit = load_audit(ROOT, "HEAD")
    data_catalog.collect_history(ROOT, "HEAD", [h["slug"] for h in roster],
                                 "https://github.com/lkowalcz/hospital-price-history",
                                 "https://github.com/lkowalcz/hospital-price-history-raw")
    with tempfile.TemporaryDirectory(prefix="source-audit-") as temp:
        cache = args.cache or Path(temp)
        cache.mkdir(parents=True, exist_ok=True)
        for record in audit["records"]:
            if record["method"] == "relocated_shards":
                verify_raw(record, args.raw_repo, ROOT)
            if record["method"] in {"crlf_to_lf", "verified_prefix"} and args.verify_downloads:
                verify_archive(record, cache)
            print(record["slug"] + ": " + record["method"], flush=True)
    print(json.dumps(audit_totals(audit), indent=2))


if __name__ == "__main__":
    main()
