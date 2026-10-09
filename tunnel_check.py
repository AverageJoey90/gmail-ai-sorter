"""Checks whether this container's own public URL is actually reachable
from outside, for the nightly Todoist alert (round 32, Joe's request:
"checks every night that the tunnel is live, if not then it pushes a
message ... as a task").

Deliberately makes a real outbound HTTP request to the *public* URL
(PUBLIC_BASE_URL), not a localhost/loopback check against the Flask app
directly - a loopback check would always succeed even if Tailscale Funnel
(or Cloudflare Tunnel, or whatever's providing HTTPS) is completely down,
since that's a question about the path from the outside world in, not
about whether the Python process itself is still running. The container
already makes outbound internet calls for Gmail/Gemini, so this needs no
extra network access.

Deliberately provider-agnostic: it's just "can something out on the
internet reach /healthz and get a 200 back", so it works the same whether
Joe is using Tailscale Funnel, Cloudflare Tunnel, or a plain reverse proxy.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

HEALTHZ_PATH = "/healthz"


def check_tunnel(public_base_url: str, timeout: int = 10) -> tuple[bool, str]:
    """Returns (reachable, detail). `detail` is always a short, readable
    string - empty on success, otherwise what went wrong (used directly in
    the Todoist task text and the dashboard's last-check status line)."""
    if not public_base_url:
        return False, "No public base URL is set."

    url = public_base_url.rstrip("/") + HEALTHZ_PATH
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.Timeout:
        return False, f"Timed out after {timeout}s reaching {url}."
    except requests.RequestException as exc:
        return False, f"Couldn't reach {url}: {exc}"

    if resp.status_code != 200:
        return False, f"{url} returned HTTP {resp.status_code} instead of 200."
    return True, ""
