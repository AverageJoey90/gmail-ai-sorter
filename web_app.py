"""The dashboard: a small Flask app on port 4568 for connecting Gmail
accounts (via Google's OAuth consent screen, entirely in-browser),
changing the daily schedule/confidence threshold/ignore list, and
triggering an immediate run. Everything it changes is persisted via
SettingsStore in /data/settings.json - no redeploy needed.

Password-gated (DASHBOARD_PASSWORD) because, once this sits behind a
public HTTPS reverse-proxy URL (see README), anyone who finds the URL
could otherwise trigger sends/drafts on your behalf or read the digest
history.
"""
from __future__ import annotations

import html
import logging
import shutil
import threading
from pathlib import Path

from flask import Flask, redirect, request, session, url_for

from . import oauth_web, pipeline
from .ai_client import AiClient
from .config import BootstrapConfig
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a;background:#f5f5f5;margin:0;padding:16px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:20px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.04em;color:#333;border-bottom:2px solid #1a56db;padding-bottom:6px;margin-top:28px}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.muted{color:#666;font-size:13px}
input[type=text],input[type=password],input[type=number]{padding:6px 8px;border:1px solid #ccc;border-radius:6px;font-size:14px;width:100%;box-sizing:border-box}
label{font-size:13px;color:#333;display:block;margin-bottom:4px;margin-top:10px}
button,.btn{display:inline-block;padding:7px 14px;background:#1a56db;color:#fff;border:none;text-decoration:none;border-radius:6px;font-size:13px;cursor:pointer}
button.secondary,.btn.secondary{background:#eee;color:#333}
button.danger,.btn.danger{background:#c0362c}
.flash{background:#fff3cd;border:1px solid #ffe69c;border-radius:6px;padding:8px 12px;margin-bottom:12px;font-size:13px}
form{margin:0}
.field{margin-bottom:10px}
"""


def _page(title: str, body: str) -> str:
    return f"""<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{STYLE}</style></head><body><div class="wrap">{body}</div></body></html>"""


def _esc(s: str) -> str:
    return html.escape(s or "")


def create_app(bootstrap: BootstrapConfig, store: SettingsStore) -> Flask:
    app = Flask(__name__)
    app.secret_key = bootstrap.flask_secret_key

    # ---- auth gate -----------------------------------------------------------
    @app.before_request
    def require_login():
        if request.endpoint in ("login_form", "login_submit", "static"):
            return None
        if not session.get("authed"):
            return redirect(url_for("login_form"))
        return None

    @app.get("/login")
    def login_form():
        error = request.args.get("error", "")
        flash = f'<div class="flash">{_esc(error)}</div>' if error else ""
        body = f"""
        <h1>Gmail AI Sorter</h1>
        {flash}
        <div class="card">
        <form method="post" action="{url_for('login_submit')}">
          <label for="pw">Dashboard password</label>
          <input type="password" id="pw" name="password" autofocus>
          <div style="margin-top:12px"><button type="submit">Log in</button></div>
        </form>
        </div>"""
        return _page("Log in - Gmail AI Sorter", body)

    @app.post("/login")
    def login_submit():
        if request.form.get("password") == bootstrap.dashboard_password:
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        return redirect(url_for("login_form", error="Wrong password."))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_form"))

    # ---- dashboard -----------------------------------------------------------
    @app.get("/")
    def dashboard():
        settings = store.get_settings()
        accounts = store.list_accounts()
        flash = request.args.get("flash", "")
        flash_html = f'<div class="flash">{_esc(flash)}</div>' if flash else ""

        account_cards = []
        for a in accounts:
            last = a.get("last_run")
            if last and last.get("ok"):
                status = (
                    f'Last run {_esc(last.get("at", ""))[:16].replace("T", " ")} UTC &mdash; '
                    f'{last.get("reviewed", 0)} reviewed, {last.get("sorted", 0)} sorted, '
                    f'{last.get("left_in_inbox", 0)} left in inbox, {last.get("needs_reply", 0)} needs reply'
                )
            elif last:
                status = f'Last run failed: {_esc(last.get("error", "unknown error"))}'
            else:
                status = "Not run yet."

            account_cards.append(f"""
            <div class="card">
              <div class="row">
                <div>
                  <strong>{_esc(a['address'])}</strong>
                  <div class="muted">{status}</div>
                </div>
                <div>
                  <form style="display:inline" method="post" action="{url_for('run_one', index=a['index'])}">
                    <button type="submit">Run now</button>
                  </form>
                  <form style="display:inline" method="post" action="{url_for('remove_account', index=a['index'])}"
                        onsubmit="return confirm('Disconnect {_esc(a['address'])}? You can reconnect it any time.');">
                    <button type="submit" class="danger">Disconnect</button>
                  </form>
                </div>
              </div>
              <form method="post" action="{url_for('update_recipient', index=a['index'])}" class="field" style="margin-top:10px">
                <label>Digest recipient</label>
                <div class="row">
                  <input type="text" name="digest_recipient" value="{_esc(a.get('digest_recipient', a['address']))}">
                  <button type="submit" class="secondary">Save</button>
                </div>
              </form>
            </div>""")

        accounts_html = "".join(account_cards) or '<div class="card muted">No Gmail accounts connected yet.</div>'

        body = f"""
        <div class="row"><h1>Gmail AI Sorter</h1><a class="btn secondary" href="{url_for('logout')}">Log out</a></div>
        {flash_html}

        <h2>Gmail accounts</h2>
        {accounts_html}
        <a class="btn" href="{url_for('oauth_start')}">+ Connect a Gmail account</a>
        <form style="display:inline;margin-left:8px" method="post" action="{url_for('run_all_now')}">
          <button type="submit" class="secondary">Run all now</button>
        </form>

        <h2>Settings</h2>
        <div class="card">
        <form method="post" action="{url_for('save_settings')}">
          <div class="field">
            <label for="run_at">Daily run time ({_esc(settings['timezone'])})</label>
            <input type="text" id="run_at" name="run_at_local_time" value="{_esc(settings['run_at_local_time'])}" placeholder="07:00">
          </div>
          <div class="field">
            <label for="threshold">Label-match confidence threshold (0-1)</label>
            <input type="number" id="threshold" name="classify_confidence_threshold" step="0.05" min="0" max="1"
                   value="{settings['classify_confidence_threshold']}">
          </div>
          <div class="field">
            <label for="ignore">Labels to never sort into (comma-separated)</label>
            <input type="text" id="ignore" name="ignore_labels" value="{_esc(', '.join(settings['ignore_labels']))}">
          </div>
          <button type="submit">Save settings</button>
        </form>
        </div>
        """
        return _page("Gmail AI Sorter", body)

    @app.post("/settings")
    def save_settings():
        run_at = request.form.get("run_at_local_time", "07:00").strip()
        try:
            hh, mm = run_at.split(":")
            int(hh), int(mm)
        except ValueError:
            return redirect(url_for("dashboard", flash="Run time must be HH:MM - not saved."))

        try:
            threshold = max(0.0, min(1.0, float(request.form.get("classify_confidence_threshold", 0.7))))
        except ValueError:
            threshold = 0.7

        ignore_labels = [s.strip() for s in request.form.get("ignore_labels", "").split(",") if s.strip()]

        store.update_settings(
            run_at_local_time=run_at,
            classify_confidence_threshold=threshold,
            ignore_labels=ignore_labels,
        )
        return redirect(url_for("dashboard", flash="Settings saved."))

    # ---- account actions -----------------------------------------------------------
    @app.post("/accounts/<int:index>/recipient")
    def update_recipient(index: int):
        recipient = request.form.get("digest_recipient", "").strip()
        if recipient:
            store.update_account(index, digest_recipient=recipient)
        return redirect(url_for("dashboard", flash="Digest recipient updated."))

    @app.post("/accounts/<int:index>/remove")
    def remove_account(index: int):
        account_dir = Path(bootstrap.data_dir) / f"account{index}"
        shutil.rmtree(account_dir, ignore_errors=True)
        store.remove_account(index)
        return redirect(url_for("dashboard", flash="Account disconnected."))

    @app.post("/accounts/<int:index>/run")
    def run_one(index: int):
        account = store.get_account(index)
        if not account:
            return redirect(url_for("dashboard", flash="Account not found."))

        def _run():
            ai = AiClient(bootstrap.gemini_api_key, bootstrap.gemini_model)
            settings = store.get_settings()
            try:
                summary = pipeline.run_account(account, bootstrap, settings, ai)
            except Exception:
                log.exception("Manual run failed for %s", account["address"])
                summary = {"ok": False, "error": "Run failed - see container logs."}
            store.record_run_result(index, summary)

        threading.Thread(target=_run, daemon=True).start()
        return redirect(url_for("dashboard", flash=f"Run started for {account['address']} - refresh in a minute or two for results."))

    @app.post("/run-all")
    def run_all_now():
        threading.Thread(target=pipeline.run_all, args=(bootstrap, store), daemon=True).start()
        return redirect(url_for("dashboard", flash="Run started for all accounts - refresh in a minute or two for results."))

    # ---- OAuth connect flow -----------------------------------------------------------
    @app.get("/oauth/start")
    def oauth_start():
        auth_url, state = oauth_web.start_authorization(bootstrap)
        session["oauth_state"] = state
        return redirect(auth_url)

    @app.get("/oauth/callback")
    def oauth_callback():
        expected_state = session.pop("oauth_state", None)
        got_state = request.args.get("state")
        if not expected_state or expected_state != got_state:
            return redirect(url_for("dashboard", flash="Google sign-in failed (state mismatch) - please try connecting again."))

        if request.args.get("error"):
            return redirect(url_for("dashboard", flash=f"Google sign-in was cancelled ({_esc(request.args['error'])})."))

        # Rebuild the callback URL from the *public* HTTPS base URL rather
        # than trusting request.url: the reverse proxy (see README) forwards
        # to this container over plain HTTP, so Flask would otherwise see
        # an http:// URL and mismatch what was registered with Google as
        # the https:// redirect_uri.
        callback_url = f"{bootstrap.public_base_url}{oauth_web.REDIRECT_PATH}?{request.query_string.decode()}"

        index = store.add_account("(connecting...)")
        try:
            address = oauth_web.finish_authorization(bootstrap, callback_url, expected_state, index)
        except Exception:
            log.exception("OAuth callback failed")
            store.remove_account(index)
            return redirect(url_for("dashboard", flash="Couldn't complete Google sign-in - see container logs."))

        store.update_account(index, address=address, digest_recipient=address)
        return redirect(url_for("dashboard", flash=f"Connected {address}."))

    return app
