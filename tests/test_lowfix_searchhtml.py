"""
Regression tests for low finding #75 in
apps/bot/handlers/user/my_configs.py.

#75 — my_configs_search_handler echoes the user's raw search query into
messages sent with the bot's default parse_mode=HTML. A query containing
'<' (e.g. "<test>") made Telegram reject the send with "can't parse
entities", so the user got a generic error instead of search results.
The echoed query must be html.escape()d in BOTH the no-results and the
results messages, while the DB search keeps using the raw text.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from apps.bot.handlers.user.my_configs import my_configs_search_handler


@pytest.fixture
def message():
    msg = MagicMock()
    msg.text = "<Test>"
    msg.from_user = MagicMock()
    msg.from_user.id = 12345
    msg.answer = AsyncMock()
    return msg


@pytest.fixture
def state():
    st = MagicMock()
    st.clear = AsyncMock()
    return st


@pytest.fixture
def user():
    u = MagicMock()
    u.id = uuid4()
    return u


def _make_sub():
    sub = MagicMock()
    sub.id = uuid4()
    return sub


async def _run_search(message, state, user, mock_session, subs):
    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    mock_session.execute = AsyncMock(return_value=result)

    with patch("apps.bot.handlers.user.my_configs.UserRepository") as repo_cls, \
         patch(
             "apps.bot.handlers.user.my_configs._build_config_button_label",
             return_value="label",
         ):
        repo_cls.return_value.get_by_telegram_id = AsyncMock(return_value=user)
        await my_configs_search_handler(message, state, mock_session)

    message.answer.assert_awaited_once()
    return message.answer.await_args.args[0]


async def test_no_results_message_escapes_html(message, state, user, mock_session):
    """No-results reply must contain the escaped query, never a raw '<'."""
    text = await _run_search(message, state, user, mock_session, subs=[])

    assert "&lt;test&gt;" in text
    assert "<test>" not in text


async def test_results_message_escapes_html(message, state, user, mock_session):
    """Results header must contain the escaped query, never a raw '<'."""
    text = await _run_search(
        message, state, user, mock_session, subs=[_make_sub()]
    )

    assert "&lt;test&gt;" in text
    assert "<test>" not in text


async def test_db_search_still_uses_raw_query(message, state, user, mock_session):
    """Escaping is display-only: the SQL contains() filter must keep the
    raw (lowercased) text, not the &lt;...&gt; entity form."""
    await _run_search(message, state, user, mock_session, subs=[])

    stmt = mock_session.execute.await_args.args[0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    assert "<test>" in str(compiled)
    assert "&lt;" not in str(compiled)
