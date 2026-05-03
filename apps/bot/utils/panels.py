from __future__ import annotations

from collections.abc import Iterable
from html import escape


def status_label(enabled: bool) -> str:
    return "فعال" if enabled else "غیرفعال"


def admin_panel(title: str, sections: Iterable[tuple[str, Iterable[tuple[str, object]]]]) -> str:
    blocks = [f"<b>{escape(title)}</b>"]
    for heading, rows in sections:
        row_lines = [
            f"• {escape(str(label))}: <code>{escape(str(value))}</code>"
            for label, value in rows
        ]
        if not row_lines:
            continue
        blocks.append(f"<b>{escape(heading)}</b>\n" + "\n".join(row_lines))
    return "\n\n".join(blocks)
