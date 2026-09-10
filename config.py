"""Bootstrap configuration: the small handful of things that only make
sense as deploy-time secrets/env vars (set once in Portainer's stack
Environment variables box and never touched again).

Everything else - which Gmail accounts are connected, the daily schedule,
label-matching confidence threshold, ignore list, per-account digest
recipient - lives in settings_store.py instead, editable anytime from the
web dashboard on port 4568 without redeploying the stack.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


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
    data_dir: str
    port: int
    flask_secret_key: str


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


def load_bootstrap_config() -> BootstrapConfig:
    data_dir = _env("DATA_DIR", "/data")

    gemini_api_key = _env("GEMINI_API_KEY")
    google_client_id = _env("GOOGLE_CLIENT_ID")
    google_client_secret = _env("GOOGLE_CLIENT_SECRET")
    public_base_url = _env("PUBLIC_BASE_URL").rstrip("/")
    dashboard_password = _env("DASHBOARD_PASSWORD")

    missing = [
        name
        for name, val in [
            ("GEMINI_API_KEY", gemini_api_key),
            ("GOOGLE_CLIENT_ID", google_client_id),
            ("GOOGLE_CLIENT_SECRET", google_client_secret),
            ("PUBLIC_BASE_URL", public_base_url),
            ("DASHBOARD_PASSWORD", dashboard_password),
        ]
        if not val
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): " + ", ".join(missing) +
            " - set these in the Portainer stack's Environment variables box. "
            "See .env.example."
        )

    return BootstrapConfig(
        gemini_api_key=gemini_api_key,
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        public_base_url=public_base_url,
        dashboard_password=dashboard_password,
        data_dir=data_dir,
        port=int(_env("PORT", "4568") or "4568"),
        flask_secret_key=_load_or_create_secret_key(data_dir),
    )
