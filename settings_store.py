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
    # How many days back the School section's own search looks (see
    # pipeline.py) - deliberately independent of the daily/weekly frequency
    # below, since the School section is a "what's coming up" reminder, not
    # part of the normal sort/triage sweep. Default 14; Joe can set this to
    # e.g. 21 for three weeks of lookback without changing how often the
    # digest itself runs.
    "school_lookback_days": 14,
    # Joe: "if you find an old Daily or Weekly digest then move that into
    # the Weekly Digest folder/label to be replaced by the newest one...
    # have this as a global setting". Off by default; applies to every
    # connected account when on (see pipeline._archive_old_digests) -
    # unlike run time/frequency/weekday (round 18), this one Joe explicitly
    # asked to keep global rather than per-account.
    "move_old_digests_to_weekly_folder": False,
    # The exact label/folder name (full path, e.g. Joe's own real one,
    # "INBOX/Weekly Digest" - a nested label) to reuse for the above. Kept
    # as a plain editable setting, same pattern as ignore_labels/
    # school_section_labels, rather than a hardcoded name - Gmail's nested
    # labels use the full path as their real name, so this has to be
    # exactly right (case-insensitive, per get_or_create_label) to find an
    # existing folder rather than creating a near-duplicate top-level one.
    "weekly_digest_label_name": "Weekly Digest",
}

# Round 18: Joe asked for the run time / frequency / weekday to be purely
# per-account settings ("there dosent need to be a run daily or weekley
# global variable in settings this can be removed and have this setting in
# each account. same as run time, this can be removed from global settings
# as there is a box in each account again") - so these are no longer part
# of DEFAULTS/get_settings() at all, and every account record always holds
# a real value for all three (add_account seeds them below; the dashboard's
# per-account forms always save a real value too - see web_app.py). These
# constants exist only as the seed value for a newly-connected account and
# as a defensive fallback in main.py/web_app.py for an account record saved
# by a pre-round-18 version of this app that still has one of these fields
# set to None (from the old "None = use the global default" scheme).
DEFAULT_RUN_AT_LOCAL_TIME = "07:00"
DEFAULT_DIGEST_FREQUENCY = "daily"
DEFAULT_DIGEST_WEEKDAY = "monday"

# Shared by main.py's scheduler (deciding whether a weekly account is due
# today) and web_app.py's dashboard (rendering/validating the day picker).
# Lives here rather than in either of those two modules specifically to
# avoid a circular import - main.py imports web_app.py, so a constant
# either of them needed couldn't live in the other.
WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def weekday_index(name: str | None, fallback: str = DEFAULT_DIGEST_WEEKDAY) -> int:
    """Maps a day name to Python's date.weekday() convention (0=Monday .. 6=Sunday),
    defensively falling back to `fallback` (itself falling back to Monday) for
    anything missing/unrecognised rather than raising."""
    try:
        return WEEKDAY_NAMES.index((name or fallback).strip().lower())
    except (ValueError, AttributeError):
        try:
            return WEEKDAY_NAMES.index(fallback)
        except ValueError:
            return 0


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
                # Round 18: these are the account's own real settings from
                # the moment it's connected, not "None = use the global
                # default" - seeded with sensible defaults, changeable any
                # time from its card on the dashboard.
                "run_at_local_time": DEFAULT_RUN_AT_LOCAL_TIME,
                "digest_frequency": DEFAULT_DIGEST_FREQUENCY,
                "digest_weekday": DEFAULT_DIGEST_WEEKDAY,
                # Joe: 'leave unread emails in inbox for 7 days before
                # labelling ... you still need to read them to allow them to
                # appear in the good to know and school sections and make a
                # draft'. Off by default so behaviour is unchanged unless a
                # user opts in on that account's card.
                "hold_unread_emails": False,
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
