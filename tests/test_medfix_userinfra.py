"""Regression tests for inline-mode access control + deep links
(deep-debug findings #43 and #44).

#43: UserAccessMiddleware/ForceJoinMiddleware are registered only on the
message/callback_query observers, so banned (and not-force-joined) users
kept full inline access. The handler now enforces both checks itself,
answering with empty results (+ a switch-to-PM button for force-join).

#44: inline deep links hardcoded https://t.me/telegramsellbot — on any
other deployment every shared referral link pointed at a dead/squatted
bot. Links are now built from the real bot username (cached Bot._me).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from apps.bot.handlers.user.inline import inline_query_handler


def _make_sub(username: str = "cfg_a1b2c3d4"):
    return SimpleNamespace(
        id=uuid4(),
        xui_client=SimpleNamespace(username=username),
        plan=None,
        used_bytes=5 * 1024**3,
        volume_bytes=50 * 1024**3,
    )


def _make_bot(username: str = "real_bot", member_status: str = "member"):
    bot = MagicMock()
    bot._me = SimpleNamespace(username=username)
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username=username))
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    return bot


def _make_query(bot=None):
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 12345
    query.answer = AsyncMock()
    query.bot = bot
    return query


def _make_session(subs):
    session = AsyncMock()
    session.flush = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    session.execute = AsyncMock(return_value=result)
    return session


def _gw(enabled: bool = False, channel: str | None = None):
    return SimpleNamespace(force_join_enabled=enabled, force_join_channel=channel)


async def _run(subs=(), user=None, query=None, gw=None):
    query = query or _make_query(bot=_make_bot())
    session = _make_session(list(subs))
    user = user or SimpleNamespace(id=uuid4(), ref_code="ab12cd34", status="active")
    with (
        patch("apps.bot.handlers.user.inline.UserRepository") as repo_cls,
        patch("apps.bot.handlers.user.inline.AppSettingsRepository") as settings_cls,
    ):
        repo_cls.return_value.get_by_telegram_id = AsyncMock(return_value=user)
        settings_cls.return_value.get_gateway_settings = AsyncMock(
            return_value=gw or _gw()
        )
        await inline_query_handler(query, session)
    return query, session


# ---------------------------------------------------------------------------
# Finding #43 — ban / force-join enforcement on the inline observer
# ---------------------------------------------------------------------------


async def test_banned_user_gets_empty_inline_answer():
    """A banned user must get ZERO inline results — no configs, no ref link."""
    banned = SimpleNamespace(id=uuid4(), ref_code="ab12cd34", status="banned")
    query, session = await _run([_make_sub()], user=banned)

    query.answer.assert_awaited_once()
    assert query.answer.await_args.args[0] == []
    # The handler must bail out before even querying the subscriptions.
    session.execute.assert_not_called()


async def test_force_join_non_member_blocked_with_pm_button():
    """Definitive non-member + force-join enabled -> empty results + button."""
    query = _make_query(bot=_make_bot(member_status="left"))
    query, session = await _run(
        [_make_sub()], query=query, gw=_gw(enabled=True, channel="@mychannel")
    )

    query.answer.assert_awaited_once()
    assert query.answer.await_args.args[0] == []
    button = query.answer.await_args.kwargs["button"]
    assert button is not None
    assert button.start_parameter == "force_join"
    session.execute.assert_not_called()


async def test_force_join_member_gets_results():
    """Channel members pass the force-join gate normally."""
    query = _make_query(bot=_make_bot(member_status="member"))
    query, _ = await _run(
        [_make_sub()], query=query, gw=_gw(enabled=True, channel="@mychannel")
    )

    results = query.answer.await_args.args[0]
    assert len(results) == 2  # config card + referral card


async def test_force_join_fails_open_when_membership_check_errors():
    """Mirrors ForceJoinMiddleware: a verification failure must NOT lock the
    whole user base out of inline mode (operator misconfiguration)."""
    bot = _make_bot()
    bot.get_chat_member = AsyncMock(side_effect=Exception("chat not found"))
    query = _make_query(bot=bot)
    query, _ = await _run([], query=query, gw=_gw(enabled=True, channel="@mychannel"))

    results = query.answer.await_args.args[0]
    assert len(results) == 1  # referral card delivered despite the error


async def test_force_join_disabled_is_not_checked():
    """With force-join off, membership must never be queried."""
    bot = _make_bot()
    query = _make_query(bot=bot)
    query, _ = await _run([], query=query, gw=_gw(enabled=False))

    bot.get_chat_member.assert_not_called()
    assert len(query.answer.await_args.args[0]) == 1


# ---------------------------------------------------------------------------
# Finding #44 — deep links built from the real bot username
# ---------------------------------------------------------------------------


async def test_deep_links_use_real_bot_username():
    query = _make_query(bot=_make_bot(username="my_actual_bot"))
    query, _ = await _run([_make_sub()], query=query)

    results = query.answer.await_args.args[0]
    expected = "https://t.me/my_actual_bot?start=ref_ab12cd34"

    # Config card button URL.
    button = results[0].reply_markup.inline_keyboard[0][0]
    assert button.url == expected
    # Referral card message body.
    assert expected in results[1].input_message_content.message_text


async def test_no_hardcoded_username_remains():
    query = _make_query(bot=_make_bot(username="my_actual_bot"))
    query, _ = await _run([_make_sub()], query=query)

    results = query.answer.await_args.args[0]
    button = results[0].reply_markup.inline_keyboard[0][0]
    assert "telegramsellbot" not in button.url
    assert "telegramsellbot" not in results[1].input_message_content.message_text


async def test_username_falls_back_to_get_me_when_cache_empty():
    """Before the first Bot.me() call _me is unset — get_me() must be used."""
    bot = _make_bot(username="late_bot")
    bot._me = None
    query = _make_query(bot=bot)
    query, _ = await _run([], query=query)

    bot.get_me.assert_awaited_once()
    text = query.answer.await_args.args[0][0].input_message_content.message_text
    assert "https://t.me/late_bot?start=ref_ab12cd34" in text
