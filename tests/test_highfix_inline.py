"""Regression tests for the inline-mode fix (deep-debug finding #12).

Config names containing '_' (guaranteed for marzban-family `{base}_{hex}`
usernames and `trial_{telegram_id}` configs) used to be interpolated raw into
a parse_mode="Markdown" message; an unpaired '_' made Telegram reject the
whole answerInlineQuery call with 400, so the user got ZERO inline results.
The fix switches to parse_mode="HTML" with html.escape() and adds a
try/except backstop around inline_query.answer.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from apps.bot.handlers.user.inline import inline_query_handler


def _make_sub(username: str):
    return SimpleNamespace(
        id=uuid4(),
        xui_client=SimpleNamespace(username=username),
        plan=None,
        used_bytes=5 * 1024**3,
        volume_bytes=50 * 1024**3,
    )


def _make_query():
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = 12345
    query.answer = AsyncMock()
    return query


def _make_session(subs):
    session = AsyncMock()
    session.flush = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    session.execute = AsyncMock(return_value=result)
    return session


async def _run(subs, query=None, user=None):
    query = query or _make_query()
    session = _make_session(subs)
    user = user or SimpleNamespace(id=uuid4(), ref_code="ab12cd34")
    with patch("apps.bot.handlers.user.inline.UserRepository") as repo_cls:
        repo_cls.return_value.get_by_telegram_id = AsyncMock(return_value=user)
        await inline_query_handler(query, session)
    return query


async def test_underscore_config_name_round_trips():
    """Names with '_' must survive intact and be sent as HTML, not Markdown."""
    query = await _run([_make_sub("trial_123456789")])

    query.answer.assert_awaited_once()
    results = query.answer.await_args.args[0]
    content = results[0].input_message_content
    assert content.parse_mode == "HTML"
    assert "trial_123456789" in content.message_text
    # No legacy-Markdown bold markers left over.
    assert "**" not in content.message_text


async def test_marzban_style_name_with_multiple_underscores():
    query = await _run([_make_sub("user_name_a1b2c3d4")])

    results = query.answer.await_args.args[0]
    assert "user_name_a1b2c3d4" in results[0].input_message_content.message_text


async def test_html_metacharacters_are_escaped():
    """User-chosen names must not be able to inject HTML entities."""
    query = await _run([_make_sub("a<b>&_name")])

    text = query.answer.await_args.args[0][0].input_message_content.message_text
    assert "a&lt;b&gt;&amp;_name" in text
    assert "a<b>&_name" not in text


async def test_referral_card_uses_html_parse_mode():
    query = await _run([])

    results = query.answer.await_args.args[0]
    assert len(results) == 1  # referral card only
    content = results[0].input_message_content
    assert content.parse_mode == "HTML"
    assert "ref_ab12cd34" in content.message_text
    assert "**" not in content.message_text


async def test_answer_failure_is_swallowed():
    """The backstop must keep a Telegram error from propagating unhandled."""
    query = _make_query()
    query.answer = AsyncMock(side_effect=Exception("Bad Request: can't parse entities"))

    # Must not raise.
    await _run([_make_sub("trial_1")], query=query)
    query.answer.assert_awaited_once()
