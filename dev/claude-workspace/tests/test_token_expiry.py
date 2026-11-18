"""The refresh token, not the self-rotating access token, is what pages a human."""
import importlib.util
import json
import pathlib
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "token_expiry_monitor", ROOT / "token-expiry" / "monitor.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

NOW = 1_795_000_000_000
DAY = monitor.DAY_MS


def creds(**oauth):
    base = {"accessToken": "sk-ant-oat01-x", "refreshToken": "sk-ant-ort01-x",
            "expiresAt": NOW + 3 * 3_600_000,
            "refreshTokenExpiresAt": NOW + 30 * DAY,
            "subscriptionType": "max"}
    base.update(oauth)
    return json.dumps({"claudeAiOauth": base})


class AssessTests(unittest.TestCase):
    def test_silent_outside_the_window(self):
        body, _, _, _ = monitor.assess(creds(), NOW, 7)
        self.assertIsNone(body)

    def test_an_expiring_access_token_is_not_an_alert(self):
        # It lapses every few hours and the CLI refreshes it unattended; the
        # whole point of this monitor is to not page about that.
        body, _, _, _ = monitor.assess(creds(expiresAt=NOW - DAY), NOW, 7)
        self.assertIsNone(body)

    def test_warns_inside_the_window(self):
        body, title, priority, _ = monitor.assess(
            creds(refreshTokenExpiresAt=NOW + 3 * DAY), NOW, 7)
        self.assertIn("expires in 3 days", body)
        self.assertEqual(title, "Claude workspace token expires soon")
        self.assertEqual(priority, "default")

    def test_last_day_escalates_and_does_not_round_up(self):
        # 23 hours left is "0 days", today, and must not read as tomorrow.
        body, _, priority, _ = monitor.assess(
            creds(refreshTokenExpiresAt=NOW + 23 * 3_600_000), NOW, 7)
        self.assertIn("expires in 0 days", body)
        self.assertEqual(priority, "high")

    def test_one_day_is_singular(self):
        body, _, priority, _ = monitor.assess(
            creds(refreshTokenExpiresAt=NOW + 25 * 3_600_000), NOW, 7)
        self.assertIn("expires in 1 day,", body)
        self.assertEqual(priority, "high")

    def test_already_expired_is_urgent(self):
        body, title, priority, _ = monitor.assess(
            creds(refreshTokenExpiresAt=NOW - DAY), NOW, 7)
        self.assertIn("expired at", body)
        self.assertEqual(title, "Claude workspace login has expired")
        self.assertEqual(priority, "urgent")

    def test_boundary_day_is_still_silent(self):
        body, _, _, _ = monitor.assess(
            creds(refreshTokenExpiresAt=NOW + 7 * DAY + 1), NOW, 7)
        self.assertIsNone(body)

    def test_missing_file_reports_signed_out(self):
        body, title, priority, _ = monitor.assess(None, NOW, 7)
        self.assertEqual(title, "Claude workspace is signed out")
        self.assertEqual(priority, "high")

    def test_unparseable_and_empty_files_report_signed_out(self):
        for raw in ("", "not json", "{}", json.dumps({"mcpOAuth": {}})):
            with self.subTest(raw=raw):
                _, title, _, _ = monitor.assess(raw, NOW, 7)
                self.assertEqual(title, "Claude workspace is signed out")

    def test_a_missing_expiry_field_is_reported_not_swallowed(self):
        # A blind monitor that stays quiet is worse than a rare false alarm.
        payload = json.loads(creds())
        del payload["claudeAiOauth"]["refreshTokenExpiresAt"]
        body, title, _, _ = monitor.assess(json.dumps(payload), NOW, 7)
        self.assertEqual(title, "Claude token expiry unknown")
        self.assertIn("cannot be checked", body)

    def test_the_live_credentials_file_has_the_shape_this_relies_on(self):
        path = pathlib.Path.home() / ".claude" / ".credentials.json"
        if not path.exists():
            self.skipTest("not running on a signed-in workspace")
        oauth = json.loads(path.read_text())["claudeAiOauth"]
        self.assertIsInstance(oauth["refreshTokenExpiresAt"], int)
        self.assertGreater(oauth["refreshTokenExpiresAt"], oauth["expiresAt"])


class ScheduleTests(unittest.TestCase):
    def test_the_chart_warns_for_more_days_than_its_schedule_skips(self):
        # Daily runs are the reminder cadence; a warnDays of 0 or a weekly
        # schedule would make the window unobservable.
        import yaml
        values = yaml.safe_load((ROOT / "values.yaml").read_text())["tokenExpiry"]
        self.assertGreaterEqual(values["warnDays"], 2)
        self.assertRegex(values["schedule"], r"^\S+ \S+ \* \* \*$")


if __name__ == "__main__":
    unittest.main()
