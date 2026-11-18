"""哥哥，通路表单的重试与世界切换须保留正确的请求。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_actual_local_route_ui_script():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 Node，无法执行界面脚本")
    result = subprocess.run(
        [node, str(root / "tests/fixtures/local_routes_ui.cjs")],
        cwd=root, encoding="utf-8", capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == {
        "status": "verified", "cases": 8, "method": "actual_UI_VM_mocked_API",
    }
