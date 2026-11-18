"""哥哥，用真实前端脚本和模拟接口验证跨层页面。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_cross_layer_ui_vm():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 Node，无法验证前端脚本")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/fixtures/cross_layer_ui_vm.cjs")],
                            cwd=root, capture_output=True, encoding="utf-8", check=True)
    assert json.loads(result.stdout.strip()) == {"status": "verified", "cases": 8,
                                                  "method": "actual_UI_VM_mocked_API_no_browser"}


def test_cross_layer_script_after_living():
    root = Path(__file__).resolve().parents[1]
    page = (root / "world_engine/web/index.html").read_text(encoding="utf-8")
    assert page.index('/ui/living.js?') < page.index('/ui/cross-layer.js?')
