from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_windows_one_click_launchers_are_ascii_without_bom() -> None:
    launcher_paths = [
        PROJECT_ROOT / "启动虚拟世界.cmd",
        PROJECT_ROOT / "停止虚拟世界.cmd",
    ]
    for path in launcher_paths:
        content = path.read_bytes()
        assert not content.startswith(b"\xef\xbb\xbf")
        assert all(byte < 128 for byte in content)
        assert b"powershell.exe -NoProfile -ExecutionPolicy Bypass" in content


def test_start_and_stop_scripts_keep_process_scope_narrow() -> None:
    start_script = (PROJECT_ROOT / "scripts" / "start_world.ps1").read_text(
        encoding="utf-8"
    )
    stop_script = (PROJECT_ROOT / "scripts" / "stop_world.ps1").read_text(
        encoding="utf-8"
    )

    assert "virtual-world-core" in start_script
    assert "world_engine.api:app" in start_script
    assert "world_engine.worker" in start_script
    assert "-WindowStyle Hidden" in start_script
    assert "StartsWith" in stop_script
    assert "world_engine.api:app" in stop_script
    assert "world_engine.worker" in stop_script
