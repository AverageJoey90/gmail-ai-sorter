"""Tiny persisted state so a container restart mid-day doesn't trigger a
second run (and therefore a second digest email) on the same day.

Deliberately not used for message-level dedupe: draft-duplication is
guarded live against the Gmail API (see
GmailClient.has_existing_draft_for_thread), and re-reviewing whatever is
still sitting in the inbox each day is the intended behaviour (matches how
the existing Cowork-based digest already works), not a bug to fix with a
processed-ids cache.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "state.json"
        self._data: dict = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def last_run_date(self, account_address: str) -> str | None:
        return self._data.get("last_run_date", {}).get(account_address)

    def mark_ran_today(self, account_address: str, date_str: str) -> None:
        self._data.setdefault("last_run_date", {})[account_address] = date_str
        self._save()

    def _save(self) -> None:
        os.makedirs(self._path.parent, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)
