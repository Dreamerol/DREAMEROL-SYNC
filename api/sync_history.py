"""Persist last successful sync timestamps per account/repository."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


_LOCK = threading.RLock()


def _default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "sync_history.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_account(account: str) -> str:
    if not isinstance(account, str) or not account.strip():
        raise ValueError("account is required")
    return account.strip().lower()


def _normalize_repo(repo: str) -> str:
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repository is required")
    return repo.strip()


class SyncHistoryStore:
    """File-backed store of last successful sync timestamps.

    Data shape: {"accounts": {"<account>": {"repos": {"owner/repo": {"last_successful_sync": "<iso>"}}}}}
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"accounts": {}}
        except OSError:
            return {"accounts": {}}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {"accounts": {}}
        if not isinstance(data, dict):
            return {"accounts": {}}
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            data["accounts"] = {}
        return data

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".sync_history.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass

    def _iter_matching_account_entries(self, accounts: dict, acc: str):
        """Yield account entries whose key matches case-insensitively.

        Non-destructive read tolerance for older/variant account key casing.
        """
        if not isinstance(accounts, dict):
            return
        # exact canonical key first (stable), then other matching keys
        seen = set()
        if acc in accounts:
            seen.add(acc)
            yield acc, accounts.get(acc)
        for key, value in accounts.items():
            if key in seen or not isinstance(key, str):
                continue
            if key.strip().lower() == acc:
                seen.add(key)
                yield key, value

    def get_last_successful(self, account: str, repository: str) -> str | None:
        acc = _normalize_account(account)
        repo = _normalize_repo(repository)
        with _LOCK:
            data = self._load()
            accounts = data.get("accounts", {})
            for _, acc_entry in self._iter_matching_account_entries(accounts, acc):
                if not isinstance(acc_entry, dict):
                    continue
                repos = acc_entry.get("repos")
                if not isinstance(repos, dict):
                    continue
                entry = repos.get(repo)
                if not isinstance(entry, dict):
                    continue
                ts = entry.get("last_successful_sync")
                if isinstance(ts, str) and ts:
                    return ts
            return None

    def record_success(self, account: str, repository: str) -> str:
        acc = _normalize_account(account)
        repo = _normalize_repo(repository)
        ts = self._clock()
        with _LOCK:
            data = self._load()
            accounts = data.setdefault("accounts", {})
            if not isinstance(accounts, dict):
                accounts = {}
                data["accounts"] = accounts
            acc_entry = accounts.setdefault(acc, {})
            if not isinstance(acc_entry, dict):
                acc_entry = {}
                accounts[acc] = acc_entry
            repos = acc_entry.setdefault("repos", {})
            if not isinstance(repos, dict):
                repos = {}
                acc_entry["repos"] = repos
            repos[repo] = {"last_successful_sync": ts}
            self._save(data)
        return ts

    def get_map(self, account: str) -> dict[str, str]:
        acc = _normalize_account(account)
        with _LOCK:
            data = self._load()
            accounts = data.get("accounts", {})
            out: dict[str, str] = {}
            for _, acc_entry in self._iter_matching_account_entries(accounts, acc):
                if not isinstance(acc_entry, dict):
                    continue
                repos = acc_entry.get("repos")
                if not isinstance(repos, dict):
                    continue
                for name, entry in repos.items():
                    if not isinstance(name, str) or not isinstance(entry, dict):
                        continue
                    ts = entry.get("last_successful_sync")
                    if isinstance(ts, str) and ts and name not in out:
                        out[name] = ts
            return out
