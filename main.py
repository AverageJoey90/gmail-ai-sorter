"""Entry point. Starts two things in one process:

1. A background daemon thread running the daily scheduler - checks every
   60s whether it's time for the configured RUN_AT_LOCAL_TIME and, if so,
   runs every connected account's sort+digest pipeline once.
2. The dashboard web server (Flask app, served via waitress) on
   0.0.0.0:PORT (default 4568) in the foreground - connect Gmail accounts,
   change settings, trigger manual runs.

Both share one SettingsStore instance (not two separate ones pointed at
the same file) so the in-process lock actually serializes concurrent
access between the scheduler thread and web request threads.
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

from . import pipeline
from .config import load_bootstrap_config
from .settings_store import SettingsStore
from .state_store import StateStore
from .web_app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def scheduler_loop(bootstrap, store: SettingsStore) -> None:
    state = StateStore(bootstrap.data_dir)
    log.info("Scheduler thread started")
    while True:
        try:
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
    bootstrap = load_bootstrap_config()
    store = SettingsStore(bootstrap.data_dir)

    run_once = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes") or "--once" in sys.argv
    if run_once:
        log.info("RUN_ONCE set - running immediately for all connected accounts and exiting")
        pipeline.run_all(bootstrap, store)
        return

    threading.Thread(target=scheduler_loop, args=(bootstrap, store), daemon=True).start()

    app = create_app(bootstrap, store)
    log.info("Dashboard listening on 0.0.0.0:%d", bootstrap.port)
    serve(app, host="0.0.0.0", port=bootstrap.port)


if __name__ == "__main__":
    main()
