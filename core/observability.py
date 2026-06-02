"""
Error tracking (Sentry) — safe, optional, zero-config-by-default.

`init_sentry(component)` is called once at the start of each process (bot,
worker, api). It is a complete no-op when:
  * `SENTRY_DSN` is not set, OR
  * `sentry-sdk` isn't installed.

So the app runs identically with or without Sentry — the operator just sets
`SENTRY_DSN` (and the dependency, already in pyproject) to turn it on. Once on,
unhandled exceptions and `logger.error(...)` calls across all three processes
are reported, tagged by component.
"""
from __future__ import annotations

import logging

from core.config import settings

logger = logging.getLogger(__name__)

_initialized = False


def init_sentry(component: str) -> bool:
    """Initialise Sentry for this process. Returns True if active."""
    global _initialized
    if _initialized:
        return True

    dsn = (getattr(settings, "sentry_dsn", None) or "").strip()
    if not dsn:
        logger.info("Sentry disabled (no SENTRY_DSN) [%s]", component)
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed — skipping [%s]", component)
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=(getattr(settings, "sentry_environment", None) or settings.app_env),
            traces_sample_rate=float(getattr(settings, "sentry_traces_sample_rate", 0.0) or 0.0),
            send_default_pii=False,
            max_breadcrumbs=50,
        )
        sentry_sdk.set_tag("component", component)
        _initialized = True
        logger.info("Sentry initialised [component=%s, env=%s]", component, settings.app_env)
        return True
    except Exception as exc:  # noqa: BLE001 — observability must never break boot
        logger.warning("Sentry init failed [%s]: %s", component, exc)
        return False
