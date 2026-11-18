"""哥哥，执行真实前端代码与隔离模拟接口，不冒称浏览器验收。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_vehicle_fuel_ui_vm():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 Node，无法运行前端函数验证")
    result = subprocess.run([node, str(ROOT / "tests/fixtures/vehicle_fuel_ui_vm.cjs")],
                            cwd=ROOT, encoding="utf-8", capture_output=True, check=True)
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    assert proof == {"status": "verified", "cases": 9, "method": "actual_UI_VM_mocked_API_no_browser"}


def test_fuel_script_loads_after_rides():
    page = (ROOT / "world_engine/web/index.html").read_text(encoding="utf-8")
    assert page.index('/ui/rides.js?') < page.index('/ui/vehicle-fuel.js?')
