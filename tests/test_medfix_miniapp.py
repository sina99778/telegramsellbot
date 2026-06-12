"""
Regression tests for medium-severity mini-app fixes (apps/api/routes/miniapp/users.py):

33. Admin review_payment action must lock the Payment row (SELECT ... FOR UPDATE)
    before calling review_gateway_payment, honoring the process_successful_payment
    lock contract (mirrors the /payments/{id}/refresh endpoint).
34/68. POST /admin/users/{id}/message must return {ok: true, message: ...} and
    record a "send_message" audit-log entry on success (the tail was previously
    stranded as unreachable dead code inside admin_transfer_user_configs).
35. _notify_ticket_user must html-escape the admin reply (HTML parse mode) and
    only a TelegramForbiddenError may mark the customer as is_bot_blocked —
    a TelegramBadRequest (formatting error) must not corrupt the flag.
36. New-ticket admin alert must html-escape the user text/first_name so a
    literal '<' does not silently kill every admin notification.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import HTTPException

from apps.api.routes.miniapp.users import (
    _notify_ticket_user,
    post_admin_action,
    reply_admin_ticket,
    send_admin_user_message,
    send_ticket_message,
)
from core.config import settings
from schemas.api.miniapp import SendTicketRequest


@pytest.fixture
def admin_user():
    admin = MagicMock()
    admin.id = uuid4()
    admin.telegram_id = 999
    admin.role = "admin"
    admin.status = "active"
    return admin


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    return bot


def _patch_bot(mock_bot):
    return patch(
        "apps.api.routes.miniapp.users.PremiumEmojiBot", return_value=mock_bot
    )


# ─── Section 33: admin review_payment row lock ───────────────────────────────


class TestReviewPaymentActionRowLock:
    async def test_review_payment_locks_row_for_update(
        self, mock_session, admin_user, make_payment, payment_id
    ):
        """The Payment must be loaded with FOR UPDATE (not session.get) so an
        admin double-tap or a race with the IPN webhook/worker serializes."""
        payment = make_payment(provider="nowpayments")
        mock_session.scalar = AsyncMock(return_value=payment)

        with patch(
            "apps.api.routes.miniapp.users.review_gateway_payment",
            new=AsyncMock(return_value="finished"),
        ):
            result = await post_admin_action(
                {"action": "review_payment", "id": str(payment_id)},
                (admin_user, mock_session),
            )

        assert result["ok"] is True
        # The lock contract: a SELECT ... FOR UPDATE, not a plain session.get.
        mock_session.get.assert_not_awaited()
        stmt = mock_session.scalar.call_args.args[0]
        assert stmt._for_update_arg is not None
        assert "FOR UPDATE" in str(stmt)

    async def test_review_payment_unknown_404(self, mock_session, admin_user):
        mock_session.scalar = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await post_admin_action(
                {"action": "review_payment", "id": str(uuid4())},
                (admin_user, mock_session),
            )
        assert exc_info.value.status_code == 404


# ─── Sections 34/68: send_admin_user_message success tail ────────────────────


class TestSendAdminUserMessageResponse:
    async def test_success_returns_ok_and_records_audit(
        self, mock_session, admin_user, mock_bot
    ):
        target = MagicMock()
        target.id = uuid4()
        target.telegram_id = 4242
        mock_session.get = AsyncMock(return_value=target)

        with _patch_bot(mock_bot):
            result = await send_admin_user_message(
                target.id, {"text": "سلام"}, (admin_user, mock_session)
            )

        # Previously the endpoint fell off the end and FastAPI returned null.
        assert result == {"ok": True, "message": "پیام برای کاربر ارسال شد."}
        mock_bot.send_message.assert_awaited_once()
        mock_bot.session.close.assert_awaited_once()

        # The send_message audit-log entry must be written.
        audit_entries = [call.args[0] for call in mock_session.add.call_args_list]
        send_logs = [e for e in audit_entries if getattr(e, "action", None) == "send_message"]
        assert len(send_logs) == 1
        assert send_logs[0].entity_id == target.id
        assert send_logs[0].payload == {"telegram_id": target.telegram_id}
        mock_session.flush.assert_awaited()

    async def test_blocked_user_still_raises_400(
        self, mock_session, admin_user, mock_bot
    ):
        target = MagicMock()
        target.id = uuid4()
        target.telegram_id = 4242
        target.is_bot_blocked = False
        mock_session.get = AsyncMock(return_value=target)
        mock_bot.send_message = AsyncMock(
            side_effect=TelegramForbiddenError(
                method=MagicMock(), message="bot was blocked by the user"
            )
        )

        with _patch_bot(mock_bot):
            with pytest.raises(HTTPException) as exc_info:
                await send_admin_user_message(
                    target.id, {"text": "سلام"}, (admin_user, mock_session)
                )

        assert exc_info.value.status_code == 400
        assert target.is_bot_blocked is True
        # No success audit-log on failure.
        audit_entries = [call.args[0] for call in mock_session.add.call_args_list]
        assert not [e for e in audit_entries if getattr(e, "action", None) == "send_message"]


# ─── Section 35: ticket reply escaping + block classification ────────────────


class TestNotifyTicketUser:
    async def test_reply_text_is_html_escaped(self, mock_bot):
        with _patch_bot(mock_bot):
            result = await _notify_ticket_user(123, uuid4(), "سرعت <10MB")

        assert result == "delivered"
        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        assert "&lt;10MB" in sent_text
        assert "<10MB" not in sent_text
        mock_bot.session.close.assert_awaited_once()

    async def test_forbidden_returns_blocked(self, mock_bot):
        mock_bot.send_message = AsyncMock(
            side_effect=TelegramForbiddenError(
                method=MagicMock(), message="bot was blocked by the user"
            )
        )
        with _patch_bot(mock_bot):
            result = await _notify_ticket_user(123, uuid4(), "متن")
        assert result == "blocked"

    async def test_bad_request_returns_failed_not_blocked(self, mock_bot):
        mock_bot.send_message = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="can't parse entities"
            )
        )
        with _patch_bot(mock_bot):
            result = await _notify_ticket_user(123, uuid4(), "متن")
        assert result == "failed"


class TestReplyAdminTicketBlockFlag:
    def _make_ticket(self):
        ticket = MagicMock()
        ticket.id = uuid4()
        ticket.user_id = uuid4()
        ticket.status = "open"
        ticket.messages = []
        ticket.user = MagicMock()
        ticket.user.id = ticket.user_id
        ticket.user.telegram_id = 555
        ticket.user.is_bot_blocked = False
        return ticket

    def _patch_repo(self, ticket):
        repo = MagicMock()
        repo.get_ticket_with_messages = AsyncMock(return_value=ticket)
        repo.add_message = AsyncMock()
        return patch(
            "apps.api.routes.miniapp.users.TicketRepository", return_value=repo
        )

    async def test_failed_delivery_does_not_mark_blocked(
        self, mock_session, admin_user
    ):
        """A BadRequest (formatting error) must NOT flag the user as having
        blocked the bot."""
        ticket = self._make_ticket()
        with (
            self._patch_repo(ticket),
            patch(
                "apps.api.routes.miniapp.users._notify_ticket_user",
                new=AsyncMock(return_value="failed"),
            ),
        ):
            result = await reply_admin_ticket(
                ticket.id, SendTicketRequest(text="پاسخ"), (admin_user, mock_session)
            )

        assert ticket.user.is_bot_blocked is False
        assert result["ok"] is True
        assert "ارسال پیام تلگرام به کاربر انجام نشد" in result["message"]

    async def test_blocked_delivery_marks_blocked(self, mock_session, admin_user):
        ticket = self._make_ticket()
        with (
            self._patch_repo(ticket),
            patch(
                "apps.api.routes.miniapp.users._notify_ticket_user",
                new=AsyncMock(return_value="blocked"),
            ),
        ):
            result = await reply_admin_ticket(
                ticket.id, SendTicketRequest(text="پاسخ"), (admin_user, mock_session)
            )

        assert ticket.user.is_bot_blocked is True
        assert result["ok"] is True

    async def test_delivered_reports_success(self, mock_session, admin_user):
        ticket = self._make_ticket()
        with (
            self._patch_repo(ticket),
            patch(
                "apps.api.routes.miniapp.users._notify_ticket_user",
                new=AsyncMock(return_value="delivered"),
            ),
        ):
            result = await reply_admin_ticket(
                ticket.id, SendTicketRequest(text="پاسخ"), (admin_user, mock_session)
            )

        assert ticket.user.is_bot_blocked is False
        assert "ارسال شد" in result["message"]


# ─── Section 36: new-ticket admin alert escaping + per-admin isolation ───────


class TestTicketAdminAlertEscaping:
    def _setup(self, mock_session, mock_bot, text):
        user = MagicMock()
        user.id = uuid4()
        user.telegram_id = 777
        user.first_name = "علی <tester>"

        ticket = MagicMock()
        ticket.id = uuid4()
        ticket.status = "open"

        repo = MagicMock()
        repo.get_open_ticket_for_user = AsyncMock(return_value=ticket)
        repo.add_message = AsyncMock()

        admin1 = MagicMock()
        admin1.telegram_id = 111
        admin2 = MagicMock()
        admin2.telegram_id = 222
        admins_result = MagicMock()
        admins_result.scalars.return_value.all.return_value = [admin1, admin2]
        mock_session.execute = AsyncMock(return_value=admins_result)

        return user, ticket, repo

    async def test_alert_escapes_user_text_and_name(self, mock_session, mock_bot):
        user, ticket, repo = self._setup(mock_session, mock_bot, "پینگ <50")

        with (
            patch("apps.api.routes.miniapp.users.TicketRepository", return_value=repo),
            patch("apps.api.routes.miniapp.users.check_rate_limit", new=AsyncMock()),
            _patch_bot(mock_bot),
        ):
            result = await send_ticket_message(
                SendTicketRequest(text="پینگ <50"), (user, mock_session)
            )

        assert result["ok"] is True
        assert mock_bot.send_message.await_count >= 2
        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        # User-controlled parts are escaped; structural HTML tags survive.
        assert "&lt;50" in sent_text
        assert "<50" not in sent_text
        assert "&lt;tester&gt;" in sent_text
        assert "<tester>" not in sent_text
        assert "<b>" in sent_text

    async def test_one_admin_failure_does_not_stop_other_sends(
        self, mock_session, mock_bot
    ):
        user, ticket, repo = self._setup(mock_session, mock_bot, "سلام")
        reached: list[int] = []

        async def _send(chat_id, text, parse_mode):
            if chat_id == 111:
                raise TelegramBadRequest(method=MagicMock(), message="boom")
            reached.append(chat_id)

        mock_bot.send_message = AsyncMock(side_effect=_send)

        with (
            patch("apps.api.routes.miniapp.users.TicketRepository", return_value=repo),
            patch("apps.api.routes.miniapp.users.check_rate_limit", new=AsyncMock()),
            _patch_bot(mock_bot),
        ):
            result = await send_ticket_message(
                SendTicketRequest(text="سلام"), (user, mock_session)
            )

        # The ticket is saved and the loop kept going past the failing admin.
        assert result["ok"] is True
        assert 222 in reached
        if settings.owner_telegram_id:
            assert settings.owner_telegram_id in reached
        mock_bot.session.close.assert_awaited_once()
