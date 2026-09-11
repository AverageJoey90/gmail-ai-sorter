"""Supervises an optional Tailscale Funnel setup inside this same container,
so that entering (or changing, or clearing) a Tailscale auth key through the
dashboard's own Setup page gets you a permanent, free, publicly-trusted
HTTPS URL - no router port-forwarding, no domain to buy, no certificate to
issue or renew yourself. This is the £0 alternative to Cloudflare Tunnel
(tunnel_manager.py, needs a domain you own) and to Asustor's own
EZ-Connect + Certificate Manager + Reverse Proxy route (README section 3).

Runs as one background daemon thread (see main.py), polling BootstrapStore
every POLL_SECONDS and reconciling against whatever tailscale_auth_key /
tailscale_hostname is currently resolved:

- No auth key configured: nothing runs. This is the default and is fine if
  you're exposing the NAS a different way.
- An auth key is set: a `tailscaled` daemon is started (state persisted
  under /data/tailscale-state so the node's identity - and therefore its
  https://<hostname>.<tailnet>.ts.net address - survives container
  restarts), then `tailscale up` joins your tailnet with that key, then
  `tailscale funnel --bg <port>` exposes the dashboard publicly. The
  resulting URL is logged clearly so you can copy it into PUBLIC_BASE_URL.
- The auth key or hostname changes: `tailscale up`/`funnel` are re-run with
  the new values.
- The daemon process exits on its own: treated the same as if nothing were
  configured yet, so the next tick starts the whole sequence fresh.

Why userspace networking: real kernel TUN mode needs a `/dev/net/tun`
device node on the *host* kernel, which not every NAS/embedded Linux build
provides (Asustor's AS1102T doesn't) - trying to pass that device through
in docker-compose.yml fails the whole deployment outright ("no such file
or directory") rather than just disabling Tailscale, so it's not usable
here at all. `--tun=userspace-networking` avoids needing that device or
any special capabilities, at the cost of being slightly slower - fine for
this use case, since Funnel is just proxying HTTP requests to a local
Flask app, not routing general network traffic. One known wrinkle: some
Tailscale versions have a bug where Funnel's TLS handshake silently fails
in userspace mode specifically when running as a non-root user - so this
container still runs as root (see the Dockerfile comment) even though
userspace mode itself doesn't otherwise require it, purely to route around
that bug.

`tailscale`/`tailscaled`'s own stdout/stderr are left to inherit this
process's, so their logs show up in the container's normal log output
(`docker logs` / Portainer's log viewer) alongside everything else.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time

from config import BootstrapStore

log = logging.getLogger(__name__)

POLL_SECONDS = 15
STOP_TIMEOUT_SECONDS = 10
SOCKET_WAIT_SECONDS = 10
STATE_DIR = "/data/tailscale-state"
SOCKET_PATH = "/var/run/tailscale/tailscaled.sock"
DEFAULT_HOSTNAME = "gmail-ai-sorter"


class TailscaleManager:
    def __init__(self, bootstrap_store: BootstrapStore, app_port: int):
        self.bootstrap_store = bootstrap_store
        self.app_port = app_port
        self._daemon: subprocess.Popen | None = None
        # (auth_key, hostname) that `up` + `funnel` were last successfully
        # run with - reset to ("", "") whenever the daemon isn't known-good,
        # so the next tick redoes both steps rather than assuming stale
        # state is still valid.
        self._configured: tuple[str, str] = ("", "")
        self._warned_missing_binary = False

    def _binaries_present(self) -> bool:
        present = bool(shutil.which("tailscaled") and shutil.which("tailscale"))
        if not present and not self._warned_missing_binary:
            log.warning(
                "tailscale/tailscaled binaries not found in this image - a Tailscale "
                "auth key was set but can't be used. Rebuild from the current Dockerfile to pick them up."
            )
            self._warned_missing_binary = True
        return present

    def _tailscale(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tailscale", f"--socket={SOCKET_PATH}", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _start_daemon(self) -> bool:
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
        try:
            self._daemon = subprocess.Popen(
                [
                    "tailscaled",
                    f"--state={STATE_DIR}/tailscaled.state",
                    f"--socket={SOCKET_PATH}",
                    "--tun=userspace-networking",
                ]
            )
        except Exception:
            log.exception("Failed to start tailscaled")
            self._daemon = None
            return False

        for _ in range(SOCKET_WAIT_SECONDS * 2):
            if os.path.exists(SOCKET_PATH):
                return True
            time.sleep(0.5)
        log.warning("tailscaled didn't create its control socket in time")
        return False

    def _configure(self, auth_key: str, hostname: str) -> None:
        up = self._tailscale("up", f"--authkey={auth_key}", f"--hostname={hostname}", "--accept-dns=false", "--reset")
        if up.returncode != 0:
            log.error("tailscale up failed: %s", (up.stderr or up.stdout).strip())
            return

        funnel = self._tailscale("funnel", "--bg", str(self.app_port))
        if funnel.returncode != 0:
            log.error("tailscale funnel failed: %s", (funnel.stderr or funnel.stdout).strip())
            return

        self._configured = (auth_key, hostname)
        self._log_public_url()

    def _log_public_url(self) -> None:
        try:
            status = self._tailscale("status", "--json")
            data = json.loads(status.stdout)
            dns_name = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
            if dns_name:
                log.info("Tailscale Funnel is live: https://%s (set this as PUBLIC_BASE_URL)", dns_name)
        except Exception:
            log.exception("Tailscale is configured but the public URL couldn't be read back - check `tailscale status` in a shell, or the Tailscale admin console")

    def _stop(self) -> None:
        proc, self._daemon = self._daemon, None
        self._configured = ("", "")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.info("tailscaled stopped")

    def _tick(self) -> None:
        if not self._binaries_present():
            return

        cfg = self.bootstrap_store.resolve()
        auth_key = (cfg.tailscale_auth_key or "").strip()
        hostname = (cfg.tailscale_hostname or DEFAULT_HOSTNAME).strip() or DEFAULT_HOSTNAME

        crashed = bool(self._daemon) and self._daemon.poll() is not None
        if crashed:
            code = self._daemon.returncode
            self._daemon = None
            self._configured = ("", "")
            log.warning("tailscaled exited unexpectedly (exit code %s) - will retry", code)
            return  # let the next tick restart everything, rather than tight-looping

        if not auth_key:
            if self._daemon:
                self._stop()
            return

        if not self._daemon:
            if not self._start_daemon():
                return

        if (auth_key, hostname) != self._configured:
            self._configure(auth_key, hostname)

    def loop(self) -> None:
        log.info("Tailscale manager thread started")
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("Tailscale manager tick failed")
            time.sleep(POLL_SECONDS)
