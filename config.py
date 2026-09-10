"""Bootstrap configuration: the handful of secrets the app needs to do
anything useful (Gemini key, Google OAuth client, public URL, dashboard
password, and an optional Cloudflare Tunnel token - see tunnel_manager.py).

These can be set as env vars in the Portainer stack, but don't have to be
- if any are missing, the dashboard boots anyway and shows a Setup page
  where they can be typed in through the browser instead. Whatever's
  entered there is persisted to /data/bootstrap.json and takes priority
  over env vars from then on, so the container starts cleanly every time
  regardless of what was filled in at deploy time.

Everything else - which Gmail accounts are connected, the daily schedule,
label-matching confidence threshold, ignore list, per-account digest
recipient - lives in settings_store.py instead, editable anytime from the
web dashboard without redeploying the stack.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

# The fields the Setup page can fill in. Only these 5 are mandatory before
# the rest of the dashboard unlocks - gemini_model has a sensible default
# and cloudflare_tunnel_token is entirely optional (see tunnel_manager.py),
# so neither is ever "missing".
REQUIRED_FIELDS = (
    "gemini_api_key",
    "google_client_id",
    "google_client_secret",
    "public_base_url",
    "dashboard_password",
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class BootstrapConfig:
    gemini_api_key: str
    gemini_model: str
    google_client_id: str
    google_client_secret: str
    public_base_url: str  # e.g. https://joe123.myasustor.com:8443 (no trailing slash)
    dashboard_password: str
    cloudflare_tunnel_token: str  # optional - see tunnel_manager.py
    data_dir: str
    port: int
    flask_secret_key: str


def is_bootstrap_complete(cfg: BootstrapConfig) -> bool:
    return all(getattr(cfg, name) for name in REQUIRED_FIELDS)


def missing_fields(cfg: BootstrapConfig) -> list[str]:
    return [name for name in REQUIRED_FIELDS if not getattr(cfg, name)]


def _load_or_create_secret_key(data_dir: str) -> str:
    """Flask needs a stable secret key to sign session cookies - generate
    one on first boot and persist it so logins survive container restarts,
    unless one is supplied explicitly via FLASK_SECRET_KEY."""
    explicit = _env("FLASK_SECRET_KEY")
    if explicit:
        return explicit

    path = Path(data_dir) / "flask_secret_key.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    key = secrets.token_hex(32)
    os.makedirs(data_dir, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    return key


class BootstrapStore:
    """Resolves the live BootstrapConfig by layering /data/bootstrap.json
    (values entered through the Setup page) over env vars (values set in
    Portainer), store winning whenever it has a non-empty value. Call
    resolve() fresh wherever you need the current config - it's a cheap
    JSON read, and it's how a Setup-page save takes effect immediately
    without a container restart."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._path = Path(data_dir) / "bootstrap.json"
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)

    def _read_overrides(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_overrides(self, data: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def update(self, **fields: str) -> None:
        """Merges non-empty fields into the persisted overrides - an
        empty/blank field in a save never overwrites a previously-saved
        value, so re-submitting the Setup form with one field changed
        doesn't wipe the others."""
        with self._lock:
            data = self._read_overrides()
            for key, value in fields.items():
                if value:
                    data[key] = value
            self._write_overrides(data)

    def resolve(self) -> BootstrapConfig:
        overrides = self._read_overrides()

        def pick(field: str, env_name: str, default: str = "") -> str:
            return overrides.get(field) or _env(env_name, default)

        return BootstrapConfig(
            gemini_api_key=pick("gemini_api_key", "GEMINI_API_KEY"),
            gemini_model=pick("gemini_model", "GEMINI_MODEL", "gemini-2.5-flash"),
            google_client_id=pick("google_client_id", "GOOGLE_CLIENT_ID"),
            google_client_secret=pick("google_client_secret", "GOOGLE_CLIENT_SECRET"),
            public_base_url=pick("public_base_url", "PUBLIC_BASE_URL").rstrip("/"),
            dashboard_password=pick("dashboard_password", "DASHBOARD_PASSWORD"),
            cloudflare_tunnel_token=pick("cloudflare_tunnel_token", "CLOUDFLARE_TUNNEL_TOKEN"),
            data_dir=self.data_dir,
            port=int(_env("PORT", "4568") or "4568"),
            flask_secret_key=_load_or_create_secret_key(self.data_dir),
        )
