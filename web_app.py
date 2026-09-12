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

from flask import Flask, Response, g, redirect, request, session, url_for

import oauth_web
import pipeline
from ai_client import AiClient
from config import BootstrapStore, is_bootstrap_complete, missing_fields
from ics_store import IcsStore
from settings_store import (
    DEFAULT_DIGEST_FREQUENCY,
    DEFAULT_DIGEST_WEEKDAY,
    DEFAULT_RUN_AT_LOCAL_TIME,
    SettingsStore,
    WEEKDAY_NAMES,
)

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


def _weekday_options(selected: str) -> str:
    """Renders <option> tags for a weekday <select> - every account always
    has a real weekday value (see settings_store.py), so there's no blank/
    "use the default" option to render here any more."""
    opts = []
    for name in WEEKDAY_NAMES:
        opts.append(f'<option value="{name}" {"selected" if selected == name else ""}>{name.capitalize()}</option>')
    return "".join(opts)


def create_app(bootstrap_store: BootstrapStore, store: SettingsStore) -> Flask:
    app = Flask(__name__)
    app.secret_key = bootstrap_store.resolve().flask_secret_key

    # ---- resolve the live config on every request -----------------------------
    # A Setup-page save takes effect immediately (no restart) because this
    # re-reads /data/bootstrap.json (layered over env vars) on every request
    # rather than using a value captured once at startup.
    @app.before_request
    def load_bootstrap():
        g.bootstrap = bootstrap_store.resolve()

    # ---- setup gate: nothing else works until the 5 required fields are set ---
    @app.before_request
    def require_setup():
        if request.endpoint in ("setup_form", "setup_submit", "serve_ics", "static"):
            return None
        if not is_bootstrap_complete(g.bootstrap):
            return redirect(url_for("setup_form"))
        return None

    # ---- auth gate -----------------------------------------------------------
    @app.before_request
    def require_login():
        if request.endpoint in ("login_form", "login_submit", "setup_form", "setup_submit", "serve_ics", "static"):
            return None
        if not session.get("authed"):
            return redirect(url_for("login_form"))
        return None

    # ---- first-run setup -------------------------------------------------------
    FIELD_LABELS = {
        "gemini_api_key": ("Gemini API key", "text", "From aistudio.google.com/apikey"),
        "gemini_model": ("Gemini model", "text", "Defaults to gemini-2.5-flash if left blank"),
        "google_client_id": ("Google OAuth client ID", "text", "From your Google Cloud OAuth client (Web application type)"),
        "google_client_secret": ("Google OAuth client secret", "text", ""),
        "public_base_url": ("Public base URL", "text", "e.g. https://joe123.myasustor.com:8443 (no trailing slash) - must match the redirect URI registered with Google"),
        "dashboard_password": ("Dashboard password", "password", "Choose a password to protect this dashboard"),
        "cloudflare_tunnel_token": (
            "Cloudflare Tunnel token",
            "text",
            "Optional - only needed if you're using Cloudflare Tunnel for HTTPS (README section 3, "
            "Route A). Leave blank if you're getting HTTPS a different way. Starting, changing, or "
            "clearing this takes effect within about 15 seconds - no redeploy needed.",
        ),
        "tailscale_auth_key": (
            "Tailscale auth key",
            "text",
            "Optional - only needed if you're using Tailscale Funnel for HTTPS (README section 3, "
            "Route C - free, no domain needed). Generate a reusable key at "
            "login.tailscale.com/admin/settings/keys. Leave blank if you're getting HTTPS a "
            "different way. Takes effect within about 15 seconds - no redeploy needed.",
        ),
        "tailscale_hostname": (
            "Tailscale hostname",
            "text",
            "Optional - only used alongside the auth key above. Defaults to \"gmail-ai-sorter\" if "
            "left blank; this becomes part of your public URL, e.g. https://gmail-ai-sorter.<your-tailnet>.ts.net.",
        ),
    }

    # Fields that are never echoed back into the form once set - just a
    # placeholder note instead, so this page can't leak an already-saved
    # secret to anyone who loads it.
    SECRET_FIELDS = ("google_client_secret", "dashboard_password", "cloudflare_tunnel_token", "tailscale_auth_key")

    @app.get("/setup")
    def setup_form():
        cfg = g.bootstrap
        error = request.args.get("error", "")
        flash = f'<div class="flash">{_esc(error)}</div>' if error else ""
        fields_html = []
        for name, (label, itype, hint) in FIELD_LABELS.items():
            current = getattr(cfg, name, "") or ""
            # Never echo the secret/password fields back into the form.
            value = "" if name in SECRET_FIELDS else _esc(current)
            placeholder = "(already set - leave blank to keep)" if current and name in SECRET_FIELDS else ""
            hint_html = f'<div class="muted">{_esc(hint)}</div>' if hint else ""
            fields_html.append(f"""
            <div class="field">
              <label for="{name}">{_esc(label)}</label>
              <input type="{itype}" id="{name}" name="{name}" value="{value}" placeholder="{_esc(placeholder)}">
              {hint_html}
            </div>""")
        body = f"""
        <h1>Gmail AI Sorter &mdash; first-time setup</h1>
        <p class="muted">These weren't set as environment variables in the Portainer stack, so fill them in here instead.
        They're saved to this container's persistent /data volume and take effect immediately - no redeploy needed.</p>
        {flash}
        <div class="card">
        <form method="post" action="{url_for('setup_submit')}">
          {''.join(fields_html)}
          <button type="submit">Save and continue</button>
        </form>
        </div>"""
        return _page("Setup - Gmail AI Sorter", body)

    @app.post("/setup")
    def setup_submit():
        fields = {name: request.form.get(name, "").strip() for name in FIELD_LABELS}
        bootstrap_store.update(**fields)
        cfg = bootstrap_store.resolve()
        if not is_bootstrap_complete(cfg):
            missing = ", ".join(missing_fields(cfg))
            return redirect(url_for("setup_form", error=f"Still missing: {missing}"))
        return redirect(url_for("login_form"))

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
        if request.form.get("password") == g.bootstrap.dashboard_password:
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

            # Round 18: run time/frequency/weekday are this account's own
            # settings, not an optional override of a global default (Joe:
            # "this can be removed from global settings as there is a box
            # in each account again") - every account record always holds a
            # real value for all three (see settings_store.py), so these
            # `or DEFAULT_*` fallbacks only matter for an account saved by a
            # pre-round-18 version of this app.
            run_at_val = a.get("run_at_local_time") or DEFAULT_RUN_AT_LOCAL_TIME
            frequency_val = a.get("digest_frequency") or DEFAULT_DIGEST_FREQUENCY
            weekday_val = a.get("digest_weekday") or DEFAULT_DIGEST_WEEKDAY
            weekday_wrap_id = f"weekday-wrap-{a['index']}"

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
              <form method="post" action="{url_for('update_schedule', index=a['index'])}" class="field" style="margin-top:10px">
                <label>Run time for this account</label>
                <div class="row">
                  <input type="text" name="run_at_local_time" value="{_esc(run_at_val)}" placeholder="07:00">
                  <button type="submit" class="secondary">Save</button>
                </div>
              </form>
              <form method="post" action="{url_for('update_frequency', index=a['index'])}" class="field" style="margin-top:10px">
                <label>Run frequency for this account</label>
                <div class="row">
                  <select name="digest_frequency" onchange="toggleWeekday(this, '{weekday_wrap_id}')">
                    <option value="daily" {"selected" if frequency_val == "daily" else ""}>Daily</option>
                    <option value="weekly" {"selected" if frequency_val == "weekly" else ""}>Weekly</option>
                  </select>
                  <button type="submit" class="secondary">Save</button>
                </div>
              </form>
              <div id="{weekday_wrap_id}" style="display:{'block' if frequency_val == 'weekly' else 'none'}">
                <form method="post" action="{url_for('update_weekday', index=a['index'])}" class="field" style="margin-top:10px">
                  <label>Weekly run day for this account</label>
                  <div class="row">
                    <select name="digest_weekday">
                      {_weekday_options(weekday_val)}
                    </select>
                    <button type="submit" class="secondary">Save</button>
                  </div>
                </form>
              </div>
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
        <p class="muted">Run time, run frequency, and weekly run day are set per Gmail account now - see each account's card above.</p>
        <div class="card">
        <form method="post" action="{url_for('save_settings')}">
          <div class="field">
            <label for="threshold">Label-match confidence threshold (0-1)</label>
            <input type="number" id="threshold" name="classify_confidence_threshold" step="0.05" min="0" max="1"
                   value="{settings['classify_confidence_threshold']}">
          </div>
          <div class="field">
            <label for="ignore">Labels to never sort into (comma-separated)</label>
            <input type="text" id="ignore" name="ignore_labels" value="{_esc(', '.join(settings['ignore_labels']))}">
          </div>
          <div class="field">
            <label for="school_labels">Keywords that route a label to the "School" digest section (comma-separated)</label>
            <input type="text" id="school_labels" name="school_section_labels" value="{_esc(', '.join(settings['school_section_labels']))}">
            <div class="muted">Matches anywhere in a label's full name/path, case-insensitive - "school" also catches a nested label like "Family/School" or a differently-worded one like "School - Yeomoor Wood", not just a label named exactly "School".</div>
          </div>
          <div class="field">
            <label for="school_lookback_days">School section lookback (days)</label>
            <input type="number" id="school_lookback_days" name="school_lookback_days" step="1" min="1" max="180"
                   value="{settings['school_lookback_days']}">
            <div class="muted">How far back the School section checks for upcoming events/reminders - independent of each account's own run frequency (e.g. 21 for three weeks). Doesn't change what gets sorted or how often the digest runs.</div>
          </div>
          <button type="submit">Save settings</button>
        </form>
        </div>

        <script>
        function toggleWeekday(sel, wrapId) {{
          document.getElementById(wrapId).style.display = sel.value === 'weekly' ? 'block' : 'none';
        }}
        </script>
        """
        return _page("Gmail AI Sorter", body)

    @app.post("/settings")
    def save_settings():
        try:
            threshold = max(0.0, min(1.0, float(request.form.get("classify_confidence_threshold", 0.7))))
        except ValueError:
            threshold = 0.7

        ignore_labels = [s.strip() for s in request.form.get("ignore_labels", "").split(",") if s.strip()]
        school_section_labels = [s.strip() for s in request.form.get("school_section_labels", "").split(",") if s.strip()]

        try:
            school_lookback_days = int(request.form.get("school_lookback_days", 14))
            if school_lookback_days < 1:
                raise ValueError
        except ValueError:
            return redirect(url_for("dashboard", flash="School section lookback must be a whole number of days (1 or more) - not saved."))

        store.update_settings(
            classify_confidence_threshold=threshold,
            ignore_labels=ignore_labels,
            school_section_labels=school_section_labels,
            school_lookback_days=school_lookback_days,
        )
        return redirect(url_for("dashboard", flash="Settings saved."))

    # ---- account actions -----------------------------------------------------------
    @app.post("/accounts/<int:index>/recipient")
    def update_recipient(index: int):
        recipient = request.form.get("digest_recipient", "").strip()
        if recipient:
            store.update_account(index, digest_recipient=recipient)
        return redirect(url_for("dashboard", flash="Digest recipient updated."))

    @app.post("/accounts/<int:index>/schedule")
    def update_schedule(index: int):
        # Round 18: run time is purely a per-account setting now - there's
        # no global default left to fall back to, so a blank/invalid value
        # is rejected outright rather than treated as "use the default".
        run_at = request.form.get("run_at_local_time", "").strip()
        try:
            hh, mm = run_at.split(":")
            int(hh), int(mm)
        except ValueError:
            return redirect(url_for("dashboard", flash="Run time must be HH:MM - not saved."))
        store.update_account(index, run_at_local_time=run_at)
        return redirect(url_for("dashboard", flash=f"Run time for this account set to {run_at}."))

    @app.post("/accounts/<int:index>/frequency")
    def update_frequency(index: int):
        frequency = request.form.get("digest_frequency", "").strip()
        if frequency not in ("daily", "weekly"):
            return redirect(url_for("dashboard", flash="Run frequency must be daily or weekly - not saved."))
        store.update_account(index, digest_frequency=frequency)
        return redirect(url_for("dashboard", flash=f"Run frequency for this account set to {frequency}."))

    @app.post("/accounts/<int:index>/weekday")
    def update_weekday(index: int):
        weekday = request.form.get("digest_weekday", "").strip().lower()
        if weekday not in WEEKDAY_NAMES:
            return redirect(url_for("dashboard", flash="Weekly run day must be a real day of the week - not saved."))
        store.update_account(index, digest_weekday=weekday)
        return redirect(url_for("dashboard", flash=f"Weekly run day for this account set to {weekday.capitalize()}."))

    @app.post("/accounts/<int:index>/remove")
    def remove_account(index: int):
        account_dir = Path(g.bootstrap.data_dir) / f"account{index}"
        shutil.rmtree(account_dir, ignore_errors=True)
        store.remove_account(index)
        return redirect(url_for("dashboard", flash="Account disconnected."))

    @app.post("/accounts/<int:index>/run")
    def run_one(index: int):
        account = store.get_account(index)
        if not account:
            return redirect(url_for("dashboard", flash="Account not found."))

        # flask.g isn't accessible from a separately-spawned thread, so
        # capture the resolved config into a local variable first.
        cfg = g.bootstrap

        def _run():
            ai = AiClient(cfg.gemini_api_key, cfg.gemini_model)
            settings = store.get_settings()
            try:
                summary = pipeline.run_account(account, cfg, settings, ai)
            except Exception:
                log.exception("Manual run failed for %s", account["address"])
                summary = {"ok": False, "error": "Run failed - see container logs."}
            store.record_run_result(index, summary)

        threading.Thread(target=_run, daemon=True).start()
        return redirect(url_for("dashboard", flash=f"Run started for {account['address']} - refresh in a minute or two for results."))

    @app.post("/run-all")
    def run_all_now():
        cfg = g.bootstrap  # captured before the thread starts - see run_one above
        threading.Thread(target=pipeline.run_all, args=(cfg, store), daemon=True).start()
        return redirect(url_for("dashboard", flash="Run started for all accounts - refresh in a minute or two for results."))

    # ---- calendar event links -----------------------------------------------------------
    # Deliberately exempt from login/setup below: this is the link a digest
    # email's "Add to Calendar" button points at, opened straight from
    # whatever mail/browser app the recipient is using (often not logged
    # into this dashboard at all) - tapping it needs to just work. The
    # token is an unguessable UUID4, so this has the same practical
    # exposure as the older calendar.google.com link it replaced (which put
    # the event details directly in a public URL instead of behind a
    # token) - see ics_store.py.
    @app.get("/ics/<token>.ics")
    def serve_ics(token: str):
        ics_store = IcsStore(g.bootstrap.data_dir)
        data = ics_store.read_event(token)
        if data is None:
            return ("This calendar link has expired or wasn't found.", 404)
        return Response(data, mimetype="text/calendar")

    # ---- OAuth connect flow -----------------------------------------------------------
    @app.get("/oauth/start")
    def oauth_start():
        auth_url, state, code_verifier = oauth_web.start_authorization(g.bootstrap)
        session["oauth_state"] = state
        session["oauth_code_verifier"] = code_verifier
        return redirect(auth_url)

    @app.get("/oauth/callback")
    def oauth_callback():
        expected_state = session.pop("oauth_state", None)
        expected_code_verifier = session.pop("oauth_code_verifier", None)
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
        callback_url = f"{g.bootstrap.public_base_url}{oauth_web.REDIRECT_PATH}?{request.query_string.decode()}"

        index = store.add_account("(connecting...)")
        try:
            address = oauth_web.finish_authorization(
                g.bootstrap, callback_url, expected_state, expected_code_verifier, index
            )
        except Exception:
            log.exception("OAuth callback failed")
            store.remove_account(index)
            return redirect(url_for("dashboard", flash="Couldn't complete Google sign-in - see container logs."))

        store.update_account(index, address=address, digest_recipient=address)
        return redirect(url_for("dashboard", flash=f"Connected {address}."))

    return app
