"""哥哥，为已选中的人物知识补回来源，不把文化说法变成人格或同意规则。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def current_character_influences(
    hits: Iterable[dict[str, object]], *, character_id: str,
) -> list[dict[str, object]]:
    """哥哥，已核验的本人观点和相关地方资料是当下参考，不是可随手丢掉的旧聊天。"""
    eligible = []
    for hit in hits:
        source = hit.get("provenance")
        if (
            not isinstance(source, dict) or hit.get("audience") != "character_common"
            or not str(hit.get("id", "")).startswith("dynamic:")
            or hit.get("source") != "sqlite:knowledge_entries"
            or not source.get("source_event_id") or not source.get("registration_id")
            or not isinstance(hit.get("content"), str)
        ):
            continue
        level = source.get("knowledge_level")
        if level not in {"public_lore", "local_claim", "character_belief"}:
            continue
        if level == "character_belief" and source.get("author_character_id") != character_id:
            continue
        eligible.append(hit)
    # 哥哥，先保留本人的例外，再沿既有检索顺序取相关说法；不额外检索或截断事实。
    eligible.sort(key=lambda hit: hit["provenance"]["knowledge_level"] != "character_belief")
    return [
        {"id": hit["id"], "section": hit.get("section", ""), "content": hit["content"],
         "provenance": dict(hit["provenance"])}
        for hit in eligible[:3]
    ]


def annotate_registered_lore(
    connection: sqlite3.Connection,
    *,
    world_id: str,
    character_id: str,
    location_ids: set[str],
    hits: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """只读核对已召回动态知识；静态知识保持原顺序与内容。

    来源说明描述知识的归属和可信程度，不证明人物赞同它，更不能替人物
    作出相处选择。不会按种族推定文化，也不额外召回未入选的资料。
    动态来源必须已经发生，且观察者确实属于当前世界。
    """
    selected = list(hits)
    dynamic_ids = {
        value.removeprefix("dynamic:")
        for hit in selected
        if isinstance(value := hit.get("id"), str) and value.startswith("dynamic:")
    }
    if not dynamic_ids:
        return selected

    placeholders = ",".join("?" for _ in dynamic_ids)
    rows = connection.execute(
        f"""
        SELECT k.*, e.location_id AS source_location_id
        FROM knowledge_entries k
        JOIN worlds w ON w.id = k.world_id
        JOIN characters observer ON observer.id = ? AND observer.world_id = w.id
        JOIN element_registration_requests r
          ON r.id = k.registration_id AND r.world_id = k.world_id
        JOIN world_events e
          ON e.id = k.source_event_id AND e.world_id = k.world_id
        WHERE k.world_id = ? AND k.id IN ({placeholders})
          AND k.status = 'active'
          AND e.occurred_at <= w.current_time
          AND r.element_type = 'lore' AND r.status = 'applied'
          AND r.result_entity_id = k.id AND r.source_event_id = k.source_event_id
          AND r.requested_by_character_id IS k.author_character_id
        """,  # noqa: S608 - 占位符只由已选结果数量生成，值仍参数化。
        (character_id, world_id, *sorted(dynamic_ids)),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    result: list[dict[str, object]] = []
    for hit in selected:
        hit_id = hit.get("id")
        if not isinstance(hit_id, str) or not hit_id.startswith("dynamic:"):
            result.append(hit)
            continue
        if hit.get("audience") in {"author_hidden", "author_canon"}:
            continue
        row = by_id.get(hit_id.removeprefix("dynamic:"))
        if row is None:
            continue
        level = row["knowledge_level"]
        visible = (
            level == "public_lore"
            or (level == "local_claim" and row["source_location_id"] in location_ids)
            or (level == "character_belief" and row["author_character_id"] == character_id)
        )
        if not visible:
            continue
        # 哥哥，动态正文以当前正式条目为准，不让旧召回或外带标签覆盖登记来源。
        annotated = dict(hit)
        annotated.update(
            source="sqlite:knowledge_entries",
            section=row["title"],
            content=row["content"],
            audience="character_common",
            provenance={
                "knowledge_level": level,
                "author_character_id": row["author_character_id"],
                "confidence": row["confidence"],
                "source_event_id": row["source_event_id"],
                "registration_id": row["registration_id"],
                "subject_entity_id": row["subject_entity_id"],
                "source_location_id": row["source_location_id"],
            },
        )
        result.append(annotated)
    return result
