"""`PlayerMovementMixin`：玩家持续移动的创建、取消与载具切换。

从 `WorldEngine` 切出的职责切片。方法体、签名与 `self` 语义一字未改。
"""

from __future__ import annotations

from world_engine.domain import MovementState, WorldSnapshot
from world_engine.repository import to_iso, utc_now


class PlayerMovementMixin:
    """`WorldEngine` 的玩家移动职责切片。"""

    def start_player_movement(
        self,
        world_id: str,
        *,
        destination_longitude: float,
        destination_latitude: float,
        vehicle_id: str | None = None,
    ) -> MovementState:
        """按玩家当前能力创建持续移动，不直接改写为目标坐标。"""
        now = utc_now()
        with self.database.write() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next(
                (
                    character
                    for character in snapshot.characters
                    if character.is_player and character.is_pov
                ),
                None,
            )
            if player is None:
                raise ValueError("当前世界还没有玩家角色")
            movement = self.movement.start(
                connection,
                world_id=world_id,
                character_id=player.id,
                destination_longitude=destination_longitude,
                destination_latitude=destination_latitude,
                vehicle_id=vehicle_id,
                world_time=snapshot.world.current_time,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE worlds SET version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), world_id),
            )
        return movement


    def cancel_player_movement(self, world_id: str) -> MovementState:
        """取消玩家当前行程，保留已经走过的真实坐标。"""
        now = utc_now()
        with self.database.write() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next(
                (
                    character
                    for character in snapshot.characters
                    if character.is_player and character.is_pov
                ),
                None,
            )
            if player is None:
                raise ValueError("当前世界还没有玩家角色")
            movement = self.movement.cancel(
                connection,
                world_id=world_id,
                character_id=player.id,
                world_time=snapshot.world.current_time,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE worlds SET version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), world_id),
            )
        return movement


    def select_player_transport(
        self, world_id: str, vehicle_id: str | None
    ) -> WorldSnapshot:
        """切换玩家当前载具；移动中禁止切换。"""
        now = utc_now()
        with self.database.write() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next(
                (
                    character
                    for character in snapshot.characters
                    if character.is_player and character.is_pov
                ),
                None,
            )
            if player is None:
                raise ValueError("当前世界还没有玩家角色")
            self.movement.select_transport(
                connection,
                world_id=world_id,
                character_id=player.id,
                vehicle_id=vehicle_id,
            )
            connection.execute(
                """
                UPDATE worlds SET version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), world_id),
            )
            return self.repository.get_snapshot(connection, world_id)
