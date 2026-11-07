"""Reject new Ruff/mypy findings while tracking explicitly recorded existing debt."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = (
    "apps",
    "core",
    "models",
    "repositories",
    "schemas",
    "services",
    "scripts/check_quality.py",
    "tests/test_quality_gate.py",
)
VERSIONS = {"ruff": "0.17.4", "mypy": "1.20.2"}


@dataclass(frozen=True)
class Diagnostic:
    path: str
    code: str
    message: str
    source: str

    @property
    def key(self) -> str:
        source_hash = hashlib.sha256(self.source.encode("utf-8")).hexdigest()
        return json.dumps([self.path, self.code, self.message, source_hash], ensure_ascii=False)


@dataclass(frozen=True)
class Comparison:
    new: dict[str, int]
    resolved: dict[str, int]


def diagnostic_counts(diagnostics: list[Diagnostic]) -> dict[str, int]:
    return dict(Counter(item.key for item in diagnostics))


def compare_diagnostics(current: dict[str, int], baseline: dict[str, int]) -> Comparison:
    return Comparison(
        dict(Counter(current) - Counter(baseline)), dict(Counter(baseline) - Counter(current))
    )


def parse_tool_output(
    tool: str,
    output: str,
    stderr: str,
    returncode: int,
    root: Path,
) -> list[Diagnostic]:
    if tool not in VERSIONS or returncode not in {0, 1} or stderr.strip():
        raise ValueError(f"{tool} failed (exit {returncode}): {stderr.strip()}")
    try:
        records = (
            json.loads(output)
            if tool == "ruff"
            else [json.loads(line) for line in output.splitlines() if line.strip()]
        )
        if not isinstance(records, list):
            raise ValueError("Expected diagnostic list")
        result = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid diagnostic")
            if tool == "mypy":
                severity = record["severity"]
                if severity == "note":
                    if (
                        not isinstance(record["file"], str)
                        or not isinstance(record["message"], str)
                        or type(record["line"]) is not int
                    ):
                        raise ValueError("Invalid diagnostic note")
                    continue
                if severity != "error":
                    raise ValueError("Unknown diagnostic severity")
            file = record["filename" if tool == "ruff" else "file"]
            row = record["location"]["row"] if tool == "ruff" else record["line"]
            code, message = record["code"], record["message"]
            if not all(isinstance(value, str) and value for value in (file, code, message)):
                raise ValueError("Missing diagnostic identity")
            if type(row) is not int or row < 1:
                raise ValueError("Invalid diagnostic location")
            path = (root / file).resolve()
            relative = path.relative_to(root.resolve()).as_posix()
            lines = path.read_text(encoding="utf-8-sig").splitlines()
            result.append(Diagnostic(relative, code, message, lines[row - 1]))
        if (returncode == 1) != bool(result):
            raise ValueError("Exit status contradicts diagnostics")
        return result
    except (KeyError, IndexError, TypeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid {tool} diagnostic output: {exc}") from exc


def load_baseline(path: Path, tool: str) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data["tool"] != tool
        or data["version"] != VERSIONS[tool]
        or data["scope"] != list(SCOPE)
        or data["schema"] != 1
    ):
        raise ValueError("Baseline metadata does not match gate")
    counts = data["diagnostics"]
    if not isinstance(counts, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 1
        for key, value in counts.items()
    ):
        raise ValueError("Invalid baseline counts")
    for key in counts:
        fingerprint = json.loads(key)
        if (
            not isinstance(fingerprint, list)
            or len(fingerprint) != 4
            or not all(isinstance(value, str) and value for value in fingerprint)
        ):
            raise ValueError("Invalid baseline fingerprint")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=VERSIONS)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    tool = args.tool
    try:
        version = subprocess.run(
            [sys.executable, "-m", tool, "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        if version.stdout.split()[1] != VERSIONS[tool]:
            raise ValueError(f"Install {tool}=={VERSIONS[tool]} before running the gate")
        command = [sys.executable, "-m", tool]
        command += (
            ["check", "--output-format", "json"]
            if tool == "ruff"
            else ["--output", "json", "--explicit-package-bases"]
        )
        process = subprocess.run(
            command + list(SCOPE),
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        diagnostics = parse_tool_output(
            tool, process.stdout, process.stderr, process.returncode, ROOT
        )
        counts = diagnostic_counts(diagnostics)
        baseline = ROOT / "quality" / f"{tool}-baseline.json"
        if args.write_baseline:
            own = [item for item in diagnostics if item.path in SCOPE[-2:]]
            if own:
                raise ValueError("Gate and gate tests must be clean before recording debt")
            baseline.parent.mkdir(exist_ok=True)
            baseline.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "tool": tool,
                        "version": VERSIONS[tool],
                        "scope": list(SCOPE),
                        "diagnostics": counts,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"Recorded {len(diagnostics)} existing {tool} diagnostics in {baseline.name}")
            return 0
        comparison = compare_diagnostics(counts, load_baseline(baseline, tool))
        for key, count in comparison.new.items():
            path, code, message, _ = json.loads(key)
            print(f"NEW ({count}): {path}: {code}: {message}")
        print(
            f"{tool}: {len(diagnostics)} existing findings; "
            f"{sum(comparison.new.values())} new; {sum(comparison.resolved.values())} resolved"
        )
        return 1 if comparison.new else 0
    except (ValueError, KeyError, IndexError, OSError, subprocess.SubprocessError) as exc:
        print(f"Quality gate failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
