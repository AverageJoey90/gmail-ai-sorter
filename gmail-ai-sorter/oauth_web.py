"""In-app Google OAuth "Web application" flow - lets you connect a Gmail
account entirely from the browser (click a button, sign in to Google,
land back on the dashboard) instead of running a script on another
machine.

This requires the NAS to be reachable over HTTPS at a real hostname
(PUBLIC_BASE_URL) because Google refuses non-HTTPS OAuth redirects except
to localhost - see the README's "Expose the NAS over HTTPS" section for
the Asustor EZ-Connect + Certificate Manager + Reverse Proxy walkthrough.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import Flow

from config import BootstrapConfig
from gmail_client import DEFAULT_SCOPES, GmailClient

REDIRECT_PATH = "/oauth/callback"


def _client_config(cfg: BootstrapConfig) -> dict:
    return {
        "web": {
            "client_id": cfg.google_client_id,
            "client_secret": cfg.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg.public_base_url + REDIRECT_PATH],
        }
    }


def build_flow(cfg: BootstrapConfig, state: str | None = None) -> Flow:
    return Flow.from_client_config(
        _client_config(cfg),
        scopes=DEFAULT_SCOPES,
        state=state,
        redirect_uri=cfg.public_base_url + REDIRECT_PATH,
    )


def start_authorization(cfg: BootstrapConfig) -> tuple[str, str]:
    """Returns (authorization_url, state) - the caller (web_app.py) stashes
    `state` in the signed Flask session and redirects the browser to
    authorization_url."""
    flow = build_flow(cfg)
    auth_url, state = flow.authorization_url(
        access_type="offline",       # required to get a refresh token
        prompt="consent",             # force re-consent so a refresh token
                                       # is issued even on a repeat connect
        include_granted_scopes="true",
    )
    return auth_url, state


def token_path_for(cfg: BootstrapConfig, index: int) -> str:
    return str(Path(cfg.data_dir) / f"account{index}" / "token.json")


def finish_authorization(cfg: BootstrapConfig, full_callback_url: str, state: str, index: int) -> str:
    """Exchanges the authorization code for tokens, writes token.json for
    the given account index, and returns the connected Gmail address.
    `state` must be the value handed back by start_authorization and
    stashed in the caller's signed session, so this can't be tricked into
    completing a flow it didn't start (CSRF protection)."""
    flow = build_flow(cfg, state=state)
    flow.fetch_token(authorization_response=full_callback_url)
    creds = flow.credentials

    token_path = token_path_for(cfg, index)
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": DEFAULT_SCOPES,
    }
    with open(token_path, "w", encoding="utf-8") as fh:
        json.dump(token_data, fh, indent=2)

    return GmailClient(token_path).get_profile_email()
