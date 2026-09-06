"""玩家显式输入渠道：前缀是界面协议，不能被会话或模型改成台词。"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerInput:
    kind: str
    text: str


def parse_player_input(raw: str) -> PlayerInput:
    match = re.match(r"^\s*(动作|行动|说话|对话|发言)\s*[:：]\s*(.*)$", raw, re.S)
    if not match:
        return PlayerInput("auto", raw.strip())
    kind = "action" if match[1] in {"动作", "行动"} else "speech"
    return PlayerInput(kind, match[2].strip())
