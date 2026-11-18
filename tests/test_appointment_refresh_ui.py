"""哥哥，只用模拟接口验证实际前端脚本，不冒称真实浏览器。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_appointment_refresh_actual_frontend_vm():
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 Node，无法验证约定前端脚本")
    run = subprocess.run([node, str(ROOT / "tests/fixtures/appointment_refresh_vm.cjs")],
                         cwd=ROOT, encoding="utf-8", capture_output=True, check=True)
    assert json.loads(run.stdout.strip()) == {"status": "verified", "cases": 11,
                                               "method": "actual_UI_VM_mocked_API_no_browser"}


def test_appointment_script_cache_version_updated():
    page = (ROOT / "world_engine/web/index.html").read_text(encoding="utf-8")
    assert '/ui/appointments.js?v=20261103-person-refresh2' in page
