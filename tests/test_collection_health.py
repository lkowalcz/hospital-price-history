import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collection_health as health


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hospitals = [{"slug": "a"}, {"slug": "b"}]

    def start(self, at="2026-10-17T00:00:00Z", host="github-actions", slugs=None):
        with patch.object(health, "now", return_value=at):
            return health.RunLog.create(self.root, host, slugs or ["a"])

    def attempt(self, log, slug="a", at="2026-10-17T01:00:00Z", result=None, finish_at=None):
        with patch.object(health, "now", return_value=at):
            aid = log.begin(slug)
        if result is not None:
            with patch.object(health, "now", return_value=finish_at or at):
                log.finish_attempt(slug, aid, result)
        return aid

    def success(self, at="2026-10-17T01:00:00Z", method="headers", **extra):
        return dict(outcome="unchanged", method=method, verified_at=at,
                    source_sha256="a" * 64, **extra)

    def report(self, at="2026-10-17T02:00:00Z"):
        source = self.root / ".collection-spool" / "observations"
        if source.exists():
            shutil.copytree(source, self.root / "observations", dirs_exist_ok=True)
        return health.build_health(self.root, self.hospitals, at)

    def test_unknown_is_never_backfilled_from_metadata(self):
        path = self.root / "data" / "a"
        path.mkdir(parents=True)
        (path / "meta.json").write_text('{"last_changed":"2026-10-17T01:00:00Z"}')
        entry = self.report()["hospitals"][0]
        self.assertIsNone(entry["last_successful_check_at"])
        self.assertFalse(entry["check_overdue"])

    def test_failure_and_deferred_never_refresh_success(self):
        log = self.start()
        self.attempt(log, result=self.success())
        self.attempt(log, at="2026-10-17T01:10:00Z", result={"outcome": "failed", "error": "timeout"})
        self.attempt(log, at="2026-10-17T01:20:00Z", result={"outcome": "deferred", "method": "discovery_only"})
        entry = self.report()["hospitals"][0]
        self.assertEqual(entry["state"], "deferred")
        self.assertEqual(entry["last_successful_check_at"], "2026-10-17T01:00:00Z")
        self.assertIsNone(entry["last_full_download_at"])

    def test_download_and_verification_use_evidence_time_not_finish_time(self):
        slow = self.start()
        self.attempt(slow, at="2026-10-17T00:10:00Z", finish_at="2026-10-17T01:50:00Z",
                     result=self.success("2026-10-17T00:20:00Z", "full_download",
                                         downloaded_at="2026-10-17T00:20:00Z"))
        newer = self.start(host="manual")
        self.attempt(newer, result=self.success())
        entry = self.report()["hospitals"][0]
        self.assertEqual(entry["last_successful_check_at"], "2026-10-17T01:00:00Z")
        self.assertEqual(entry["last_full_download_at"], "2026-10-17T00:20:00Z")
        self.assertEqual(entry["latest_attempt"]["host"], "manual")

    def test_daily_attempts_cannot_hide_overdue_weekly_verification(self):
        log = self.start()
        self.attempt(log, result=self.success(verification_interval_hours=168))
        recent = self.start("2026-10-26T01:30:00Z")
        self.attempt(recent, at="2026-10-26T01:30:00Z", result={
            "outcome": "deferred", "verification_interval_hours": 168})
        entry = self.report("2026-10-26T02:00:00Z")["hospitals"][0]
        self.assertFalse(entry["check_overdue"])
        self.assertTrue(entry["verification_overdue"])
        self.assertEqual(entry["verification_interval_hours"], 168)

    def test_manual_success_does_not_hide_dead_scheduled_collector(self):
        self.start()
        log = self.start("2026-10-21T00:00:00Z", host="manual")
        self.attempt(log, at="2026-10-21T01:00:00Z", result=self.success("2026-10-21T01:00:00Z"))
        entry = self.report("2026-10-21T02:00:00Z")["hospitals"][0]
        self.assertTrue(entry["check_overdue"])
        self.assertFalse(entry["verification_overdue"])

    def test_skipped_hospital_stays_unknown_then_overdue_after_rollout_grace(self):
        self.start()
        entry = self.report()["hospitals"][1]
        self.assertIsNone(entry["last_attempt_at"])
        self.assertFalse(entry["check_overdue"])
        entry = self.report("2026-10-21T02:00:00Z")["hospitals"][1]
        self.assertTrue(entry["check_overdue"])

    def test_interrupted_run_exposes_planned_and_started_gaps_then_recovers(self):
        log = self.start(slugs=["a", "b"])
        self.attempt(log)
        early = self.report()["hospitals"]
        self.assertEqual(early[0]["state"], "in_progress")
        later = self.report("2026-10-17T08:00:00Z")
        self.assertEqual(later["runs"][0]["missing_outcomes"], ["a", "b"])
        self.assertTrue(all("collection_interrupted" in e["issues"] for e in later["hospitals"]))
        recovery = self.start("2026-10-17T09:00:00Z")
        self.attempt(recovery, at="2026-10-17T09:00:00Z", result=self.success("2026-10-17T09:00:00Z"))
        recovered = self.report("2026-10-17T10:00:00Z")["hospitals"]
        self.assertNotIn("collection_interrupted", recovered[0]["issues"])
        self.assertIn("collection_interrupted", recovered[1]["issues"])

    def test_cannot_complete_a_run_with_missing_outcomes(self):
        log = self.start()
        with self.assertRaisesRegex(ValueError, "missing hospital outcomes"):
            log.finish()

    def test_partial_record_can_be_finalized_as_interrupted(self):
        log = self.start()
        with log.path.open("ab") as handle:
            handle.write(b'{"event":')
        with patch.object(health, "now", return_value="2026-10-17T01:00:00Z"):
            log.finish("interrupted")
        self.assertEqual(health.events(log.path.read_bytes())[-1]["outcome"], "interrupted")

    def test_reject_fake_success_or_failure_with_verified_time(self):
        for result in ({"outcome": "unchanged", "method": "headers"},
                       {"outcome": "failed", "verified_at": "2026-10-17T01:00:00Z"}):
            log = self.start()
            self.attempt(log, result=result)
            with self.assertRaises(ValueError):
                health.events(log.path.read_bytes())

    def test_clock_skew_cannot_make_source_fresh(self):
        self.start("2027-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "future"):
            self.report()

    def test_policy_matches_actual_pi_assignment(self):
        import local_refetch
        policy = health.policy_for(health.ROOT)
        self.assertEqual(set(policy["overrides"]), set(local_refetch.LOCAL_ONLY))
        self.assertEqual(set(policy["overrides"].values()), {"pi"})

    def test_archive_failure_is_separate_and_errors_redact_queries(self):
        self.assertEqual(health.archive_result({}), "not_recorded")
        self.assertEqual(health.archive_result({"sha256": "x", "cold_storage_attempt": {"sha256": "x"}}), "failed")
        self.assertNotIn("secret", health.error_text(ValueError("HTTP error https://x/a?sig=secret")))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.a, self.b = self.root / "a", self.root / "b"
        self.command(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.command(self.root, "clone", str(self.remote), str(self.a))
        self.command(self.a, "config", "user.name", "Test")
        self.command(self.a, "config", "user.email", "test@example.org")
        (self.a / "data.txt").write_text("committed\n")
        self.command(self.a, "add", "data.txt")
        self.command(self.a, "commit", "-m", "initial")
        self.command(self.a, "push", "origin", "main")
        self.command(self.root, "clone", str(self.remote), str(self.b))

    def command(self, root, *args):
        return health.git(root, *args).decode().strip()

    def remote_files(self):
        return self.command(self.remote, "ls-tree", "-r", "--name-only", "main").splitlines()

    def test_publish_preserves_dirty_worktree_index_head_and_other_host(self):
        original = self.command(self.a, "rev-parse", "HEAD")
        (self.a / "data.txt").write_text("staged edit\n")
        self.command(self.a, "add", "data.txt")
        (self.a / "data.txt").write_text("unstaged edit\n")
        first = health.RunLog.create(self.a, "github-actions", ["a"])
        second = health.RunLog.create(self.b, "pi", ["b"])
        health.publish(self.a)
        health.publish(self.b)
        self.assertEqual(self.command(self.a, "rev-parse", "HEAD"), original)
        self.assertEqual(self.command(self.a, "show", ":data.txt"), "staged edit")
        self.assertEqual((self.a / "data.txt").read_text(), "unstaged edit\n")
        self.assertEqual(self.command(self.remote, "show", "main:data.txt"), "committed")
        self.assertEqual(len([p for p in self.remote_files() if p.endswith(".jsonl")]), 2)
        first.finish("interrupted")
        health.publish(self.a)
        self.assertIn(second.path.name, " ".join(self.remote_files()))

    def test_stale_publisher_cannot_shorten_remote_log(self):
        log = health.RunLog.create(self.a, "pi", ["a"])
        initial = log.path.read_bytes()
        log.finish("interrupted")
        health.publish(self.a)
        before = self.command(self.remote, "rev-parse", "main")
        log.path.write_bytes(initial)
        health.publish(self.a)
        self.assertEqual(self.command(self.remote, "rev-parse", "main"), before)

    def test_divergent_log_is_rejected(self):
        log = health.RunLog.create(self.a, "pi", ["a"])
        health.publish(self.a)
        log.path.write_text(log.path.read_text().replace('"planned_slugs": ["a"]', '"planned_slugs": ["b"]'))
        with self.assertRaisesRegex(ValueError, "divergent"):
            health.publish(self.a)

    def test_concurrent_push_is_rebased_without_losing_either_host(self):
        health.RunLog.create(self.a, "github-actions", ["a"])
        health.RunLog.create(self.b, "pi", ["b"])
        original_git, raced = health.git, []
        def racing_git(root, *args, **kwargs):
            if Path(root).resolve() == self.a.resolve() and args[0] == "push" and not raced:
                raced.append(True)
                health.publish(self.b)
            return original_git(root, *args, **kwargs)
        with patch.object(health, "git", side_effect=racing_git):
            health.publish(self.a)
        self.assertEqual(len([p for p in self.remote_files() if p.endswith(".jsonl")]), 2)

    def test_partial_last_record_is_not_published(self):
        log = health.RunLog.create(self.a, "pi", ["a"])
        valid = log.path.read_bytes()
        with log.path.open("ab") as handle:
            handle.write(b'{"event":')
        health.publish(self.a)
        path = log.path.relative_to((self.a / ".collection-spool").resolve()).as_posix()
        self.assertEqual(health.git(self.remote, "show", "main:" + path), valid)


if __name__ == "__main__":
    unittest.main()
