"""Run the repaired panel scenarios without a production panel or credentials."""
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "tests/test_audit_repairs.py", "tests/test_highfix_xui.py", "tests/test_xui_db_backup.py", "tests/test_xui_runtime.py"], cwd=root))
