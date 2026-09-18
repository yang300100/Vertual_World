"""世界元素注册器。

原先是一个 2233 行的单文件模块，现按职责拆成包：

- `models`：枚举、候选 Spec、视图、异常、`Effect`/`HandlerResult`/`RegistrationContext` 与处理器协议
- `handlers`：各类元素的处理器（人物、地点、物品与生活）
- `registry`：`WorldElementRegistry` 统一门面
- `construction`：`ConstructionProjectService` 施工推进
- `agent`：`RegistrationIntentDetector` 与 `RegistrarAgent`

本模块重导出全部公共名字，因此 `from world_engine.registration import ...`
的既有写法保持不变。
"""

from __future__ import annotations

from world_engine.registration.agent import RegistrarAgent, RegistrationIntentDetector
from world_engine.registration.construction import ConstructionProjectService
from world_engine.registration.handlers import (
    ActivityRecipeHandler,
    BuildingHandler,
    CharacterArrivalHandler,
    CharacterBirthHandler,
    CommodityHandler,
    InteriorRoomHandler,
    LoreHandler,
    RoutinePlanHandler,
    SettlementHandler,
    StructureHandler,
    WorkplaceBudgetHandler,
)
from world_engine.registration.models import (
    REGISTRATION_PAYLOAD_ADAPTER,
    BuildingSpec,
    CharacterArrivalSpec,
    CharacterBirthSpec,
    Effect,
    ElementRegistrationSubmit,
    ElementRegistrationView,
    ElementType,
    HandlerResult,
    LoreSpec,
    RegistrarCandidateBatch,
    RegistrationConflict,
    RegistrationContext,
    RegistrationEffectView,
    RegistrationHandler,
    RegistrationNotFound,
    RegistrationPayload,
    RegistrationRejected,
    RegistrationStatus,
    SettlementSpec,
    StructureSpec,
    # 三个私有辅助函数原本也定义在模块顶层；重导出以保持既有访问路径不变。
    _clean_nonnegative_values,
    _entity_exists,
    _require_location,
)
from world_engine.registration.registry import LOGGER, WorldElementRegistry

__all__ = [
    "LOGGER",
    "REGISTRATION_PAYLOAD_ADAPTER",
    "_clean_nonnegative_values",
    "_entity_exists",
    "_require_location",
    "ActivityRecipeHandler",
    "BuildingHandler",
    "BuildingSpec",
    "CharacterArrivalHandler",
    "CharacterArrivalSpec",
    "CharacterBirthHandler",
    "CharacterBirthSpec",
    "CommodityHandler",
    "ConstructionProjectService",
    "Effect",
    "ElementRegistrationSubmit",
    "ElementRegistrationView",
    "ElementType",
    "HandlerResult",
    "InteriorRoomHandler",
    "LoreHandler",
    "LoreSpec",
    "RegistrarAgent",
    "RegistrarCandidateBatch",
    "RegistrationConflict",
    "RegistrationContext",
    "RegistrationEffectView",
    "RegistrationHandler",
    "RegistrationIntentDetector",
    "RegistrationNotFound",
    "RegistrationPayload",
    "RegistrationRejected",
    "RegistrationStatus",
    "RoutinePlanHandler",
    "SettlementHandler",
    "SettlementSpec",
    "StructureHandler",
    "StructureSpec",
    "WorkplaceBudgetHandler",
    "WorldElementRegistry",
]
