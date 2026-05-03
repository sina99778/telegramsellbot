import json

import pytest
from aiogram.methods import SendMessage

from services.telegram.premium_emoji import (
    PremiumEmojiRuntimeSettings,
    apply_premium_emoji_to_method,
    parse_emoji_map_text,
    render_premium_emoji_html,
)


def test_render_premium_emoji_replaces_semantic_keys():
    rendered = render_premium_emoji_html("✅ Done 🚀", {"success": "5368324170671202286", "rocket": "5368324170671202287"})

    assert '<tg-emoji emoji-id="5368324170671202286">✅</tg-emoji>' in rendered
    assert '<tg-emoji emoji-id="5368324170671202287">🚀</tg-emoji>' in rendered


def test_render_premium_emoji_skips_code_and_existing_tags():
    rendered = render_premium_emoji_html(
        "✅ <code>✅</code> <tg-emoji emoji-id=\"old\">🚀</tg-emoji>",
        {"success": "5368324170671202286", "rocket": "5368324170671202287"},
    )

    assert rendered.count('emoji-id="5368324170671202286"') == 1
    assert "<code>✅</code>" in rendered
    assert '<tg-emoji emoji-id="old">🚀</tg-emoji>' in rendered


def test_parse_emoji_map_accepts_json_and_lines():
    assert parse_emoji_map_text(json.dumps({"success": "5368324170671202286"})) == {
        "success": "5368324170671202286",
    }
    assert parse_emoji_map_text("success=5368324170671202286\n🚀:5368324170671202287") == {
        "success": "5368324170671202286",
        "🚀": "5368324170671202287",
    }


@pytest.mark.asyncio
async def test_apply_premium_emoji_uses_default_html_parse_mode(monkeypatch):
    async def fake_runtime_settings():
        return PremiumEmojiRuntimeSettings(enabled=True, emoji_map={"success": "5368324170671202286"})

    monkeypatch.setattr(
        "services.telegram.premium_emoji.get_runtime_premium_emoji_settings",
        fake_runtime_settings,
    )
    method = SendMessage(chat_id=1, text="✅ Done")

    await apply_premium_emoji_to_method(method, default_parse_mode="HTML")

    assert method.text == '<tg-emoji emoji-id="5368324170671202286">✅</tg-emoji> Done'


@pytest.mark.asyncio
async def test_apply_premium_emoji_respects_explicit_parse_mode_none(monkeypatch):
    async def fake_runtime_settings():
        return PremiumEmojiRuntimeSettings(enabled=True, emoji_map={"success": "5368324170671202286"})

    monkeypatch.setattr(
        "services.telegram.premium_emoji.get_runtime_premium_emoji_settings",
        fake_runtime_settings,
    )
    method = SendMessage(chat_id=1, text="✅ Done", parse_mode=None)

    await apply_premium_emoji_to_method(method, default_parse_mode="HTML")

    assert method.text == "✅ Done"
