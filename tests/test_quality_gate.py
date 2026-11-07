"""The quality gate may allow recorded debt, but must reject new diagnostics."""

import json
from pathlib import Path

import pytest

from scripts.check_quality import (
    Diagnostic,
    compare_diagnostics,
    diagnostic_counts,
    parse_tool_output,
)


def test_new_diagnostic_is_rejected_even_when_old_debt_was_removed() -> None:
    old = Diagnostic("core/a.py", "F401", "unused import", "import old")
    new = Diagnostic("core/a.py", "F401", "unused import", "import new")
    result = compare_diagnostics(diagnostic_counts([new]), diagnostic_counts([old]))
    assert result.new == {new.key: 1}
    assert result.resolved == {old.key: 1}


def test_another_identical_diagnostic_cannot_hide_behind_existing_debt() -> None:
    item = Diagnostic("core/a.py", "F401", "unused import", "import old")
    result = compare_diagnostics(diagnostic_counts([item, item]), diagnostic_counts([item]))
    assert result.new == {item.key: 1}


def test_line_number_movement_does_not_expand_the_baseline() -> None:
    # Line numbers are intentionally not part of a Diagnostic. The exact
    # message, code, relative path and source still bind the recorded debt.
    item = Diagnostic("core/a.py", "F401", "unused import", "import old")
    assert not compare_diagnostics(diagnostic_counts([item]), diagnostic_counts([item])).new


def test_no_current_diagnostics_reports_all_recorded_debt_resolved() -> None:
    item = Diagnostic("core/a.py", "F401", "unused import", "import old")
    assert compare_diagnostics({}, diagnostic_counts([item])).resolved == {item.key: 1}


@pytest.mark.parametrize(
    "tool,output,code",
    [
        ("ruff", "not JSON", 1),
        ("ruff", "[]", 2),
        ("ruff", "[]", 1),
        ("ruff", "[{}]", 1),
        ("mypy", "not JSON", 1),
        ("mypy", "", 2),
        ("mypy", "", 1),
        ("mypy", "{}", 1),
    ],
)
def test_invalid_tool_results_fail_closed(
    tmp_path: Path, tool: str, output: str, code: int
) -> None:
    with pytest.raises(ValueError):
        parse_tool_output(tool, output, "", code, tmp_path)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_tool_stderr_fails_closed(tmp_path: Path, tool: str) -> None:
    with pytest.raises(ValueError):
        parse_tool_output(tool, "[]" if tool == "ruff" else "", "tool failed", 0, tmp_path)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_parsed_diagnostic_uses_source_line_and_relative_path(tmp_path: Path, tool: str) -> None:
    source = tmp_path / "core" / "a.py"
    source.parent.mkdir()
    source.write_text("\nimport old\n", encoding="utf-8")
    if tool == "ruff":
        output = json.dumps(
            [
                {
                    "filename": str(source),
                    "code": "F401",
                    "message": "unused import",
                    "location": {"row": 2, "column": 1},
                }
            ]
        )
    else:
        output = json.dumps(
            {
                "file": str(source),
                "line": 2,
                "column": 1,
                "code": "F401",
                "message": "unused import",
                "severity": "error",
            }
        )
    assert parse_tool_output(tool, output, "", 1, tmp_path) == [
        Diagnostic("core/a.py", "F401", "unused import", "import old")
    ]


@pytest.mark.parametrize("tool,output", [("ruff", "[]"), ("mypy", "")])
def test_clean_tool_results_are_accepted(tmp_path: Path, tool: str, output: str) -> None:
    assert parse_tool_output(tool, output, "", 0, tmp_path) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": 1, "tool": "ruff", "version": "0.17.4", "scope": [], "diagnostics": {}},
        {"schema": 1, "tool": "ruff", "version": "0.17.4", "diagnostics": {}},
    ],
)
def test_invalid_baseline_metadata_fails_closed(tmp_path: Path, payload: dict[str, object]) -> None:
    from scripts.check_quality import load_baseline

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        load_baseline(baseline, "ruff")


def test_note_only_output_cannot_mask_mypy_error_exit(tmp_path: Path) -> None:
    output = json.dumps({"severity": "note", "message": "incomplete analysis"})
    with pytest.raises(ValueError):
        parse_tool_output("mypy", output, "", 1, tmp_path)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_diagnostic_outside_workspace_fails_closed(tmp_path: Path, tool: str) -> None:
    if tool == "ruff":
        output = json.dumps(
            [
                {
                    "filename": "../outside.py",
                    "code": "F401",
                    "message": "unused",
                    "location": {"row": 1},
                }
            ]
        )
    else:
        output = json.dumps(
            {
                "file": "../outside.py",
                "line": 1,
                "code": "unused",
                "message": "unused",
                "severity": "error",
            }
        )
    with pytest.raises(ValueError):
        parse_tool_output(tool, output, "", 1, tmp_path)


def test_malformed_note_record_cannot_be_silently_accepted(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        parse_tool_output("mypy", '{"severity":"note"}', "", 0, tmp_path)
