"""世界元素登记、移除与建筑工程的接口。

这些路由原先内嵌在 `api.create_app` 里。这里按「元素生命周期」搬出，
行为与之完全一致（路径、方法、响应模型、状态码与错误文案一字未改）。

登记与移除都遵循同一套形态：提交时把领域异常映射成 404/409，把 sqlite
的并发写冲突映射成 503；列表则先确认世界存在，再交给服务层。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from world_engine.registration import (
    ConstructionProjectService,
    ElementRegistrationSubmit,
    ElementRegistrationView,
    ElementType,
    RegistrationConflict,
    RegistrationNotFound,
    RegistrationStatus,
    WorldElementRegistry,
)
from world_engine.removal import (
    ElementRemovalConflict,
    ElementRemovalNotFound,
    ElementRemovalSubmit,
    ElementRemovalView,
    WorldElementRemover,
)
from world_engine.local_routes import LocalRouteConflict, LocalRouteNotFound, LocalRouteService
from world_engine.repository import to_iso


class ConstructionProjectStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["surveyed", "constructing", "cancelled"]


class RegistrationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class LocalRouteStateRequest(BaseModel):
    """哥哥，编年者的状态确认仍须绑定真实来源和当前版本。"""

    model_config = ConfigDict(extra="forbid")

    is_open: bool = Field(strict=True)
    expected_state_version: int = Field(ge=1, strict=True)
    source_event_id: str = Field(min_length=1, max_length=100)
    requested_by_character_id: str | None = None


def build_elements_router(database) -> APIRouter:
    """世界元素登记、移除与建筑工程的接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["世界元素"])

    element_registry = WorldElementRegistry()
    element_remover = WorldElementRemover()
    construction_projects = ConstructionProjectService()

    @router.get("/local-routes/options")
    def local_route_options(world_id: str):
        with database.read() as connection:
            try:
                at = LocalRouteService._world(connection, world_id)
            except LocalRouteNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            datasets = [dict(row) for row in connection.execute(
                "SELECT id,name FROM navigation_datasets WHERE world_id=? AND review_status='approved' "
                "ORDER BY created_at DESC,id", (world_id,),
            )]
            locations = {row["id"]: {**dict(row), "sources": []} for row in connection.execute(
                "SELECT id,name,space_layer FROM locations WHERE world_id=? AND is_active=1 "
                "AND space_layer='ground' ORDER BY name,id", (world_id,),
            )}
            events = connection.execute(
                "SELECT e.id,e.location_id,e.occurred_at,e.summary FROM world_events e "
                "JOIN locations l ON l.id=e.location_id AND l.world_id=e.world_id "
                "WHERE e.world_id=? AND e.occurred_at<=? AND l.is_active=1 AND l.space_layer='ground' "
                "ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT 1000", (world_id, to_iso(at)),
            ).fetchall()
            for event in events:
                try:
                    LocalRouteService.source_time(event["occurred_at"], at)
                except LocalRouteConflict:
                    continue
                locations[event["location_id"]]["sources"].append({
                    "id": event["id"], "occurred_at": event["occurred_at"], "summary": event["summary"],
                })
            return {"datasets": datasets, "locations": list(locations.values()), "world_time": to_iso(at)}

    @router.get("/local-routes")
    def local_routes(world_id: str):
        with database.read() as connection:
            try:
                return LocalRouteService.rows(connection, world_id)
            except LocalRouteNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/local-routes/{route_id}")
    def local_route(world_id: str, route_id: str):
        with database.read() as connection:
            try:
                return LocalRouteService.get(connection, world_id, route_id)
            except LocalRouteNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except LocalRouteConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.patch("/local-routes/{route_id}/state")
    def local_route_state(world_id: str, route_id: str, payload: LocalRouteStateRequest):
        try:
            with database.write() as connection:
                return LocalRouteService.set_open(
                    connection, world_id, route_id, **payload.model_dump(),
                )
        except LocalRouteNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LocalRouteConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @router.post(
        "/registrations",
        response_model=ElementRegistrationView,
        status_code=201,
    )
    def submit_element_registration(
        world_id: str, payload: ElementRegistrationSubmit
    ) -> ElementRegistrationView:
        """提交严格类型的世界元素注册请求；重复幂等键不会重复应用。"""

        try:
            with database.write() as connection:
                return element_registry.submit(
                    connection,
                    world_id=world_id,
                    request=payload,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @router.get(
        "/registrations",
        response_model=list[ElementRegistrationView],
    )
    def list_element_registrations(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        element_type: ElementType | None = None,
        status: RegistrationStatus | None = None,
    ) -> list[ElementRegistrationView]:
        try:
            with database.read() as connection:
                return element_registry.list(
                    connection,
                    world_id=world_id,
                    limit=limit,
                    element_type=element_type,
                    status=status,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get(
        "/registrations/{registration_id}",
        response_model=ElementRegistrationView,
    )
    def get_element_registration(
        world_id: str, registration_id: str
    ) -> ElementRegistrationView:
        try:
            with database.read() as connection:
                return element_registry.get(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post(
        "/registrations/{registration_id}/confirm",
        response_model=ElementRegistrationView,
    )
    def confirm_element_registration(
        world_id: str, registration_id: str
    ) -> ElementRegistrationView:
        try:
            with database.write() as connection:
                return element_registry.confirm(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/registrations/{registration_id}/reject",
        response_model=ElementRegistrationView,
    )
    def reject_element_registration(
        world_id: str,
        registration_id: str,
        payload: RegistrationReviewRequest,
    ) -> ElementRegistrationView:
        try:
            with database.write() as connection:
                return element_registry.reject(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                    reason=payload.reason,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/removals",
        response_model=ElementRemovalView,
        status_code=201,
    )
    def submit_element_removal(
        world_id: str, payload: ElementRemovalSubmit
    ) -> ElementRemovalView:
        """按来源事件执行可审计墓碑删除，永不绕过领域规则物理删行。"""

        try:
            with database.write() as connection:
                return element_remover.submit(
                    connection,
                    world_id=world_id,
                    request=payload,
                )
        except ElementRemovalNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ElementRemovalConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="世界正在由另一个进程更新") from exc

    @router.get(
        "/removals",
        response_model=list[ElementRemovalView],
    )
    def list_element_removals(
        world_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[ElementRemovalView]:
        with database.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
            return element_remover.list(connection, world_id=world_id, limit=limit)

    @router.get("/construction-projects")
    def list_construction_projects(world_id: str) -> list[dict[str, object]]:
        with database.read() as connection:
            exists = connection.execute(
                "SELECT 1 FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="世界不存在")
            return construction_projects.list(connection, world_id=world_id)

    @router.patch("/registrations/{registration_id}/construction")
    def update_construction_project(
        world_id: str,
        registration_id: str,
        payload: ConstructionProjectStatusUpdate,
    ) -> dict[str, object]:
        try:
            with database.write() as connection:
                return construction_projects.set_status(
                    connection,
                    world_id=world_id,
                    registration_id=registration_id,
                    status=payload.status,
                )
        except RegistrationNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
