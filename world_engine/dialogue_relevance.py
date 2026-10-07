"""Yuki 为 NPC 旧资料挑话题、保留认知边界，并把审计信息留在服务端。"""

from __future__ import annotations

import re

DIALOGUE_SOURCE_LIMITS = {
    "knowledge_context": (1200, 3),
    "recent_private_memories": (500, 3),
    "conversation_episodes": (500, 2),
    "recalled_past_conversation": (600, 3),
    "scene.recent_personal_events": (350, 3),
    "personal_experience.known_events": (350, 3),
    "personal_experience.beliefs": (400, 3),
}

_WORDS = re.compile(r"[a-z0-9_]{2,}|[㐀-䶿一-鿿]+", re.IGNORECASE)
_TURN = re.compile(r"^\[[^\]\r\n]+\] (?:玩家行动|玩家|NPC)：([\s\S]*)$")
_SPEAKER = re.compile(r"(?m)^(?:玩家行动|玩家|NPC)：")
_QUOTES = re.compile(r"[“「]([^”」]+)[”」]")
_SOCIAL_HEADER = re.compile(r"^[^\r\n]+?与[^\r\n]+?进行了一次交流。")
_SOCIAL_LINE = re.compile(r"「[^」\r\n]+」(?:开口道|回应道)：“([\s\S]*?)”。")
_PAST = ("刚才", "刚刚", "之前", "以前", "上次", "最近", "记得", "那件事", "发生了什么")
_GENERIC = (
    "在干什么",
    "干什么",
    "做什么",
    "发生了什么",
    "你这是",
    "你是不是",
    "你是",
    "是什么",
    "有什么",
    "为什么",
    "什么",
    "怎么样",
    "怎么",
    "在哪里",
    "哪里",
    "你好呀",
    "你好",
    "您好",
    "小伙伴",
    "可以",
    "能不能",
    "有没有",
    "还有",
    "刚才",
    "刚刚",
    "之前",
    "以前",
    "上次",
    "最近",
    "记得",
    "那件事",
    "原话",
    "今天",
    "昨天",
    "明天",
    "现在",
    "一下",
    "了吗",
    "好吗",
    "我吗",
    "就是",
    "继续",
    "然后呢",
    "接下来",
    "是的",
    "这一行",
)
_FOLLOW_UP = ("那今天", "那现在", "那之后", "那件事", "继续", "然后呢", "那你呢")


def natural_text(value: object) -> str:
    """Yuki 只匹配正文，不让 UUID、检索分数或字段名误触发召回。"""
    if isinstance(value, str):
        match = _TURN.match(value)
        return match[1] if match else value
    if not isinstance(value, dict):
        return ""
    return " ".join(
        str(value[key])
        for key in (
            "summary",
            "content",
            "subject_name",
            "value",
            "title",
            "details",
            "result",
        )
        if value.get(key) is not None
    )


class DialogueRelevance:
    """Yuki 忽略双方称呼和泛问词；需要续接时才借用最近一轮话题。"""

    def __init__(self, query: str, *, names: tuple[str, ...] = (), history=()) -> None:
        self.query = query
        self.names = tuple(name for name in names if name)
        self.asks_about_past = any(cue in query for cue in _PAST)
        self.terms = self._terms(query)
        if (not self.terms and any(cue in query for cue in _FOLLOW_UP)) or "这一行" in query:
            self.terms.update(self._terms(" ".join(natural_text(turn) for turn in history[-2:])))

    def wants_original(self, turns: list[object]) -> bool:
        """Yuki 只在追溯原话或继续核对旧行动时加入完整旧问答。"""
        action_followup = any(
            isinstance(turn, str) and "] 玩家行动：" in turn for turn in turns
        ) and any(cue in self.query for cue in ("接下来", "继续", "核对", "进度", "完成"))
        return self.asks_about_past or "原话" in self.query or action_followup

    def _terms(self, text: str) -> set[str]:
        for word in (*self.names, *_GENERIC):
            text = text.replace(word, " ")
        for word in ("进城", "入城", "出城"):
            text = text.replace(word, "城门")
        terms: set[str] = set()
        for match in _WORDS.findall(text.casefold()):
            if re.fullmatch(r"[你我他她它这那还都啊呀呢吗吧的了么欸嗯哦是]+", match):
                continue
            if match.isascii():
                terms.add(match)
            else:
                terms.update(match[index : index + 2] for index in range(len(match) - 1))
        return terms

    def matches(self, value: object) -> bool:
        return bool(self.terms & self._terms(natural_text(value)))


def _normalized(text: str) -> str:
    return "".join(re.findall(r"[a-z0-9㐀-䶿一-鿿]", text.casefold()))


