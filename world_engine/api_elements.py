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


class ConstructionProjectStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["surveyed", "constructing", "cancelled"]


class RegistrationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


def build_elements_router(database) -> APIRouter:
    """世界元素登记、移除与建筑工程的接口。"""

    router = APIRouter(prefix="/api/worlds/{world_id}", tags=["世界元素"])

    element_registry = WorldElementRegistry()
    element_remover = WorldElementRemover()
    construction_projects = ConstructionProjectService()

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
