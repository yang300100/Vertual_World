"""世界元素处理器子包；按元素类别分组。"""

from __future__ import annotations

from world_engine.registration.handlers.character import (
    CharacterArrivalHandler,
    CharacterBirthHandler,
)
from world_engine.registration.handlers.items import (
    ActivityRecipeHandler,
    CommodityHandler,
    InteriorRoomHandler,
    RoutinePlanHandler,
    WorkplaceBudgetHandler,
)
from world_engine.registration.handlers.place import (
    BuildingHandler,
    LoreHandler,
    SettlementHandler,
    StructureHandler,
)
from world_engine.registration.handlers.vehicle import VehicleHandler
from world_engine.registration.handlers.vehicle_fuel import VehicleFuelProfileHandler
from world_engine.registration.handlers.local_route import LocalRouteHandler

__all__ = [
    "ActivityRecipeHandler",
    "BuildingHandler",
    "CharacterArrivalHandler",
    "CharacterBirthHandler",
    "CommodityHandler",
    "InteriorRoomHandler",
    "LoreHandler",
    "RoutinePlanHandler",
    "SettlementHandler",
    "StructureHandler",
    "WorkplaceBudgetHandler",
    "VehicleHandler",
    "VehicleFuelProfileHandler",
    "LocalRouteHandler",
]