class DialogueCoverage:
    """Yuki 只用已装入消息的资料去重，预算淘汰的历史不会遮掉记忆。"""

    def __init__(self) -> None:
        self.fragments: list[str] = []
        self.event_ids: set[str] = set()

    def repeated(self, value: object, *, source: str = "") -> bool:
        if isinstance(value, dict):
            ids = value.get("source_event_ids") or [value.get("event_id") or value.get("id")]
            ids = [str(item) for item in ids if item]
            if ids and all(item in self.event_ids for item in ids):
                if source != "recent_private_memories" or any(
                    _normalized(natural_text(value)) in previous for previous in self.fragments
                ):
                    return True
        text = natural_text(value)
        social_body = _SOCIAL_HEADER.sub("", text)
        social_parts = _SOCIAL_LINE.findall(social_body)
        if social_parts:
            if _normalized(_SOCIAL_LINE.sub("", social_body)):
                return False
            return all(
                _normalized(part) in self.fragments for part in social_parts if _normalized(part)
            )
        # Yuki 不让旧问题或旧台词覆盖后来真正发生的非对话事实。
        if source in {
            "scene.recent_personal_events",
            "personal_experience.known_events",
            "knowledge_context",
        }:
            if not isinstance(value, dict) or value.get("event_type") != "action.socialize":
                return False
        quotes = _QUOTES.findall(text)
        if source == "recent_private_memories" and isinstance(value, dict):
            if value.get("memory_type") and not quotes and not _SPEAKER.search(text):
                return False
        if not quotes and isinstance(value, dict) and value.get("event_type") == "action.socialize":
            if any(len(part) >= 4 and part in _normalized(text) for part in self.fragments):
                return True
        if quotes and not _SPEAKER.search(text):
            outside = _QUOTES.sub("", text)
            outside = re.sub(r"^我与.+?交谈：", "", outside)
            outside = re.sub(r"对方说|我回应|玩家说|NPC回应|NPC说|开口道|回应道", "", outside)
            # Yuki 不能因为引号里的旧台词相同，就删掉引号外的新发现。
            if _normalized(outside):
                return False
        parts = _SPEAKER.split(text) if _SPEAKER.search(text) else quotes
        parts = parts or [text]
        normalized = [_normalized(part) for part in parts if _normalized(part)]
        if quotes or _SPEAKER.search(text):
            return bool(normalized) and all(part in self.fragments for part in normalized)
        return bool(normalized) and all(
            len(part) >= 4 and any(part in previous for previous in self.fragments)
            for part in normalized
        )

    def remember(self, value: object, *, event_ids: bool = True) -> None:
        if isinstance(value, dict) and event_ids:
            ids = value.get("source_event_ids") or [value.get("event_id") or value.get("id")]
            self.event_ids.update(str(item) for item in ids if item)
        text = natural_text(value)
        social_parts = _SOCIAL_LINE.findall(_SOCIAL_HEADER.sub("", text))
        parts = _SPEAKER.split(text) if _SPEAKER.search(text) else social_parts or [text]
        self.fragments.extend(_normalized(part) for part in parts if _normalized(part))


def compact_dialogue_item(
    source: str,
    value: object,
    relevance: DialogueRelevance,
    *,
    keep_provenance: bool = False,
) -> object:
    """Yuki 保留完整事实句和来源状态，不用硬截半句来节省上下文。"""
    if not isinstance(value, dict):
        return value
    fields = {
        "knowledge_context": ("audience", "section", "content", "valid_from", "valid_until"),
        "beliefs": (
            "subject_name",
            "predicate",
            "value",
            "status",
            "confidence",
            "last_verified_at",
            "summary",
            "evidence",
        ),
        "conversation_episodes": ("summary", "summary_kind", "open_questions", "open_commitments"),
        "recent_private_memories": ("summary", "memory_type", "importance", "confidence"),
        "recent_personal_events": ("event_type", "occurred_at", "summary", "occurrences"),
        "known_events": ("summary", "occurred_at", "source_kind", "confidence"),
    }.get(source)
    if fields is None:
        return dict(value)
    result = {key: value[key] for key in fields if value.get(key) is not None}
    if source == "beliefs" and isinstance(result.get("evidence"), list):
        result["evidence"] = [
            {
                key: entry[key]
                for key in ("method", "quote", "source_name", "learned_at")
                if entry.get(key) is not None
            }
            for entry in result["evidence"][:2]
            if isinstance(entry, dict)
        ]
    if source == "conversation_episodes":
        summary = str(result.get("summary") or "")
        # Yuki 按完整问答提取相关旧原话，节选仍标为原话，不推断新的事实。
        exchanges = re.split(r"(?m)(?=^玩家(?:行动)?：)", summary)
        selected = [part for part in exchanges if part.strip() and relevance.matches(part)]
        if selected and len(selected) < len([part for part in exchanges if part.strip()]):
            result["summary"] = "\n".join(part.strip() for part in selected[-2:])
            result["summary_kind"] = "original_excerpt"
        for key in ("open_questions", "open_commitments"):
            if isinstance(result.get(key), list):
                result[key] = [item for item in result[key] if relevance.matches(item)][:2]
                if not result[key]:
                    result.pop(key)
    if keep_provenance:
        # Yuki 在两层预算之间保留去重身份，最终发送时再去掉这些审计字段。
        result.update(
            {
                key: value[key]
                for key in ("id", "event_id", "source_event_ids")
                if value.get(key) is not None
            }
        )
    return result
