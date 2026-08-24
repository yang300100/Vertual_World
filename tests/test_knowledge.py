from __future__ import annotations

from pathlib import Path

import pytest

from world_engine.knowledge import WorldKnowledgeBase


def _write_document(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_chinese_search_returns_relevant_section_and_keeps_audience(tmp_path: Path) -> None:
    _write_document(
        tmp_path / "magic.md",
        """<!-- rag: audience=author_hidden; tags=魔力,枯竭 -->
# 魔法设定

## 魔力枯竭

持续施法会使区域魔力衰退，需要经过一段时间恢复。

## 航海

远洋船只依靠季风决定航线。
""",
    )
    knowledge_base = WorldKnowledgeBase.from_paths((tmp_path,), project_root=tmp_path)

    hits = knowledge_base.search("为什么持续施法会造成魔力枯竭", limit=1)

    assert knowledge_base.chunk_count == 2
    assert hits[0].chunk.section == "魔法设定 > 魔力枯竭"
    assert hits[0].chunk.audience == "author_hidden"
    assert hits[0].chunk.source == "magic.md"


def test_always_include_still_obeys_character_visibility(tmp_path: Path) -> None:
    _write_document(
        tmp_path / "author.md",
        """<!-- rag: audience=author_hidden; always_include=true -->
# 作者约束

人物不能知道隐藏真相。
""",
    )
    _write_document(
        tmp_path / "common.md",
        """<!-- rag: audience=character_common; always_include=true -->
# 通用常识

市场中的货物都有主人。
""",
    )
    knowledge_base = WorldKnowledgeBase.from_paths((tmp_path,))

    hits = knowledge_base.search(
        "完全无关的查询",
        audiences={"character_common"},
        limit=5,
    )

    assert [hit.chunk.section for hit in hits] == ["通用常识"]


def test_empty_audiences_returns_no_knowledge(tmp_path: Path) -> None:
    _write_document(
        tmp_path / "hidden.md",
        """# 隐藏真相

这份没有元数据的资料应当安全地默认为作者隐藏知识。
""",
    )
    knowledge_base = WorldKnowledgeBase.from_paths((tmp_path,))

    assert knowledge_base.chunks[0].audience == "author_hidden"
    assert knowledge_base.search("隐藏真相", audiences=set()) == []


def test_invalid_audience_fails_fast(tmp_path: Path) -> None:
    _write_document(
        tmp_path / "invalid.md",
        """<!-- rag: audience=everyone -->
# 无效资料

这份资料不应被静默加载。
""",
    )

    with pytest.raises(ValueError, match="audience无效"):
        WorldKnowledgeBase.from_paths((tmp_path,))
