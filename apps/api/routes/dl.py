from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter()


_ALLOWED_APPS: dict[str, str] = {
    "v2rayng": "v2rayng://install-config?url={url}",
    "shadowrocket": "shadowrocket://install-sub?url={url}",
    "v2box": "v2box://install-sub?url={url}",
}

_ALLOWED_URL_SCHEMES = {"http", "https"}


def _is_safe_target_url(raw_url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    return True


@router.get("/dl/{app}")
async def deep_link(app: str, url: str) -> RedirectResponse:
    """Redirect HTTP subscription links into application deep links."""
    app_key = app.lower()
    template = _ALLOWED_APPS.get(app_key)
    if template is None:
        logger.warning("dl: rejected unknown app=%s", app)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unsupported app.")

    decoded_url = urllib.parse.unquote(url)
    if not _is_safe_target_url(decoded_url):
        logger.warning("dl: rejected unsafe url for app=%s", app_key)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid subscription URL.")

    safe_url = urllib.parse.quote(decoded_url, safe=":/?=&%")
    target = template.format(url=safe_url)
    return RedirectResponse(url=target)
