"""Entry point. Starts four things in one process:

1. A background daemon thread running the scheduler - checks every 60s
   whether it's time for each connected account's own run time (an
   optional per-account override, falling back to the global
   RUN_AT_LOCAL_TIME default) and, if so, whether it's actually due given
   its frequency (daily, or weekly - also an optional per-account
   override; a weekly account is checked at the same time of day but only
   fires once ~7 days have passed since its last run - see `_is_due`), and
   if so runs that account's sort+digest pipeline once. Accounts are
   tracked independently, so different run times/frequencies per account
   all work at once.
2. A background daemon thread supervising an optional Cloudflare Tunnel
   subprocess (see tunnel_manager.py) - reacts within seconds to a tunnel
   token being entered, changed, or cleared on the dashboard's Setup page.
3. A background daemon thread supervising an optional Tailscale Funnel
   setup (see tailscale_manager.py) - the free, no-domain alternative to
   Cloudflare Tunnel; reacts within seconds to an auth key being entered,
   changed, or cleared on the dashboard's Setup page.
4. The dashboard web server (Flask app, served via waitress) on
   0.0.0.0:PORT (default 4568) in the foreground - connect Gmail accounts,
   change settings, trigger manual runs.

All four share one SettingsStore/BootstrapStore pair (not separate
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
from ai_client import AiClient
from config import BootstrapStore, is_bootstrap_complete
from settings_store import SettingsStore
from state_store import StateStore
from tailscale_manager import TailscaleManager
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


def _parse_hh_mm(value: str, fallback: str) -> tuple[int, int]:
    try:
        hh, mm = value.split(":")
        return int(hh), int(mm)
    except (ValueError, AttributeError):
        hh, mm = fallback.split(":")
        return int(hh), int(mm)


def _is_due(frequency: str, days_since: int | None) -> bool:
    """Given that it's already this account's scheduled time-of-day (an
    hour/minute match was just checked by the caller), decides whether it
    should actually fire today. `days_since` is how long ago it last ran
    (None = never run before). Daily accounts are due unless they already
    ran today (days_since == 0, e.g. a container restart mid-day re-checked
    the same minute). Weekly accounts are checked at their usual daily
    time-of-day too - there's no separate day-of-week setting - but only
    actually fire once ~7 days have passed since their last run."""
    if frequency == "weekly":
        return days_since is None or days_since >= 7
    return days_since != 0


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
            default_run_at = settings.get("run_at_local_time", "07:00")
            now = datetime.now(tz)
            today_str = now.strftime("%Y-%m-%d")

            # Each account fires at its own run time (an optional override on
            # the account record) or the global default if it hasn't set
            # one - so two accounts on different schedules are each checked
            # and marked as run independently, rather than one shared
            # HH:MM triggering every account at once.
            default_frequency = settings.get("digest_frequency", "daily")
            for account in store.list_accounts():
                hh, mm = _parse_hh_mm(account.get("run_at_local_time") or "", default_run_at)
                if now.hour != hh or now.minute != mm:
                    continue
                frequency = account.get("digest_frequency") or default_frequency
                days_since = state.days_since_last_run(account["address"], now.date())
                if not _is_due(frequency, days_since):
                    continue
                log.info(
                    "Scheduled run starting for %s (run time %02d:%02d, %s)",
                    account["address"], hh, mm, frequency,
                )
                ai = AiClient(bootstrap.gemini_api_key, bootstrap.gemini_model)
                try:
                    summary = pipeline.run_account(account, bootstrap, settings, ai)
                except Exception:
                    log.exception("Scheduled run failed for %s", account["address"])
                    summary = {"ok": False, "error": "Run failed - see container logs."}
                store.record_run_result(account["index"], summary)
                state.mark_ran_today(account["address"], today_str)
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

    port = bootstrap_store.resolve().port

    tunnel_manager = TunnelManager(bootstrap_store)
    threading.Thread(target=tunnel_manager.loop, daemon=True).start()

    tailscale_manager = TailscaleManager(bootstrap_store, port)
    threading.Thread(target=tailscale_manager.loop, daemon=True).start()

    app = create_app(bootstrap_store, store)
    log.info("Dashboard listening on 0.0.0.0:%d", port)
    serve(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
