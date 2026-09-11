"""Supervises an optional `cloudflared tunnel run` subprocess inside this
same container, so that a Cloudflare Tunnel token entered (or changed, or
left blank) through the dashboard's own Setup page takes effect within a
few seconds - no separate Docker service, no Docker socket access, and no
stack redeploy needed.

Runs as one background daemon thread (see main.py), polling BootstrapStore
every POLL_SECONDS and reconciling one cloudflared child process against
whatever cloudflare_tunnel_token is currently resolved:

- No token configured: no process runs. This is the default and is
  perfectly fine if you're exposing the NAS over HTTPS a different way
  (see README section 3, Route B).
- A token is set: a process is started with that token, and left alone as
  long as it's still running and the token hasn't changed.
- The token changes (edited, or cleared) since the process was started:
  the old process is stopped and, if a new non-empty token is present, a
  fresh one is started with it.
- The process exits on its own (crash, transient network issue): treated
  the same as an unset token so the next tick restarts it - cloudflared
  itself already retries individual connection drops internally without
  exiting, so an actual process exit here usually means something more
  persistent (bad token, no network); restarting on a timer rather than
  instantly avoids a tight crash loop.

cloudflared's own stdout/stderr are left to inherit this process's, so its
connection logs show up in the container's normal log output (`docker
logs` / Portainer's log viewer) alongside everything else.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

from config import BootstrapStore

log = logging.getLogger(__name__)

POLL_SECONDS = 15
STOP_TIMEOUT_SECONDS = 10


class TunnelManager:
    def __init__(self, bootstrap_store: BootstrapStore):
        self.bootstrap_store = bootstrap_store
        self._process: subprocess.Popen | None = None
        self._running_token: str = ""
        self._warned_missing_binary = False

    def _binary_path(self) -> str | None:
        path = shutil.which("cloudflared") or (
            "/usr/local/bin/cloudflared" if os.path.exists("/usr/local/bin/cloudflared") else None
        )
        if not path and not self._warned_missing_binary:
            log.warning(
                "cloudflared binary not found in this image - Cloudflare Tunnel token "
                "was set but can't be used. Rebuild from the current Dockerfile to pick it up."
            )
            self._warned_missing_binary = True
        return path

    def _start(self, token: str) -> None:
        binary = self._binary_path()
        if not binary:
            return
        env = dict(os.environ)
        env["TUNNEL_TOKEN"] = token
        try:
            self._process = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "run"], env=env)
            self._running_token = token
            log.info("cloudflared tunnel started (pid %s)", self._process.pid)
        except Exception:
            log.exception("Failed to start cloudflared")
            self._process = None
            self._running_token = ""

    def _stop(self) -> None:
        proc, self._process = self._process, None
        self._running_token = ""
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.info("cloudflared tunnel stopped")

    def _tick(self) -> None:
        cfg = self.bootstrap_store.resolve()
        token = (cfg.cloudflare_tunnel_token or "").strip()

        crashed = bool(self._process) and self._process.poll() is not None
        if crashed:
            code = self._process.returncode
            self._process = None
            self._running_token = ""
            log.warning("cloudflared exited unexpectedly (exit code %s) - will retry", code)
            return  # let the next tick (in POLL_SECONDS) restart it, rather than tight-looping

        if token != self._running_token:
            if self._process:
                self._stop()
            if token:
                self._start(token)

    def loop(self) -> None:
        log.info("Tunnel manager thread started")
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("Tunnel manager tick failed")
            time.sleep(POLL_SECONDS)
