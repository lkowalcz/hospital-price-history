import copy
import json
import subprocess
import unittest
from unittest.mock import patch

import collection_incidents as incidents


class FakeGitHub:
    repo = "example/history"

    def __init__(self):
        self.issues, self.comments, self.labels, self.writes = [], {}, [], []
        self.at = "2026-11-07T06:00:00Z"

    def listing(self, endpoint):
        if endpoint == f"issues?state=open&labels={incidents.LABEL}":
            return [copy.deepcopy(i) for i in self.issues if i["state"] == "open"]
        if endpoint == "labels":
            return self.labels
        number = int(endpoint.split("/")[1])
        return copy.deepcopy(self.comments[number])

    def request(self, endpoint, method="GET", payload=None):
        self.writes.append((endpoint, method, copy.deepcopy(payload)))
        if endpoint == "labels":
            self.labels.append(payload)
            return payload
        if endpoint == "issues":
            number = len(self.issues) + 1
            issue = {**payload, "number": number, "created_at": self.at, "state": "open",
                     "html_url": f"https://github.com/{self.repo}/issues/{number}",
                     "user": {"login": "github-actions[bot]"}}
            self.issues.append(issue)
            self.comments[number] = []
            return issue
        number = int(endpoint.split("/")[1])
        if endpoint.endswith("/comments"):
            comment = {**payload, "created_at": self.at, "user": {"login": "github-actions[bot]"}}
            self.comments[number].append(comment)
            return comment
        self.issues[number - 1].update(payload)
        return self.issues[number - 1]


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.entry = {"slug": "maine", "issues": ["collection_failed", "collection_verification_overdue"],
            "last_attempt_at": "2026-11-06T14:32:08Z", "last_successful_check_at": "2026-10-30T13:26:36Z",
            "latest_attempt": {"path": "observations/2026-11-06/run.jsonl", "result": {
                "outcome": "failed", "error": "received an empty MRF"}}}
        self.report = {"monitoring_since": "2026-10-17T00:00:00Z",
                       "evaluated_at": self.github.at, "hospitals": [self.entry]}

    def run_report(self, at=None, apply=True):
        if at:
            self.github.at = self.report["evaluated_at"] = at
        return incidents.reconcile(self.report, self.github, "a" * 40, apply=apply, assignee="owner")

    def recover(self, at="2026-11-08T05:00:00Z"):
        self.entry["issues"] = []
        self.entry["last_successful_check_at"] = at
        self.entry["latest_attempt"]["result"] = {"outcome": "unchanged", "verified_at": at}

    def test_opens_once_then_reminds_weekly_without_refreshing_data(self):
        original = copy.deepcopy(self.report)
        messages, failed = self.run_report()
        self.assertFalse(failed)
        self.assertIn("Opened", messages[0])
        self.assertEqual(self.github.issues[0]["assignees"], ["owner"])
        self.assertIn("received an empty MRF", self.github.issues[0]["body"])
        self.assertIn("/blob/" + "a" * 40, self.github.issues[0]["body"])
        self.assertEqual(self.report, original)
        initial_writes = len(self.github.writes)
        self.run_report("2026-11-08T06:00:00Z")
        self.run_report("2026-11-14T05:59:59Z")
        self.assertEqual(len(self.github.writes), initial_writes)
        self.run_report("2026-11-14T06:00:00Z")
        self.run_report()  # manual rerun at the same time
        self.assertEqual(len(self.github.comments[1]), 1)
        self.run_report("2026-11-21T06:00:00Z")
        self.assertEqual(len(self.github.comments[1]), 2)

    def test_human_comments_cannot_delay_reminders(self):
        self.run_report()
        self.github.comments[1].append({"body": incidents.NOTICE, "created_at": "2026-11-13T06:00:00Z",
                                        "user": {"login": "someone-else"}})
        self.run_report("2026-11-14T06:00:00Z")
        self.assertEqual(len(self.github.comments[1]), 2)

    def test_recovery_closes_once_and_recurrence_opens_new_incident(self):
        self.run_report()
        self.recover()
        self.run_report("2026-11-08T06:00:00Z")
        self.assertEqual(self.github.issues[0]["state"], "closed")
        self.assertIn("Source verification recovered", self.github.comments[1][0]["body"])
        self.run_report()
        self.assertEqual(len(self.github.comments[1]), 1)
        self.entry["issues"] = ["collection_failed"]
        self.entry["latest_attempt"]["result"] = {"outcome": "failed", "error": "offline again"}
        self.run_report("2026-11-09T06:00:00Z")
        self.assertEqual(len(self.github.issues), 2)
        self.assertEqual(self.github.issues[1]["state"], "open")

    def test_old_success_in_progress_or_deferred_is_not_recovery(self):
        self.run_report()
        self.recover("2026-11-06T05:00:00Z")
        self.run_report("2026-11-08T06:00:00Z")
        for result in (None, {"outcome": "deferred"}):
            self.entry["latest_attempt"]["result"] = result
            self.run_report()
        self.assertEqual(self.github.issues[0]["state"], "open")
        self.assertEqual(self.github.comments[1], [])

    def test_new_hospital_alerts_immediately_during_an_existing_incident(self):
        self.run_report()
        self.report["hospitals"].append({**self.entry, "slug": "other"})
        self.run_report("2026-11-08T06:00:00Z")
        self.assertEqual(len(self.github.issues), 2)
        self.assertEqual(self.github.comments[1], [])

    def test_collector_failures_remain_fatal_even_with_an_open_incident(self):
        self.run_report()
        for issue in incidents.COLLECTOR_ISSUES:
            self.recover()
            self.entry["issues"] = [issue]
            _, failed = self.run_report("2026-11-08T06:00:00Z")
            self.assertTrue(failed)
            self.assertEqual(self.github.issues[0]["state"], "open")

    def test_discovery_fallback_warning_alone_does_not_open_source_outage(self):
        self.entry["issues"] = ["collection_discovery_failed"]
        _, failed = self.run_report()
        self.assertFalse(failed)
        self.assertEqual(self.github.writes, [])

    def test_dry_run_never_writes(self):
        self.assertIn("Would open", self.run_report(apply=False)[0][0])
        self.assertEqual(self.github.writes, [])
        self.run_report()
        initial_writes = len(self.github.writes)
        self.assertIn("Would remind", self.run_report("2026-11-14T06:00:00Z", apply=False)[0][0])
        self.recover("2026-11-14T05:00:00Z")
        self.assertIn("Would close", self.run_report(apply=False)[0][0])
        self.assertEqual(len(self.github.writes), initial_writes)

    def test_api_error_cannot_be_reported_as_success(self):
        with patch.object(self.github, "request", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_report()

    def test_retry_after_failed_close_does_not_repeat_recovery_comment(self):
        self.run_report()
        self.recover()
        request = self.github.request
        def fail_close(endpoint, method="GET", payload=None):
            if method == "PATCH":
                raise subprocess.CalledProcessError(1, "gh")
            return request(endpoint, method, payload)
        with patch.object(self.github, "request", side_effect=fail_close):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_report("2026-11-08T06:00:00Z")
        self.run_report()
        self.assertEqual(len(self.github.comments[1]), 1)
        self.assertEqual(self.github.issues[0]["state"], "closed")

    def test_missing_observations_or_removed_hospital_cannot_close_issue(self):
        self.run_report()
        self.report["monitoring_since"] = None
        with self.assertRaisesRegex(ValueError, "No collection observations"):
            self.run_report()
        self.report["monitoring_since"] = "2026-10-17T00:00:00Z"
        self.report["hospitals"] = []
        with self.assertRaisesRegex(ValueError, "outside the roster"):
            self.run_report()
        self.assertEqual(self.github.issues[0]["state"], "open")

    def test_invalid_or_duplicate_tracking_state_fails(self):
        self.run_report()
        self.github.issues.append(copy.deepcopy(self.github.issues[0]))
        with self.assertRaisesRegex(ValueError, "Multiple open incidents"):
            self.run_report()
        self.github.issues.pop()
        self.github.issues[0]["body"] = "Deleted tracking state"
        with self.assertRaisesRegex(ValueError, "no tracking marker"):
            self.run_report()

    def test_external_error_redacts_queries_and_cannot_mention_users(self):
        self.entry["latest_attempt"]["result"]["error"] = "@someone https://example.org/a?token=secret\nfailed"
        self.run_report()
        body = self.github.issues[0]["body"]
        self.assertNotIn("secret", body)
        self.assertNotIn("@someone", body)
        self.assertIn("> failed", body)


class GitHubTests(unittest.TestCase):
    def test_listing_paginates_without_search_index(self):
        github = incidents.GitHub("example/history")
        with patch.object(github, "request", side_effect=[[{}] * 100, [{"number": 101}]]) as request:
            result = github.listing("issues?state=open")
        self.assertEqual(len(result), 101)
        self.assertEqual(request.call_args.args[0], "issues?state=open&per_page=100&page=2")

    def test_body_is_json_on_stdin_without_shell_interpolation(self):
        payload = {"body": "line one\n`literal` $(literal)"}
        with patch.object(subprocess, "check_output", return_value='{"number": 1}') as command:
            incidents.GitHub("example/history").request("issues", "POST", payload)
        self.assertEqual(json.loads(command.call_args.kwargs["input"]), payload)
        self.assertNotIn("shell", command.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
