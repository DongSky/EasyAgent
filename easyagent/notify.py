"""Run-completion notifications for EasyAgent (multi-client push channel).

The server itself is transport-agnostic: when ``EASYAGENT_NOTIFY_WEBHOOK`` is
set, :func:`notify_run_finished` POSTs a small JSON payload to that URL every
time a run reaches a terminal state. Point the webhook at ntfy.sh, Bark,
Pushover, or any relay you like — that last hop is what wakes up a phone
whose SSE stream was killed in the background.

Never raises: notification is best-effort and must not break the run loop.
Use :func:`notify_run_finished_async` from the run loop so delivery never
blocks mission completion; the sync variant is kept for scripts/tests.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import threading
import urllib.parse
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_TIMEOUT_S = 10.0
_MAX_SUMMARY = 2000


def _webhook_url() -> str:
    return os.environ.get("EASYAGENT_NOTIFY_WEBHOOK", "").strip()


def _notify_on() -> set[str]:
    raw = os.environ.get("EASYAGENT_NOTIFY_ON", "done,failed").lower()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _is_loopback_url(url: str) -> bool:
    """True when the webhook target is loopback: connect directly, bypassing
    any egress proxy from the environment (proxies routinely break
    localhost). LAN/public URLs keep the normal proxy behavior
    (trust_env=True) so user proxy config is respected."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except Exception:
        return False
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def notify_run_finished(run_id: str, status: str, goal: str = "",
                        summary: str = "") -> dict:
    """POST a completion notice to the configured webhook. Never raises."""
    url = _webhook_url()
    if not url:
        return {"ok": False, "reason": "no webhook configured"}
    if status not in _notify_on():
        return {"ok": False, "reason": f"status {status!r} not in notify set"}
    payload = {
        "run_id": run_id,
        "status": status,
        "goal": (goal or "")[:500],
        "summary": (summary or "")[:_MAX_SUMMARY],
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "easyagent",
    }
    try:
        import httpx
        # Loopback/LAN webhooks (e.g. a local ntfy relay) must not be routed
        # through an egress proxy; public URLs keep normal proxy behavior.
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT_S,
                          trust_env=not _is_loopback_url(url))
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.info("notify webhook returned HTTP %s", resp.status_code)
        return {"ok": ok, "http_status": resp.status_code}
    except Exception as exc:  # noqa: BLE001 - best effort by design
        log.info("notify webhook failed (%s)", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}


def notify_run_finished_async(run_id: str, status: str, goal: str = "",
                              summary: str = "") -> threading.Thread | None:
    """Fire-and-forget delivery on a daemon thread. The run loop's ``done``
    event is already emitted before this is called, so completion ordering
    is preserved while delivery (DNS, TLS, retries) can never stall the
    mission thread. Returns the thread, or None when no webhook is set."""
    if not _webhook_url() or status not in _notify_on():
        return None
    t = threading.Thread(
        target=notify_run_finished,
        args=(run_id, status, goal, summary),
        name=f"notify-{run_id[:12]}",
        daemon=True,
    )
    t.start()
    return t
