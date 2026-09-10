"""Entry point. Starts three things in one process:

1. A background daemon thread running the daily scheduler - checks every
   60s whether it's time for the configured RUN_AT_LOCAL_TIME and, if so,
   runs every connected account's sort+digest pipeline once.
2. A background daemon thread supervising an optional Cloudflare Tunnel
   subprocess (see tunnel_manager.py) - reacts within seconds to a tunnel
   token being entered, changed, or cleared on the dashboard's Setup page.
3. The dashboard web server (Flask app, served via waitress) on
   0.0.0.0:PORT (default 4568) in the foreground - connect Gmail accounts,
   change settings, trigger manual runs.

All three share one SettingsStore/BootstrapStore pair (not separate
instances pointed at the same files) so the in-process locks actually
serialize concurrent access across threads.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from waitress import serve

import pipeline
from config import BootstrapStore, is_bootstrap_complete
from settings_store import SettingsStore
from state_store import StateStore
from tunnel_manager import TunnelManager
from web_app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

# The 5 required secrets (Gemini key, Google OAuth client, public URL,
# dashboard password) are resolved fresh from BootstrapStore rather than
# once at startup - if they're missing at boot, the dashboard still comes
# up and serves a Setup page (see web_app.py) where they can be typed in.
# Once saved there, this same process picks them up on the next resolve()
# call with no restart needed.
DATA_DIR = os.environ.get("DATA_DIR", "/data")


def scheduler_loop(bootstrap_store: BootstrapStore, store: SettingsStore) -> None:
    state = StateStore(bootstrap_store.data_dir)
    log.info("Scheduler thread started")
    while True:
        try:
            bootstrap = bootstrap_store.resolve()
            if not is_bootstrap_complete(bootstrap):
                # Nothing to do yet - waiting on the Setup page to be filled in.
                time.sleep(60)
                continue

            settings = store.get_settings()
            tz = ZoneInfo(settings.get("timezone", "Europe/London"))
            hh, mm = (int(x) for x in settings.get("run_at_local_time", "07:00").split(":"))
            now = datetime.now(tz)
            today_str = now.strftime("%Y-%m-%d")

            if now.hour == hh and now.minute == mm:
                accounts = store.list_accounts()
                due = [a for a in accounts if state.last_run_date(a["address"]) != today_str]
                if due:
                    log.info("Scheduled run starting for %d account(s)", len(due))
                    pipeline.run_all(bootstrap, store)
                    for a in due:
                        state.mark_ran_today(a["address"], today_str)
        except Exception:
            # A bad settings value or a transient error must not kill the
            # scheduler thread permanently - log and keep ticking.
            log.exception("Scheduler tick failed")
        time.sleep(60)


def main() -> None:
    bootstrap_store = BootstrapStore(DATA_DIR)
    store = SettingsStore(DATA_DIR)

    run_once = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes") or "--once" in sys.argv
    if run_once:
        bootstrap = bootstrap_store.resolve()
        if not is_bootstrap_complete(bootstrap):
            log.error(
                "RUN_ONCE set but setup isn't complete yet - start the dashboard "
                "normally first and fill in the Setup page, then try --once again."
            )
            return
        log.info("RUN_ONCE set - running immediately for all connected accounts and exiting")
        pipeline.run_all(bootstrap, store)
        return

    threading.Thread(target=scheduler_loop, args=(bootstrap_store, store), daemon=True).start()

    tunnel_manager = TunnelManager(bootstrap_store)
    threading.Thread(target=tunnel_manager.loop, daemon=True).start()

    app = create_app(bootstrap_store, store)
    port = bootstrap_store.resolve().port
    log.info("Dashboard listening on 0.0.0.0:%d", port)
    serve(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
