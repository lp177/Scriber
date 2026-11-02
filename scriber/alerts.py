"""Security e-mail alerts: tell the operator when the dashboard does something sensitive.

Sent for changes that could be used to take data out of Scriber — a provider
endpoint, an API key, the admin login, a new or upgraded API token — and for a
refused endpoint change. Configured only from the environment / ``.env``
(``ALERT_EMAIL_TO`` + ``SMTP_*``, never editable from the dashboard), so a
hijacked dashboard can neither silence nor redirect them.

Best-effort, like the audio archive and the memory refresh: sending runs in the
background, and a missing configuration or an SMTP failure only logs — it never
fails or delays the request that triggered it. Secret values are never put in
an alert, only key names.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from fastapi import Request

from scriber import config

log = logging.getLogger(__name__)

#: SMTP connect/command timeout, in seconds.
SMTP_TIMEOUT_SECONDS = 20

# Strong references to in-flight send tasks (the loop only keeps weak ones).
_TASKS: set[asyncio.Task[None]] = set()


def request_origin(request: Request | None) -> str:
    """Describe where a dashboard request came from (proxy headers included)."""
    if request is None:
        return "unknown"
    parts = [f"peer {request.client.host if request.client else '?'}"]
    for header in ("x-real-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            parts.append(f"{header} {value}")
    agent = request.headers.get("user-agent")
    if agent:
        parts.append(f"user-agent {agent[:200]}")
    return ", ".join(parts)


def _send_blocking(cfg: config.AlertEmailConfig, subject: str, body: str) -> None:
    """Send one message over SMTP (blocking; run in a thread)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.sender or "scriber@localhost"
    message["To"] = ", ".join(cfg.to)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain="scriber.local")
    message.set_content(body)
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            cfg.host, cfg.port, timeout=SMTP_TIMEOUT_SECONDS, context=context
        )
    else:
        smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_SECONDS)
    with smtp:
        if cfg.security == "starttls":
            smtp.starttls(context=context)
        if cfg.username:
            smtp.login(cfg.username, cfg.password)
        smtp.send_message(message)


async def _send(cfg: config.AlertEmailConfig, subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(_send_blocking, cfg, subject, body)
        log.info("Security alert e-mailed: %s", subject)
    except Exception as exc:
        log.error("Could not e-mail the security alert %r: %s", subject, exc)


def security_alert(
    event: str, details: list[str], *, user: str | None = None, request: Request | None = None
) -> None:
    """Log a security event and, when configured, e-mail it in the background.

    *event* is a one-line summary (the mail subject); *details* are the body
    lines, which must never contain a secret value.
    """
    origin = request_origin(request)
    log.warning("SECURITY: %s (by %s, %s): %s", event, user or "?", origin, "; ".join(details))
    cfg = config.get().alert_email
    if not cfg.enabled:
        return
    host = request.headers.get("host", "?") if request is not None else "?"
    body = "\n".join(
        [
            f"Scriber ({host}) — {event}",
            "",
            *details,
            "",
            f"Dashboard user: {user or '?'}",
            f"Origin: {origin}",
            "",
            "If this wasn't you, change ADMIN_PASSWORD and WEB_SECRET in the .env",
            "file, restart Scriber, then check the provider settings and the API",
            "tokens (and rotate any API key whose endpoint was changed).",
        ]
    )
    try:
        task = asyncio.get_running_loop().create_task(
            _send(cfg, f"[Scriber security] {event}", body)
        )
    except RuntimeError:  # no running loop (not expected from a request handler)
        log.error("Could not schedule the security alert e-mail: no event loop.")
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
