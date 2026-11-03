import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
            "weekly": {"percent": 30, "resetsAt": 1794182400000},
        }})
        self.assertEqual([row["label"] for row in limits], ["Rolling", "Weekly"])
        self.assertEqual(limits[1]["percent"], 0.3)
        self.assertEqual(limits[1]["resetsAt"], "2026-11-09T00:00:00+00:00")

    def test_opencode_go_prefers_v2_local_api(self):
        payload = {
            "data": {
                "go": {
                    "rolling": {"percent": 4, "resetsAt": 1793707711000},
                    "weekly": {"percent": 12, "resetsAt": 1794182400000},
                    "monthly": {"percent": 8, "resetsAt": 1796051708000},
                }
            }
        }
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch.object(collector.shutil, "which", return_value="/usr/bin/opencode"), patch.object(
            collector.subprocess, "run", return_value=completed
        ) as run:
            result = collector.collect_opencode_go(True)
        self.assertTrue(result["available"])
        self.assertEqual([row["label"] for row in result["limits"]], ["Rolling", "Weekly"])
        self.assertEqual(result["limits"][1]["percent"], 0.12)
        self.assertIn("/api/experimental/usage/limits", run.call_args_list[0].args[0])

    def test_opencode_go_daily_usage_differences_cumulative_stats(self):
        totals = {1: 10, 2: 35, 3: 42}

        def stats_result(command, **_kwargs):
            period = int(command[command.index("--days") + 1])
            payload = {
                "models": [{
                    "model": {"providerID": "opencode", "id": "go-model"},
                    "tokens": {"input": totals[period], "output": 0, "reasoning": 0, "cache": {}},
                }]
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with patch.object(collector.subprocess, "run", side_effect=stats_result):
            rows = collector.collect_opencode_go_stats_usage("/usr/bin/opencode", days=3)
        self.assertEqual([row["tokens"] for row in rows], [7, 25, 10])

    def test_opencode_go_daily_usage_reads_indexed_local_history(self):
        with tempfile.TemporaryDirectory() as directory:
            data_home = Path(directory)
            database_dir = data_home / "opencode"
            database_dir.mkdir()
            database = database_dir / "opencode.db"
            connection = collector.sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE session_message "
                "(id TEXT PRIMARY KEY, type TEXT, time_created INTEGER, data TEXT)"
            )
            today = collector.datetime.now().astimezone()
            payload = json.dumps({
                "model": {"providerID": "opencode"},
                "tokens": {"input": 2, "output": 3, "reasoning": 4, "cache": {"read": 5, "write": 6}},
            })
            connection.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?)",
                ("message-1", "assistant", int(today.timestamp() * 1000), payload),
            )
            connection.commit()
            connection.close()
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
                rows = collector.collect_opencode_go_daily_usage("/usr/bin/opencode", days=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["tokens"], 20)

    def test_usage_token_total_includes_cache_and_reasoning(self):
        self.assertEqual(collector.usage_token_total({
            "input": 1,
            "output": 2,
            "reasoning": 3,
            "cache": {"read": 4, "write": 5},
        }), 15)

    def test_claude_usage_windows(self):
        limits = collector.normalize_claude_usage({
            "five_hour": {"utilization": 21, "resets_at": "2026-11-03T12:00:00+00:00"},
            "seven_day": {"utilization": 45.5, "resets_at": "2026-11-10T07:00:00+00:00"},
            "seven_day_opus": None,
        })
        self.assertEqual([row["label"] for row in limits], ["5h", "Weekly"])
        self.assertEqual(limits[0]["percent"], 0.21)
        self.assertEqual(limits[1]["percent"], 0.455)

    def test_claude_daily_usage_deduplicates_message_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            project = config / "projects" / "project"
            project.mkdir(parents=True)
            timestamp = collector.datetime.now().astimezone().replace(microsecond=0).isoformat()
            row = {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {
                    "id": "message-1",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 4,
                    },
                },
            }
            (project / "session.jsonl").write_text(
                json.dumps(row) + "\n" + json.dumps(row) + "\n",
                encoding="utf-8",
            )
            rows, available, error = collector.collect_claude_daily_usage(config)
        self.assertTrue(available)
        self.assertEqual(error, "")
        self.assertEqual(sum(item["tokens"] for item in rows), 10)

    def test_claude_is_optional_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": directory}
        ):
            self.assertIsNone(collector.collect_claude(True))

    def test_claude_uses_normalized_cache_when_service_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "claude"
            cache = root / "cache"
            config.mkdir()
            (config / ".credentials.json").write_text(
                '{"claudeAiOauth":{"accessToken":"secret","subscriptionType":"max"}}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(config),
                "XDG_CACHE_HOME": str(cache),
            }), patch.object(collector.urllib.request, "urlopen", side_effect=OSError("offline")):
                collector.write_claude_cache({
                    "available": True,
                    "plan": "max",
                    "limits": [{"label": "Weekly", "percent": 0.25, "resetsAt": "later"}],
                    "error": "",
                    "cached": False,
                })
                result = collector.collect_claude(True)
        self.assertTrue(result["available"])
        self.assertTrue(result["cached"])
        self.assertEqual(result["limits"][0]["percent"], 0.25)

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
