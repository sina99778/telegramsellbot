"""Queued panel corrections must not be reported as already delivered."""

from apps.bot.handlers.admin.recovery import _format_fixvol_report


def test_report_shows_queued_changes_and_manual_review() -> None:
    report = _format_fixvol_report({
        "checked": 2, "fixed": 1, "no_data": 0, "skipped": 1,
        "details": [
            {"result": "queued", "remark": "queued-service", "old_total": 1024**3,
             "old_used": 512 * 1024**2, "new_total": 512 * 1024**2},
            {"result": "review_required", "remark": "paid-service",
             "reason": "renewal_history_requires_review"},
        ],
    }, applied=True)
    assert "queued-service" in report
    assert "paid-service" in report
    assert "صف" in report
    assert "بررسی" in report
    assert "اعمال شد" not in report
