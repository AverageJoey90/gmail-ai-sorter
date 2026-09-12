"""Persists generated .ics event files on disk so the dashboard can serve
them back by an opaque token - this is what makes the digest's "Add to
Calendar" link a real .ics file (see ics_builder.py and web_app.py's
`/ics/<token>` route) rather than a Google-Calendar-specific web link.

Files live under <data_dir>/ics/<token>.ics, so they survive container
restarts and keep working for as long as an old digest email sits around
in someone's inbox. Deliberately unauthenticated when served (the token
is the only "credential" - same exposure as the plain calendar.google.com
link this replaced, which put the event details directly in the URL
instead), so the token is a full UUID4 hex string - not sequential, not
guessable. save_event() opportunistically prunes anything past the
retention window on every call, which is cheap enough here that a
separate cleanup job/thread isn't worth the extra moving part.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

RETENTION_SECONDS = 90 * 24 * 60 * 60  # 90 days


class IcsStore:
    def __init__(self, data_dir: str):
        self._dir = Path(data_dir) / "ics"
        self._dir.mkdir(parents=True, exist_ok=True)

    def save_event(self, ics_bytes: bytes) -> str:
        """Writes the event's .ics bytes and returns a new opaque token to
        serve it back by."""
        token = uuid.uuid4().hex
        (self._dir / f"{token}.ics").write_bytes(ics_bytes)
        self._prune()
        return token

    def read_event(self, token: str) -> bytes | None:
        """Returns the raw .ics bytes for `token`, or None if it's missing,
        expired, or not a well-formed token - `token` comes straight off a
        URL path segment, so it's validated before it ever touches the
        filesystem (no path traversal via `..`/`/` etc.)."""
        if not token or not all(c in "0123456789abcdef" for c in token):
            return None
        try:
            return (self._dir / f"{token}.ics").read_bytes()
        except OSError:
            return None

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        try:
            for f in self._dir.glob("*.ics"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    continue
        except OSError:
            pass
