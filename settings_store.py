"""Everything the web dashboard lets you change lives here, persisted as
one small JSON file in the /data volume. Both the background scheduler
thread and the Flask request-handling threads touch this, so every
public method takes the lock for its whole read-modify-write.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "accounts": [],  # [{"index", "address", "digest_recipient", "connected_at", "last_run": {...} | None}]
    "next_index": 1,
    "run_at_local_time": "07:00",
    "timezone": "Europe/London",
    "ignore_labels": [],
    "classify_confidence_threshold": 0.7,
    # Labels that get their own dedicated "School" section in the digest
    # (top 3, AI-ranked, own recent-window search - see pipeline.py) instead
    # of competing for a slot in the general "Good to know" ranking.
    # Case-insensitive match against the label name. Defaults to "School"
    # since that's the common case, but editable from the dashboard without
    # a redeploy, and more than one label name can be listed.
    "school_section_labels": ["School"],
    # "daily" (default) or "weekly" - how often the sort+digest run fires.
    # Can be overridden per-account (see add_account below); an account
    # left at None uses this global default. Weekly accounts still get
    # checked at their usual daily run-time, but only actually fire once
    # ~7 days have passed since their last run (see main.py's _is_due) -
    # and their labelled-folder sweep looks at everything from the last 7
    # days rather than only unread mail (see pipeline.py).
    "digest_frequency": "daily",
}


class SettingsStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "settings.json"
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)
        if not self._path.exists():
            self._write(dict(DEFAULTS))

    # ---- low-level -----------------------------------------------------------
    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULTS)

    def _write(self, data: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- accounts -----------------------------------------------------------
    def list_accounts(self) -> list[dict]:
        with self._lock:
            return self._read().get("accounts", [])

    def get_account(self, index: int) -> dict | None:
        with self._lock:
            for a in self._read().get("accounts", []):
                if a["index"] == index:
                    return a
        return None

    def add_account(self, address: str) -> int:
        with self._lock:
            data = self._read()
            index = data.get("next_index", 1)
            data.setdefault("accounts", []).append({
                "index": index,
                "address": address,
                "digest_recipient": address,
                "run_at_local_time": None,  # None = use the global default run_at_local_time setting
                "digest_frequency": None,  # None = use the global default digest_frequency setting
                "connected_at": datetime.now(timezone.utc).isoformat(),
                "last_run": None,
            })
            data["next_index"] = index + 1
            self._write(data)
            return index

    def remove_account(self, index: int) -> None:
        with self._lock:
            data = self._read()
            data["accounts"] = [a for a in data.get("accounts", []) if a["index"] != index]
            self._write(data)

    def update_account(self, index: int, **fields) -> None:
        with self._lock:
            data = self._read()
            for a in data.get("accounts", []):
                if a["index"] == index:
                    a.update(fields)
            self._write(data)

    def record_run_result(self, index: int, summary: dict) -> None:
        summary_with_time = {**summary, "at": datetime.now(timezone.utc).isoformat()}
        self.update_account(index, last_run=summary_with_time)

    # ---- global settings -----------------------------------------------------------
    def get_settings(self) -> dict:
        with self._lock:
            data = self._read()
            return {k: data.get(k, v) for k, v in DEFAULTS.items() if k != "accounts"}

    def update_settings(self, **fields) -> None:
        with self._lock:
            data = self._read()
            data.update(fields)
            self._write(data)
