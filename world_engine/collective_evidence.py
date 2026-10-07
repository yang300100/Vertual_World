"""哥哥，只解析本人实际取得的记录及随信送达的证据，绝不读取他人的私有记录。"""

import json

from world_engine.repository import to_iso


def visible_evidence(c, actor_id, at):
    rows = c.execute(
        "SELECT id,kind,summary,content_json,world_time FROM collective_observations "
        "WHERE character_id=? AND world_time<=? ORDER BY world_time,rowid",
        (actor_id, to_iso(at)),
    ).fetchall()
    packets = {}
    for row in rows:
        packets[row["id"]] = {
            "id": row["id"],
            "summary": row["summary"],
            "content": json.loads(row["content_json"]),
            "world_time": row["world_time"],
        }
    for row in rows:
        content = packets[row["id"]]["content"]
        if row["kind"] != "received_report":
            continue
        receipt = {
            "observation_id": row["id"],
            "holder_id": actor_id,
            "received_at": row["world_time"],
        }
        # 哥哥，转发可以包着旧收件记录；只沿实际附带的证据读取，不把文字里的编号当证据。
        while content.get("kind") == "received_report":
            evidence = content.get("evidence")
            if not isinstance(evidence, dict) or not all(
                key in evidence for key in ("id", "summary", "content", "world_time")
            ):
                break
            packets.setdefault(
                evidence["id"],
                {
                    **evidence,
                    "relay_receipts": [*evidence.get("relay_receipts", []), receipt],
                },
            )
            content = evidence["content"]
            if not isinstance(content, dict):
                break
    return packets


def known_evidence(c, actor_id, at):
    return set(visible_evidence(c, actor_id, at))


def evidence_packet(c, actor_id, observation_id, at):
    packet = visible_evidence(c, actor_id, at).get(observation_id)
    if packet is None:
        raise ValueError("不能引用或寄送本人尚未实际取得的证据")
    return packet
