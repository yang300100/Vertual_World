"""哥哥，不支持的控制参数必须反馈，不能冒充完成指定条件的实验。"""

import pytest

from world_engine.collective_cooperation import WorkTask
from world_engine.collective_types import CollectiveCommand


@pytest.mark.parametrize("op", ["experiment", "observe", "inspect", "wait"])
@pytest.mark.parametrize("settings", [{"depth_from_surface_cm": 20}, {"depth_fraction": 0.95}])
def test_nonprocessing_command_rejects_ignored_settings(op, settings):
    with pytest.raises(ValueError, match="settings"):
        CollectiveCommand.model_validate({"op": op, "settings": settings})


def test_nonprocessing_command_cannot_hide_settings_behind_unused_operation():
    with pytest.raises(ValueError, match="settings"):
        CollectiveCommand.model_validate(
            {"op": "wait", "operation": "heat", "settings": {"temperature": 50}}
        )


def test_material_processing_keeps_supported_settings():
    command = CollectiveCommand.model_validate(
        {"op": "experiment", "operation": "heat", "settings": {"temperature": 50}}
    )
    assert command.settings == {"temperature": 50}


@pytest.mark.parametrize("settings", [{"depth_from_surface_cm": 20}, {"depth_fraction": 0.95}])
def test_cooperative_assay_rejects_ignored_settings(settings):
    with pytest.raises(ValueError, match="settings"):
        WorkTask.model_validate(
            {
                "key": "measure",
                "worker_id": "worker",
                "operation": "assay",
                "batch_id": "batch",
                "quantity": 1,
                "settings": settings,
            }
        )


def test_cooperative_material_processing_keeps_supported_settings():
    task = WorkTask.model_validate(
        {
            "key": "heat",
            "worker_id": "worker",
            "operation": "heat",
            "batch_id": "batch",
            "quantity": 1,
            "settings": {"temperature": 50},
        }
    )
    assert task.settings == {"temperature": 50}
