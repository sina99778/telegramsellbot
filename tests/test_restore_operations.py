"""Exercise restore sequencing with a command stub; no real Docker is used."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")


def run_restore(tmp_path, *, invalid=False, switch_database=False):
    shutil.copy(ROOT / "restore.sh", tmp_path)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/ops-common.sh", tmp_path / "scripts")
    (tmp_path / ".env").write_text("POSTGRES_USER=fixture\nPOSTGRES_DB=fixture\n")
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    (bin_path / "flock").write_text("#!/bin/bash\nexit 0\n", newline="\n")
    docker = bin_path / "docker"
    docker.write_text(
        r"""#!/bin/bash
echo "$*" >> "$RESTORE_LOG"
if [[ "$1" == ps ]]; then echo telegramsellbot-postgres; exit 0; fi
if [[ "$1" == inspect ]]; then echo true; exit 0; fi
if [[ "$*" == *pg_dump* ]]; then echo '-- PostgreSQL database dump'; exit 0; fi
if [[ "$*" == *'-tAc'* ]]; then echo 1; exit 0; fi
if [[ "$*" == *'--single-transaction'* ]]; then
  sql="$(cat)"
  echo "$sql" >> "$RESTORE_LOG"
  [[ "$sql" != *BROKEN* ]] || exit 1
elif [[ "$*" == *'exec -i'* && "$*" == *psql* ]]; then
  cat >> "$RESTORE_LOG"
fi
exit 0
""",
        newline="\n",
    )
    for tool in (docker, bin_path / "flock"):
        tool.chmod(0o755)
    dump = "-- PostgreSQL database dump\nCREATE TABLE fixture(id int);\n"
    if invalid:
        dump += "BROKEN STATEMENT;\n"
    if switch_database:
        dump += "\\connect fixture\n"
    (tmp_path / "dump.sql").write_text(dump, newline="\n")
    # Use Bash's own path conversion so Git Bash/MSYS gets a POSIX PATH.
    result = subprocess.run(
        [
            BASH,
            "--noprofile",
            "--norc",
            "-c",
            'export PATH="$PWD/bin:$PATH"; export TSB_LOCK_FILE="$PWD/lock"; '
            'export RESTORE_LOG="$PWD/commands.log"; '
            'printf "OVERWRITE\\n" | bash restore.sh dump.sql',
        ],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=25,
    )
    return result, (tmp_path / "commands.log").read_text()


def test_invalid_sql_never_stops_apps_or_renames_target(tmp_path):
    result, log = run_restore(tmp_path, invalid=True)
    assert result.returncode != 0
    assert "--single-transaction" in log
    assert "stop telegramsellbot" not in log and "RENAME TO" not in log
    assert 'DROP DATABASE IF EXISTS "fixture"' not in log


def test_success_keeps_old_database_and_takes_fresh_snapshot_before_cutover(tmp_path):
    result, log = run_restore(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        log.index("--single-transaction") < log.index("pg_dump") < log.index("stop telegramsellbot")
    )
    assert 'ALTER DATABASE "fixture" RENAME TO "tsb_before_restore_' in log
    assert 'DROP DATABASE IF EXISTS "fixture"' not in log
    assert "BEGIN;" in log and "COMMIT;" in log
    assert list((tmp_path / "backups").glob("pre_restore_*.sql.gz"))


def test_database_switching_dump_rejected_before_shadow_restore(tmp_path):
    result, log = run_restore(tmp_path, switch_database=True)
    assert result.returncode != 0
    assert "CREATE DATABASE" not in log and "stop telegramsellbot" not in log
