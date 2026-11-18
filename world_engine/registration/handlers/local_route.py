"""哥哥，局部道路只有明确核实并确认后才会进入通行记录。"""

from world_engine.local_routes import LocalRouteConflict, LocalRouteService
from world_engine.registration.models import (
    Effect,
    ElementType,
    HandlerResult,
    LocalRouteSpec,
    RegistrationRejected,
    RegistrationStatus,
)


class LocalRouteHandler:
    element_type = ElementType.LOCAL_ROUTE

    def apply(self, context, payload):
        if not isinstance(payload, LocalRouteSpec):
            raise TypeError("局部通路处理器收到错误的数据类型")
        try:
            route_id = LocalRouteService.register(context, payload)
        except LocalRouteConflict as exc:
            raise RegistrationRejected(str(exc)) from exc
        return HandlerResult(
            status=RegistrationStatus.APPLIED,
            summary=f"既存局部通路“{payload.name}”已核实登记。",
            entity_type="local_route", entity_id=route_id, entity_name=payload.name,
            location_id=payload.location_id,
            effects=[Effect(
                effect_type="local_route.registered", entity_type="local_route", entity_id=route_id,
                payload={"dataset_id": payload.dataset_id, "location_id": payload.location_id,
                         "revision": 1, "is_open": True},
            )],
        )
