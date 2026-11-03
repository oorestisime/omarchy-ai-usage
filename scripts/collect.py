#!/usr/bin/env python3
"""Collect account-wide AI subscription usage without reading conversation content."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CLIENT_INFO = {
    "name": "omarchy-ai-usage",
    "title": "Omarchy AI Usage",
    "version": "0.3.0",
}


class CollectorError(RuntimeError):
    pass


def xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


def safe_message(error: BaseException) -> str:
    text = str(error).strip()
    if not text:
        return type(error).__name__
    return text.replace(str(Path.home()), "~")[:240]


def auth_identity(home: Path) -> str:
    try:
        value = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        account_id = str((value.get("tokens") or {}).get("account_id") or "")
    except (OSError, ValueError, TypeError):
        account_id = ""
    seed = account_id or str(home.resolve())
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def configured_accounts() -> list[dict[str, str]]:
    config_home = xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")
    result = []
    for directory in ("ai-usage", "chatgpt-usage"):
        path = config_home / directory / "accounts.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise CollectorError(f"Could not read {path}: {safe_message(error)}") from error
        rows = value.get("accounts", []) if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise CollectorError(f"{path} must contain an accounts array")
        for row in rows:
            if not isinstance(row, dict) or not row.get("home"):
                continue
            result.append({
                "label": str(row.get("label") or Path(str(row["home"])).name),
                "home": str(Path(str(row["home"])).expanduser()),
            })
    return result


def registry_accounts() -> list[dict[str, str]]:
    state_home = xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")
    path = state_home / "omarchy" / "agents" / "accounts" / "codex.json"
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    result = []
    for row in registry.get("accounts", []):
        if not isinstance(row, dict):
            continue
        home = row.get("home") or (Path.home() / ".codex" if row.get("primary") else None)
        if not home:
            continue
        result.append({"label": str(row.get("label") or row.get("id") or "Codex"), "home": str(home)})
    return result


def discover_accounts(extra: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    data_home = xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")
    default_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    candidates: list[dict[str, str]] = [{"label": "Main", "home": str(default_home)}]
    candidates.extend(registry_accounts())
    for directory in ("ai-usage", "chatgpt-usage"):
        accounts_root = data_home / directory / "accounts"
        if accounts_root.is_dir():
            for path in sorted(accounts_root.iterdir()):
                if path.is_dir() and (path / "auth.json").is_file():
                    candidates.append({"label": path.name, "home": str(path)})
    candidates.extend(configured_accounts())
    candidates.extend(extra or [])

    result = []
    seen_paths: set[str] = set()
    seen_identities: set[str] = set()
    for row in candidates:
        home = Path(row["home"]).expanduser()
        path_key = str(home.resolve())
        identity = auth_identity(home)
        if path_key in seen_paths or identity in seen_identities:
            continue
        seen_paths.add(path_key)
        seen_identities.add(identity)
        result.append({"label": row["label"], "home": str(home), "id": identity})
    return result


class RpcClient:
    def __init__(self, codex: str, home: Path):
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        self.process = subprocess.Popen(
            [codex, "-s", "read-only", "-a", "never", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise CollectorError("Codex app-server did not expose its RPC streams")
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout

    def request(self, request_id: int, method: str, params: dict[str, Any] | None = None, timeout: float = 8) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.stdout], [], [], min(0.25, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            line = self.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    detail = message["error"]
                    raise CollectorError(str(detail.get("message") if isinstance(detail, dict) else detail))
                return message.get("result") or {}
        raise CollectorError(f"Codex app-server timed out during {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.stdin.flush()

    def close(self) -> None:
        try:
            self.process.terminate()
        except OSError:
            return
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


def normalize_limit(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict) or window.get("usedPercent") is None:
        return None
    minutes = int(window.get("windowDurationMins") or 0)
    if minutes == 10_080:
        label = "Weekly"
    elif minutes and minutes % 60 == 0:
        label = f"{minutes // 60}h"
    elif minutes:
        label = f"{minutes}m"
    else:
        label = "Limit"
    reset = int(window.get("resetsAt") or 0)
    return {
        "label": label,
        "percent": max(0.0, min(1.0, float(window["usedPercent"]) / 100.0)),
        "resetsAt": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else "",
    }


def normalize_usage(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}, []
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    buckets = []
    for row in value.get("dailyUsageBuckets") or []:
        if not isinstance(row, dict) or not row.get("startDate"):
            continue
        try:
            tokens = max(0, int(row.get("tokens") or 0))
        except (TypeError, ValueError):
            continue
        buckets.append({"startDate": str(row["startDate"]), "tokens": tokens})
    buckets.sort(key=lambda row: row["startDate"])
    return summary, buckets


def collect_account(account: dict[str, str], codex: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": account["id"],
        "label": account["label"],
        "email": "",
        "plan": "",
        "limits": [],
        "summary": {},
        "dailyUsage": [],
        "available": False,
        "error": "",
    }
    client: RpcClient | None = None
    try:
        client = RpcClient(codex, Path(account["home"]))
        client.request(1, "initialize", {"clientInfo": CLIENT_INFO}, timeout=8)
        client.notify("initialized", {})
        limits_result = client.request(2, "account/rateLimits/read", timeout=8)
        usage_result = client.request(3, "account/usage/read", timeout=10)
        try:
            account_result = client.request(4, "account/read", {"refreshToken": False}, timeout=3)
        except CollectorError:
            account_result = {}

        limits = limits_result.get("rateLimits") if isinstance(limits_result, dict) else {}
        limits = limits if isinstance(limits, dict) else {}
        identity = account_result.get("account") if isinstance(account_result, dict) else {}
        identity = identity if isinstance(identity, dict) else {}
        result["email"] = str(identity.get("email") or "")
        result["plan"] = str(limits.get("planType") or identity.get("planType") or "")
        result["limits"] = [entry for entry in (
            normalize_limit(limits.get("primary")),
            normalize_limit(limits.get("secondary")),
        ) if entry]
        result["summary"], result["dailyUsage"] = normalize_usage(usage_result)
        credits = limits_result.get("rateLimitResetCredits") if isinstance(limits_result, dict) else None
        if isinstance(credits, dict) and credits.get("availableCount") is not None:
            result["resetCredits"] = max(0, int(credits.get("availableCount") or 0))
        result["available"] = True
    except (OSError, ValueError, CollectorError) as error:
        result["error"] = safe_message(error)
    finally:
        if client is not None:
            client.close()
    return result


def normalize_reset(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    return str(value or "")


def normalize_opencode_go(payload: Any) -> list[dict[str, Any]]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        raise CollectorError("OpenCode Go returned an unsupported response")
    labels = {"rolling": "Rolling", "weekly": "Weekly"}
    result = []
    for key in ("rolling", "weekly"):
        row = usage.get(key)
        if not isinstance(row, dict) or row.get("percent") is None:
            continue
        result.append({
            "label": labels[key],
            "percent": max(0.0, min(1.0, float(row["percent"]) / 100.0)),
            "resetsAt": normalize_reset(row.get("resetsAt")),
        })
    return result


def usage_token_total(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
    values = (
        usage.get("input"),
        usage.get("output"),
        usage.get("reasoning"),
        cache.get("read"),
        cache.get("write"),
    )
    total = 0
    for value in values:
        try:
            total += max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    return total


def opencode_go_total(payload: Any) -> int:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise CollectorError("OpenCode stats returned an unsupported response")
    total = 0
    for row in models:
        model = row.get("model") if isinstance(row, dict) else None
        provider = str((model or {}).get("providerID") or "")
        if provider in {"opencode", "opencode-go"}:
            total += usage_token_total(row.get("tokens"))
    return total


def collect_opencode_go_stats_usage(opencode: str, days: int = 7) -> list[dict[str, Any]]:
    cumulative = 0
    rows = []
    today = datetime.now().astimezone().date()
    for period in range(1, max(1, days) + 1):
        completed = subprocess.run(
            [opencode, "stats", "--days", str(period), "--json", "--models", "--limit", "1000"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise CollectorError(f"OpenCode stats command failed: {detail}")
        payload = json.loads(completed.stdout)
        total = opencode_go_total(payload)
        rows.append({
            "startDate": (today - timedelta(days=period - 1)).isoformat(),
            "tokens": max(0, total - cumulative),
        })
        cumulative = max(cumulative, total)
    rows.sort(key=lambda row: row["startDate"])
    return rows


def collect_opencode_go_daily_usage(opencode: str, days: int = 49) -> list[dict[str, Any]]:
    data_home = xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")
    database = data_home / "opencode" / "opencode.db"
    if not database.is_file():
        return collect_opencode_go_stats_usage(opencode, min(days, 7))
    today = datetime.now().astimezone().date()
    first_day = today - timedelta(days=max(1, days) - 1)
    cutoff = int(datetime.combine(first_day, datetime.min.time()).astimezone().timestamp() * 1000)
    query = """
        SELECT
            strftime('%Y-%m-%d', time_created / 1000, 'unixepoch', 'localtime') AS start_date,
            SUM(
                COALESCE(json_extract(data, '$.tokens.input'), 0) +
                COALESCE(json_extract(data, '$.tokens.output'), 0) +
                COALESCE(json_extract(data, '$.tokens.reasoning'), 0) +
                COALESCE(json_extract(data, '$.tokens.cache.read'), 0) +
                COALESCE(json_extract(data, '$.tokens.cache.write'), 0)
            ) AS tokens
        FROM session_message
        WHERE time_created >= ?
          AND type = 'assistant'
          AND json_extract(data, '$.model.providerID') IN ('opencode', 'opencode-go')
        GROUP BY start_date
        ORDER BY start_date
    """
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        try:
            values = {str(row[0]): max(0, int(row[1] or 0)) for row in connection.execute(query, (cutoff,))}
        finally:
            connection.close()
    except sqlite3.Error:
        return collect_opencode_go_stats_usage(opencode, min(days, 7))
    return [
        {"startDate": (first_day + timedelta(days=offset)).isoformat(),
         "tokens": values.get((first_day + timedelta(days=offset)).isoformat(), 0)}
        for offset in range(max(1, days))
    ]


def collect_opencode_go_legacy() -> dict[str, Any] | None:
    data_home = xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")
    auth_path = data_home / "opencode" / "auth.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        key = str((auth.get("opencode-go") or {}).get("key") or "")
    except (OSError, ValueError, TypeError):
        key = ""
    if not key:
        return None
    request = urllib.request.Request(
        "https://opencode.ai/zen/go/v1/usage",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "omarchy-ai-usage/0.3.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise CollectorError("OpenCode Go response exceeded 1 MiB")
        payload = json.loads(raw)
        return {"available": True, "limits": normalize_opencode_go(payload), "error": ""}
    except (OSError, TypeError, ValueError, urllib.error.HTTPError, CollectorError) as error:
        return {"available": False, "limits": [], "error": safe_message(error)}


def collect_opencode_go(enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    opencode = shutil.which("opencode")
    command_error = ""
    result: dict[str, Any] | None = None
    if opencode:
        try:
            completed = subprocess.run(
                [opencode, "api", "--standalone", "GET", "/api/experimental/usage/limits"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
                raise CollectorError(f"OpenCode usage command failed: {detail}")
            payload = json.loads(completed.stdout)
            data = payload.get("data") if isinstance(payload, dict) else None
            go = data.get("go") if isinstance(data, dict) else None
            if go is not None:
                result = {"available": True, "limits": normalize_opencode_go({"usage": go}), "error": ""}
            else:
                command_error = "OpenCode Go is not active for the current OpenCode Console account"
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, CollectorError) as error:
            command_error = safe_message(error)

    if result is None:
        legacy = collect_opencode_go_legacy()
        if legacy is not None and legacy.get("available"):
            result = legacy
        elif legacy is not None and not command_error:
            result = legacy
        elif command_error:
            result = {"available": False, "limits": [], "error": command_error}
        else:
            return None

    result["dailyUsage"] = []
    result["dailyUsageAvailable"] = False
    result["dailyUsageError"] = ""
    if opencode:
        try:
            result["dailyUsage"] = collect_opencode_go_daily_usage(opencode)
            result["dailyUsageAvailable"] = True
        except (OSError, subprocess.SubprocessError, TypeError, ValueError, CollectorError) as error:
            result["dailyUsageError"] = safe_message(error)
    return result


def normalize_claude_usage(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise CollectorError("Claude returned an unsupported response")
    windows = [
        ("five_hour", "5h"),
        ("seven_day", "Weekly"),
        ("seven_day_opus", "Opus weekly"),
        ("seven_day_sonnet", "Sonnet weekly"),
        ("seven_day_oauth_apps", "OAuth apps weekly"),
        ("seven_day_cowork", "Cowork weekly"),
    ]
    result = []
    for key, label in windows:
        row = payload.get(key)
        if not isinstance(row, dict) or row.get("utilization") is None:
            continue
        result.append({
            "label": label,
            "percent": max(0.0, min(1.0, float(row["utilization"]) / 100.0)),
            "resetsAt": normalize_reset(row.get("resets_at")),
        })
    return result


def claude_token_total(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        try:
            total += max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def collect_claude_daily_usage(config_home: Path, days: int = 49) -> tuple[list[dict[str, Any]], bool, str]:
    projects = config_home / "projects"
    if not projects.is_dir():
        return [], False, "Claude local history was not found"
    today = datetime.now().astimezone().date()
    first_day = today - timedelta(days=max(1, days) - 1)
    totals = {first_day + timedelta(days=offset): 0 for offset in range(max(1, days))}
    seen: set[str] = set()
    cutoff = datetime.combine(first_day, datetime.min.time()).astimezone().timestamp()
    try:
        paths = projects.rglob("*.jsonl")
        for path in paths:
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        if '"assistant"' not in line or '"usage"' not in line:
                            continue
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if row.get("type") != "assistant":
                            continue
                        message = row.get("message") if isinstance(row.get("message"), dict) else {}
                        usage = message.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        identity = str(message.get("id") or row.get("requestId") or row.get("uuid") or "")
                        if identity and identity in seen:
                            continue
                        timestamp = str(row.get("timestamp") or "")
                        try:
                            moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
                        except ValueError:
                            continue
                        day = moment.date()
                        if day not in totals:
                            continue
                        if identity:
                            seen.add(identity)
                        totals[day] += claude_token_total(usage)
            except OSError:
                continue
    except OSError as error:
        return [], False, safe_message(error)
    return [
        {"startDate": day.isoformat(), "tokens": totals[day]}
        for day in sorted(totals)
    ], True, ""


def claude_cache_path() -> Path:
    cache_home = xdg_path("XDG_CACHE_HOME", Path.home() / ".cache")
    return cache_home / "ai-usage" / "claude.json"


def read_claude_cache(max_age_seconds: int = 86_400) -> dict[str, Any] | None:
    try:
        value = json.loads(claude_cache_path().read_text(encoding="utf-8"))
        saved_at = float(value.get("savedAt") or 0)
        result = value.get("result")
        if time.time() - saved_at > max_age_seconds or not isinstance(result, dict):
            return None
        limits = result.get("limits")
        if not isinstance(limits, list):
            return None
        return {**result, "cached": True}
    except (OSError, ValueError, TypeError):
        return None


def write_claude_cache(result: dict[str, Any]) -> None:
    path = claude_cache_path()
    temporary = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"savedAt": time.time(), "result": result}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def collect_claude(enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    config_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()
    daily_usage, daily_available, daily_error = collect_claude_daily_usage(config_home)
    auth_path = config_home / ".credentials.json"
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        oauth = auth.get("claudeAiOauth") if isinstance(auth, dict) else None
        access_token = str((oauth or {}).get("accessToken") or "")
        plan = str((oauth or {}).get("subscriptionType") or "")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as error:
        return {
            "available": False, "plan": "", "limits": [], "error": safe_message(error),
            "dailyUsage": daily_usage, "dailyUsageAvailable": daily_available,
            "dailyUsageError": daily_error,
        }
    if not access_token:
        return None
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Anthropic-Beta": "oauth-2025-04-20",
            "User-Agent": "omarchy-ai-usage/0.3.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise CollectorError("Claude response exceeded 1 MiB")
        payload = json.loads(raw)
        result = {
            "available": True,
            "plan": plan,
            "limits": normalize_claude_usage(payload),
            "error": "",
            "cached": False,
            "dailyUsage": daily_usage,
            "dailyUsageAvailable": daily_available,
            "dailyUsageError": daily_error,
        }
        write_claude_cache(result)
        return result
    except (OSError, TypeError, ValueError, urllib.error.HTTPError, CollectorError) as error:
        cached = read_claude_cache()
        if cached is not None:
            if plan and not cached.get("plan"):
                cached["plan"] = plan
            cached["dailyUsage"] = daily_usage
            cached["dailyUsageAvailable"] = daily_available
            cached["dailyUsageError"] = daily_error
            return cached
        return {
            "available": False, "plan": plan, "limits": [], "error": safe_message(error),
            "dailyUsage": daily_usage, "dailyUsageAvailable": daily_available,
            "dailyUsageError": daily_error,
        }


def aggregate(accounts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    daily: dict[str, int] = {}
    lifetime = 0
    longest_streak = 0
    current_streak = 0
    for account in accounts:
        if not account.get("available"):
            continue
        summary = account.get("summary") or {}
        lifetime += int(summary.get("lifetimeTokens") or 0)
        longest_streak = max(longest_streak, int(summary.get("longestStreakDays") or 0))
        current_streak = max(current_streak, int(summary.get("currentStreakDays") or 0))
        for row in account.get("dailyUsage") or []:
            day = str(row.get("startDate") or "")
            if day:
                daily[day] = daily.get(day, 0) + int(row.get("tokens") or 0)
    combined = [{"startDate": day, "tokens": daily[day]} for day in sorted(daily)]
    peak = max((row["tokens"] for row in combined), default=0)
    return combined, {
        "lifetimeTokens": lifetime,
        "peakDailyTokens": peak,
        "currentStreakDays": current_streak,
        "longestStreakDays": longest_streak,
    }


def parse_extra_accounts(values: list[str]) -> list[dict[str, str]]:
    result = []
    for value in values:
        if "=" not in value:
            raise CollectorError("--account must be LABEL=PATH")
        label, path = value.split("=", 1)
        if not label.strip() or not path.strip():
            raise CollectorError("--account must be LABEL=PATH")
        result.append({"label": label.strip(), "home": str(Path(path.strip()).expanduser())})
    return result


def build_snapshot(
    extra_accounts: list[dict[str, str]],
    include_opencode_go: bool,
    include_claude: bool,
) -> dict[str, Any]:
    codex = shutil.which("codex")
    discovered = discover_accounts(extra_accounts)
    if codex:
        workers = min(4, max(1, len(discovered)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            accounts = list(pool.map(lambda account: collect_account(account, codex), discovered))
    else:
        accounts = [{
            "id": account["id"], "label": account["label"], "email": "", "plan": "",
            "limits": [], "summary": {}, "dailyUsage": [], "available": False,
            "error": "Codex CLI is not installed",
        } for account in discovered]
    combined, summary = aggregate(accounts)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "Codex app-server account/usage/read",
        "accounts": accounts,
        "combinedDailyUsage": combined,
        "summary": summary,
        "claude": collect_claude(include_claude),
        "openCodeGo": collect_opencode_go(include_opencode_go),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--no-claude", action="store_true")
    parser.add_argument("--no-opencode-go", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        snapshot = build_snapshot(
            parse_extra_accounts(args.account),
            not args.no_opencode_go,
            not args.no_claude,
        )
    except CollectorError as error:
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "error": safe_message(error)}))
        return 1
    print(json.dumps(snapshot, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
