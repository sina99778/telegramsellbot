"""Safe static-file resolution for the dashboard SPA catch-all route.

Kept in its own tiny module (no FastAPI imports) so the containment logic is
unit-testable without constructing the whole API app.
"""
from __future__ import annotations

import pathlib


def resolve_dashboard_file(base_dir: pathlib.Path, path: str) -> pathlib.Path | None:
    """Resolve a user-supplied sub-path to a real file strictly inside base_dir.

    SECURITY: `path` is attacker-controlled and ``Path /`` does not normalize
    ``..`` — without full resolution + containment, a request like
    ``/dashboard/..%2f..%2f.env`` (decoded to ``../../.env``) would serve the
    app's .env with every secret in it. Returns None unless the fully-resolved
    candidate is an existing regular file contained in base_dir.
    """
    try:
        candidate = (base_dir / path).resolve()
    except (OSError, ValueError):
        return None
    try:
        if candidate.is_relative_to(base_dir.resolve()) and candidate.is_file():
            return candidate
    except OSError:
        return None
    return None
