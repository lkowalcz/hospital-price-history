"""Restore the most recent original-file queue checkpoint on a fresh CI runner.

Only artifacts from this workflow on main are eligible. A failed restore must
stop collection, rather than replace a recoverable queue with an empty one.
"""

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import preserve_source as originals

ARTIFACT_PREFIX = "original-queue-"


def extract(archive, destination):
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("Unsafe archive recovery path")
            if entry.is_dir():
                continue
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    checkpoint = json.loads((destination / "checkpoint.json").read_text())
    if checkpoint.get("schema_version") != 1:
        raise ValueError("Unknown original-queue checkpoint schema")
    for path in destination.glob("*/*/record.json"):
        record = originals.validate(json.loads(path.read_text()))
        if path.parent.parts[-2:] != (record["slug"], record["download_sha256"]):
            raise ValueError("Original receipt does not match queue path")
        if record["status"] == "pending" and not any(
                (path.parent / name).is_file() for name in ("original", "original.zst")):
            raise ValueError("Checkpoint is missing a pending original")
    for path in destination.glob("*/*/original*"):
        if not (path.parent / "record.json").exists():
            raise ValueError("Checkpoint contains an original without a receipt; recover it explicitly")


def latest_checkpoint(repo):
    """Unique names also work for reruns without deleting an earlier checkpoint."""
    page = 1
    while True:
        response = json.loads(subprocess.check_output([
            "gh", "api", f"repos/{repo}/actions/artifacts?per_page=100&page={page}"]))
        artifacts = response["artifacts"]
        for artifact in sorted(artifacts, key=lambda a: a["id"], reverse=True):
            run = artifact.get("workflow_run") or {}
            if (not artifact["name"].startswith(ARTIFACT_PREFIX)
                    or run.get("head_branch") != "main" or run.get("head_repository_id") is None
                    or run.get("head_repository_id") != run.get("repository_id")):
                continue
            details = json.loads(subprocess.check_output([
                "gh", "api", f"repos/{repo}/actions/runs/{run['id']}"]))
            if details.get("path") != ".github/workflows/scrape.yml":
                continue
            if artifact["expired"]:
                raise ValueError("Latest original-queue checkpoint expired; investigate before collecting")
            return artifact
        if page * 100 >= response["total_count"] or not artifacts:
            return None
        page += 1


def restore(root=originals.ROOT):
    destination = originals.spool(root)
    if destination.exists():
        raise ValueError("Restore requires a fresh spool directory")
    repo = os.environ["GITHUB_REPOSITORY"]
    latest = latest_checkpoint(repo)
    with tempfile.TemporaryDirectory(dir=Path(root)) as temporary:
        stage = Path(temporary) / "spool"
        stage.mkdir()
        if latest:
            archive = Path(temporary) / "queue.zip"
            with archive.open("wb") as handle:
                subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{latest['id']}/zip"],
                               stdout=handle, check=True)
            extract(archive, stage)
            print(f"Restored original queue from artifact {latest['id']}")
        for path in (Path(root) / "data").glob("*/originals/*.json"):
            record = originals.validate(json.loads(path.read_text()))
            if record["status"] == "pending" and record.get("collector") == "github-actions":
                recovered = stage / record["slug"] / record["download_sha256"] / "record.json"
                if not recovered.is_file():
                    raise ValueError("Pending original receipt is missing from the recovery checkpoint")
        originals.write_json(stage / "checkpoint.json", {"schema_version": 1,
                              "restored_at": originals.now()})
        os.replace(stage, destination)


if __name__ == "__main__":
    restore()
