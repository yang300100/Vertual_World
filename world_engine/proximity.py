"""人物视线与近距离交互的统一空间阈值。"""

# 100 米：人物管理当前以 0.001° 展示坐标，约等于百米级网格。
# 场景展示、交谈、联络信笺与前端过滤必须共用此唯一阈值。
VISIBLE_PERSON_RADIUS_KM = 0.1
VOICE_RADIUS_KM = {"whisper": 0.003, "normal": 0.02, "shout": 0.1}


def same_room(first, second) -> bool:
    """门后的独立空间不能仅凭经纬度相同就成为同一可交互场景。"""
    def room(value):
        if hasattr(value, "keys"):
            return value["current_room_id"] if "current_room_id" in value.keys() else None
        return getattr(value, "current_room_id", None)

    return room(first) == room(second)
