import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "collect.py"
SPEC = importlib.util.spec_from_file_location("ai_usage_collector", MODULE_PATH)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(collector)


class CollectorTests(unittest.TestCase):
    def test_normalize_weekly_limit(self):
        value = collector.normalize_limit({
            "usedPercent": 48,
            "windowDurationMins": 10080,
            "resetsAt": 1780000000,
        })
        self.assertEqual(value["label"], "Weekly")
        self.assertEqual(value["percent"], 0.48)
        self.assertTrue(value["resetsAt"].endswith("+00:00"))

    def test_normalize_usage_sorts_and_ignores_bad_rows(self):
        summary, days = collector.normalize_usage({
            "summary": {"lifetimeTokens": 30},
            "dailyUsageBuckets": [
                {"startDate": "2026-10-02", "tokens": 20},
                {"tokens": 999},
                {"startDate": "2026-10-01", "tokens": 10},
            ],
        })
        self.assertEqual(summary["lifetimeTokens"], 30)
        self.assertEqual([row["startDate"] for row in days], ["2026-10-01", "2026-10-02"])

    def test_aggregate_sums_accounts_by_day(self):
        accounts = [
            {"available": True, "summary": {"lifetimeTokens": 10}, "dailyUsage": [{"startDate": "2026-10-01", "tokens": 4}]},
            {"available": True, "summary": {"lifetimeTokens": 20}, "dailyUsage": [{"startDate": "2026-10-01", "tokens": 6}]},
            {"available": False, "summary": {"lifetimeTokens": 99}, "dailyUsage": [{"startDate": "2026-10-01", "tokens": 99}]},
        ]
        days, summary = collector.aggregate(accounts)
        self.assertEqual(days, [{"startDate": "2026-10-01", "tokens": 10}])
        self.assertEqual(summary["lifetimeTokens"], 30)
        self.assertEqual(summary["peakDailyTokens"], 10)

    def test_aggregate_handles_many_accounts(self):
        accounts = [
            {
                "available": True,
                "summary": {"lifetimeTokens": (index + 1) * 100},
                "dailyUsage": [
                    {"startDate": "2026-10-01", "tokens": index + 1},
                    {"startDate": "2026-10-02", "tokens": (index + 1) * 2},
                ],
            }
            for index in range(8)
        ]
        days, summary = collector.aggregate(accounts)
        self.assertEqual(days, [
            {"startDate": "2026-10-01", "tokens": 36},
            {"startDate": "2026-10-02", "tokens": 72},
        ])
        self.assertEqual(summary["lifetimeTokens"], 3600)
        self.assertEqual(summary["peakDailyTokens"], 72)

    def test_opencode_go_windows(self):
        limits = collector.normalize_opencode_go({"usage": {
            "rolling": {"percent": 2, "resetsAt": "soon"},
            "weekly": {"percent": 30, "resetsAt": "later"},
        }})
        self.assertEqual([row["label"] for row in limits], ["Rolling", "Weekly"])
        self.assertEqual(limits[1]["percent"], 0.3)

    def test_identity_does_not_expose_account_id(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "auth.json").write_text('{"tokens":{"account_id":"secret-account"}}', encoding="utf-8")
            identity = collector.auth_identity(home)
        self.assertEqual(len(identity), 16)
        self.assertNotIn("secret-account", identity)

    def test_discovery_deduplicates_same_login(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            first.mkdir()
            second.mkdir()
            auth = '{"tokens":{"account_id":"same-account"}}'
            (first / "auth.json").write_text(auth, encoding="utf-8")
            (second / "auth.json").write_text(auth, encoding="utf-8")
            old_configured = collector.configured_accounts
            old_registry = collector.registry_accounts
            try:
                collector.configured_accounts = lambda: []
                collector.registry_accounts = lambda: []
                accounts = collector.discover_accounts([
                    {"label": "One", "home": str(first)},
                    {"label": "Two", "home": str(second)},
                ])
            finally:
                collector.configured_accounts = old_configured
                collector.registry_accounts = old_registry
        matching = [account for account in accounts if account["label"] in ("One", "Two")]
        self.assertEqual(len(matching), 1)

    def test_discovery_keeps_many_unique_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extra = []
            for index in range(8):
                home = root / f"account-{index}"
                home.mkdir()
                (home / "auth.json").write_text(
                    '{"tokens":{"account_id":"account-%d"}}' % index,
                    encoding="utf-8",
                )
                extra.append({"label": f"Account {index}", "home": str(home)})
            with patch.object(collector, "configured_accounts", return_value=[]), patch.object(
                collector, "registry_accounts", return_value=[]
            ):
                accounts = collector.discover_accounts(extra)
        matching = [account for account in accounts if account["label"].startswith("Account ")]
        self.assertEqual(len(matching), 8)
        self.assertEqual(len({account["id"] for account in matching}), 8)

    def test_configured_accounts_reads_current_and_legacy_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            config_home = Path(directory)
            current = config_home / "ai-usage"
            legacy = config_home / "chatgpt-usage"
            current.mkdir()
            legacy.mkdir()
            (current / "accounts.json").write_text(
                '{"accounts":[{"label":"Current","home":"~/.codex-current"}]}',
                encoding="utf-8",
            )
            (legacy / "accounts.json").write_text(
                '{"accounts":[{"label":"Legacy","home":"~/.codex-legacy"}]}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
                accounts = collector.configured_accounts()
        self.assertEqual([account["label"] for account in accounts], ["Current", "Legacy"])


if __name__ == "__main__":
    unittest.main()
