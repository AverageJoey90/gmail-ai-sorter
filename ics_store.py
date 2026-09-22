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

Round 28: alongside <token>.ics, save_event() now also writes a small
<token>.json sidecar with the event's display fields (title, a
human-readable date/time, location) - see read_meta(). This is what feeds
web_app.py's new `/ics/<token>` landing page, added the same round: Joe's
real iPhone testing showed the raw .ics link could still prompt Apple
Calendar to "subscribe" rather than add a one-off event, and since Apple's
exact rule for that isn't reliably documented anywhere, the landing page
means the event's actual details are always visible on screen (with an
explicit "Add to Calendar" button underneath pointing at the same
<token>.ics as before) rather than the outcome being a silent, unexplained
subscribe screen with no fallback.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

RETENTION_SECONDS = 90 * 24 * 60 * 60  # 90 days


class IcsStore:
    def __init__(self, data_dir: str):
        self._dir = Path(data_dir) / "ics"
        self._dir.mkdir(parents=True, exist_ok=True)

    def save_event(
        self, ics_bytes: bytes, *, title: str = "", start_display: str = "", location: str = "",
    ) -> str:
        """Writes the event's .ics bytes (plus its display fields, if given,
        as a JSON sidecar - see read_meta) and returns a new opaque token to
        serve them back by."""
        token = uuid.uuid4().hex
        (self._dir / f"{token}.ics").write_bytes(ics_bytes)
        if title or start_display or location:
            meta = {"title": title, "start_display": start_display, "location": location}
            (self._dir / f"{token}.json").write_text(json.dumps(meta), encoding="utf-8")
        self._prune()
        return token

    def read_event(self, token: str) -> bytes | None:
        """Returns the raw .ics bytes for `token`, or None if it's missing,
        expired, or not a well-formed token - `token` comes straight off a
        URL path segment, so it's validated before it ever touches the
        filesystem (no path traversal via `..`/`/` etc.)."""
        if not self._valid_token(token):
            return None
        try:
            return (self._dir / f"{token}.ics").read_bytes()
        except OSError:
            return None

    def read_meta(self, token: str) -> dict | None:
        """Returns the {"title", "start_display", "location"} saved
        alongside `token`'s .ics bytes, or None if there's no sidecar (an
        older event saved before round 28, or a bad/missing token) - the
        landing page falls back to a generic "tap below to add it" message
        in that case rather than failing outright."""
        if not self._valid_token(token):
            return None
        try:
            return json.loads((self._dir / f"{token}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _valid_token(token: str) -> bool:
        return bool(token) and all(c in "0123456789abcdef" for c in token)

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        try:
            for f in self._dir.glob("*.ics"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        Path(str(f)[:-len(".ics")] + ".json").unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass
