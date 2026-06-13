"""The backup bundle must ALWAYS carry a restore-ready .env.

docker-compose `env_file:` injects variables but does not mount the file, so
inside the worker container `_read_env_file()` used to find nothing and the
bundle shipped with the "no APP_SECRET_KEY" warning. Two-layer fix: the
compose file now mounts ./.env read-only, and the job falls back to
regenerating an .env from the live settings when no file exists.
"""
from __future__ import annotations

import json
import tarfile
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_reconstructed_env_contains_the_critical_keys():
    from apps.worker.jobs.backup import _reconstruct_env_from_settings

    text = _reconstruct_env_from_settings().decode("utf-8")

    # The keys a restore cannot live without — secrets must be REVEALED,
    # not pydantic's masked '**********'.
    assert "APP_SECRET_KEY=" in text
    assert "BOT_TOKEN=" in text
    assert "DATABASE_URL=" in text
    assert "**********" not in text
    # dotenv shape: comment header + KEY=value lines.
    assert text.startswith("#")
    for line in text.splitlines():
        assert line.startswith("#") or "=" in line


def test_reconstructed_env_serializes_bools_as_lowercase():
    from apps.worker.jobs.backup import _reconstruct_env_from_settings

    text = _reconstruct_env_from_settings().decode("utf-8")
    assert "=True" not in text and "=False" not in text  # bools become true/false


def test_bundle_manifest_records_env_reconstructed():
    from apps.worker.jobs.backup import _build_bundle

    bundle = _build_bundle(
        pg_dump_bytes=b"SQL",
        env_bytes=b"X=1\n",
        env_reconstructed=True,
        ready_configs=None,
        xui_dumps=[],
        git_sha="abc",
        git_branch="master",
        hostname="h",
    )
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        names = tar.getnames()
        assert "env" in names
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["contents"]["env"] is True
    assert manifest["contents"]["env_reconstructed"] is True


@pytest.mark.asyncio
async def test_run_backup_falls_back_to_reconstruction(mock_session):
    """When no .env file exists in the container, the bundle still gets one."""
    import apps.worker.jobs.backup as b

    captured: dict = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return b"BUNDLE"

    bot = MagicMock()
    bot.send_document = AsyncMock()
    bot.send_message = AsyncMock()

    with patch.object(b, "_dump_postgres", AsyncMock(return_value=b"SQL")), \
         patch.object(b, "_read_env_file", return_value=None), \
         patch.object(b, "_reconstruct_env_from_settings", return_value=b"APP_SECRET_KEY=k\n"), \
         patch.object(b, "_read_ready_configs_dir", return_value=None), \
         patch.object(b, "_dump_xui_databases", AsyncMock(return_value=[])), \
         patch.object(b, "_build_bundle", side_effect=fake_build), \
         patch.object(b, "AppSettingsRepository", return_value=MagicMock()):
        # manual_requester_id set → interval gate skipped, single DM target.
        await b.run_backup(mock_session, bot, manual_requester_id=777)

    assert captured["env_bytes"] == b"APP_SECRET_KEY=k\n"
    assert captured["env_reconstructed"] is True
    bot.send_document.assert_awaited_once()
    # The caption must say the env IS included (the reconstructed variant).
    caption = bot.send_document.call_args.kwargs.get("caption") or bot.send_document.call_args[0][2]
    assert ".env" in caption and "بازسازی" in caption
