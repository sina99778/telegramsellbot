"""
Regression tests for medium-severity QR fixes (deep_debug findings 57-58):

57. The configs-tab empty-state buy button must navigate to the real
    'store' page, not the nonexistent 'shop' (which blanked the mini-app).
58. vless:// credentials must never be sent to api.qrserver.com — QR codes
    are rendered locally by the new authenticated GET /api/miniapp/qr
    endpoint (apps/api/routes/miniapp/qr.py, core.qr.make_qr_bytes).
"""
from __future__ import annotations

import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from apps.api.routes.miniapp.qr import MAX_QR_PAYLOAD_CHARS, render_qr
from apps.api.routes.miniapp.users import _get_current_user

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGES_JS = REPO_ROOT / "miniapp" / "js" / "pages.js"


@pytest.fixture
def miniapp_auth(mock_session):
    user = MagicMock()
    user.status = "active"
    return (user, mock_session)


# ─── Section 58: local QR endpoint ───────────────────────────────────────────


class TestRenderQrEndpoint:
    async def test_returns_png_from_make_qr_bytes(self, miniapp_auth):
        fake_png = b"\x89PNG\r\n\x1a\nfakebody"
        with patch(
            "apps.api.routes.miniapp.qr.make_qr_bytes", return_value=fake_png
        ) as mock_make:
            response = await render_qr(data="vless://uuid@host:443?type=ws", auth=miniapp_auth)

        mock_make.assert_called_once_with("vless://uuid@host:443?type=ws")
        assert response.media_type == "image/png"
        assert response.body == fake_png

    async def test_response_is_never_cached(self, miniapp_auth):
        """The URL carries _auth + the credential payload — the response
        must not be disk-cached by the webview or intermediaries."""
        with patch(
            "apps.api.routes.miniapp.qr.make_qr_bytes", return_value=b"\x89PNG"
        ):
            response = await render_qr(data="vless://x", auth=miniapp_auth)
        assert response.headers["cache-control"] == "no-store"

    async def test_empty_payload_rejected_400(self, miniapp_auth):
        with pytest.raises(HTTPException) as exc_info:
            await render_qr(data="   ", auth=miniapp_auth)
        assert exc_info.value.status_code == 400

    async def test_oversized_payload_rejected_400(self, miniapp_auth):
        with pytest.raises(HTTPException) as exc_info:
            await render_qr(data="v" * (MAX_QR_PAYLOAD_CHARS + 1), auth=miniapp_auth)
        assert exc_info.value.status_code == 400

    async def test_generation_failure_returns_503(self, miniapp_auth):
        """make_qr_bytes returns b'' when segno is missing — surface 503,
        never an empty 200 image."""
        with patch("apps.api.routes.miniapp.qr.make_qr_bytes", return_value=b""):
            with pytest.raises(HTTPException) as exc_info:
                await render_qr(data="vless://x", auth=miniapp_auth)
        assert exc_info.value.status_code == 503

    def test_endpoint_uses_miniapp_auth_dependency(self):
        """The QR endpoint must require the same _get_current_user auth
        (initData header / _auth / _session) as every other miniapp route."""
        auth_param = inspect.signature(render_qr).parameters["auth"]
        assert auth_param.default.dependency is _get_current_user


class TestQrRouterRegistration:
    def test_qr_route_attached_to_included_router(self):
        """apps/api/main.py only includes the sibling routers; the package
        __init__ must graft /qr onto the users router so the endpoint is
        actually served under /api/miniapp."""
        import apps.api.routes.miniapp  # noqa: F401 — executes the wiring
        from apps.api.routes.miniapp.users import router as users_router

        paths = {route.path for route in users_router.routes}
        assert "/qr" in paths


# ─── Frontend regressions (findings 57 + 58) ─────────────────────────────────


class TestPagesJsRegressions:
    def test_no_third_party_qr_service(self):
        """Finding 58: the vless:// credential must not be embedded in an
        api.qrserver.com (or any goqr.me) image URL."""
        source = PAGES_JS.read_text(encoding="utf-8")
        assert "qrserver.com" not in source
        assert "goqr.me" not in source

    def test_qr_img_points_at_local_endpoint(self):
        source = PAGES_JS.read_text(encoding="utf-8")
        assert "/api/miniapp/qr?data=" in source

    def test_empty_state_navigates_to_existing_store_page(self):
        """Finding 57: UI.navigate('shop') targeted a nonexistent page id
        and blanked the whole content area for brand-new users."""
        source = PAGES_JS.read_text(encoding="utf-8")
        assert "UI.navigate('shop')" not in source
        assert "UI.navigate('store')" in source
