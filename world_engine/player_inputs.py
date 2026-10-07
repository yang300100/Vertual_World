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


def approach_target_phrase(raw: str) -> str | None:
    """Yuki 只接收明确走向某人的句式，免得把问话误当成位移。"""
    match = re.fullmatch(
        r"(?:我)?(?:走到|走近|走向|靠近|来到|移动到|移动至|移到|前往)(.+?)(?:的)?(?:附近|身边|面前|旁边)[。！!]*",
        raw.strip(),
    )
    return match[1].strip() if match else None


def gift_target_phrase(raw: str) -> tuple[str, str] | None:
    """Yuki 把明确的赠物句拆成物品与收件人，问句不会进入这条规则。"""
    match = re.fullmatch(
        r"(?:我)?(?:将|把)\s*(.+?)\s*(?:赠与|赠送给|送给|交给|归还给)\s*(.+?)[。！!]*",
        raw.strip(),
    )
    if not match:
        return None
    item_name, target_label = match[1].strip(), match[2].strip()
    return (item_name, target_label) if item_name and target_label else None


def unprefixed_action_text(raw: str) -> str | None:
    """Yuki 只把开头明确描述的动作交给确定性结算，问句仍留给对话。"""
    if parse_player_input(raw).kind != "auto":
        return None
    text = raw.strip()
    if not text or "？" in text or "?" in text or re.search(r"[吗么呢]\s*[。！!]*$", text):
        return None
    if re.match(r"^(?:我)?(?:想问|要问|请问|询问|打听|告诉|聊|交谈)", text):
        return None
    action = re.sub(r"^(?:我)?(?:(?:现在|这就|马上|先|想要|想|要|打算|准备)\s*)*", "", text)
    from world_engine.life import parse_life_activity
    from world_engine.player_activities import activity_steps, native_action

    if (native_action(action) or parse_life_activity(action)
            or approach_target_phrase(action) or gift_target_phrase(action)):
        return action
    record_action = (
        r"^(?:记录|记下|记载|测绘|勘察|观察|查看|检查|调查|"
        r"整理|誊清|誊写|核对|校对|校核|比对)"
    )
    if re.match(record_action, action) and activity_steps(action):
        return action
    return None
