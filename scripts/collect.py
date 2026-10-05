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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
CLIENT_INFO = {
    "name": "omarchy-ai-usage",
    "title": "Omarchy AI Usage",
    "version": "0.1.0",
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


def normalize_opencode_go(payload: Any) -> list[dict[str, Any]]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        raise CollectorError("OpenCode Go returned an unsupported response")
    labels = {"rolling": "Rolling", "weekly": "Weekly", "monthly": "Monthly"}
    result = []
    for key in ("rolling", "weekly", "monthly"):
        row = usage.get(key)
        if not isinstance(row, dict) or row.get("percent") is None:
            continue
        result.append({
            "label": labels[key],
            "percent": max(0.0, min(1.0, float(row["percent"]) / 100.0)),
            "resetsAt": str(row.get("resetsAt") or ""),
        })
    return result


def collect_opencode_go(enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
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
        headers={"Authorization": f"Bearer {key}", "User-Agent": "omarchy-ai-usage/0.1.0"},
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


def build_snapshot(extra_accounts: list[dict[str, str]], include_opencode_go: bool) -> dict[str, Any]:
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
        "openCodeGo": collect_opencode_go(include_opencode_go),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--no-opencode-go", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        snapshot = build_snapshot(parse_extra_accounts(args.account), not args.no_opencode_go)
    except CollectorError as error:
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "error": safe_message(error)}))
        return 1
    print(json.dumps(snapshot, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
