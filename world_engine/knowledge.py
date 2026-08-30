from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_RAG_METADATA_PATTERN = re.compile(r"<!--\s*rag:\s*(.*?)\s*-->", re.IGNORECASE)
_HEADING_PATTERN = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_ASCII_TOKEN_PATTERN = re.compile(r"[a-z0-9_]{2,}")
_CJK_SEQUENCE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_VALID_AUDIENCES = {"author_hidden", "guardrail", "character_common"}
# Noryia 已取代早期三大陆政治线；这些文件作为作者存档保留，但不能进入运行时检索。
_RETIRED_POLITICAL_FILES = {
    "16-northern-continent-civilization-framework.md",
    "17-history-of-three-regions.md",
    "18-states-cultures-and-conflicts.md",
    "19-canonical-chronology.md",
    "20-current-powers-and-customs.md",
    "21-war-and-treaty-ledger.md",
    "22-lineages-gray-elves-and-dragon-isolation.md",
    "23-faiths-calendars-and-memory.md",
    "24-far-north-dragon-history.md",
}


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """一段可独立召回、带认知边界的世界设定。"""

    id: str
    source: str
    section: str
    content: str
    audience: str
    always_include: bool
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    chunk: KnowledgeChunk
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.chunk.id,
            "source": self.chunk.source,
            "section": self.chunk.section,
            "audience": self.chunk.audience,
            "score": round(self.score, 6),
            "content": self.chunk.content,
        }


class WorldKnowledgeBase:
    """针对中英文世界设定的轻量本地BM25知识库。"""

    def __init__(self, chunks: Iterable[KnowledgeChunk]) -> None:
        self.chunks = tuple(chunks)
        self._tokens = tuple(self._weighted_tokens(chunk) for chunk in self.chunks)
        self._frequencies = tuple(Counter(tokens) for tokens in self._tokens)
        self._document_frequencies = self._build_document_frequencies(self._tokens)
        self._average_length = (
            sum(len(tokens) for tokens in self._tokens) / len(self._tokens)
            if self._tokens
            else 1.0
        )

    @classmethod
    def from_paths(
        cls,
        paths: Iterable[Path],
        *,
        project_root: Path | None = None,
    ) -> WorldKnowledgeBase:
        files: set[Path] = set()
        for raw_path in paths:
            path = raw_path.resolve()
            if path.is_file() and path.suffix.lower() == ".md":
                files.add(path)
            elif path.is_dir():
                files.update(item.resolve() for item in path.rglob("*.md") if item.is_file())

        chunks: list[KnowledgeChunk] = []
        for path in sorted(files, key=lambda item: str(item).casefold()):
            if path.name in _RETIRED_POLITICAL_FILES:
                continue
            source = _display_path(path, project_root)
            chunks.extend(_parse_markdown(path, source))
        return cls(chunks)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def search(
        self,
        query: str,
        *,
        audiences: set[str] | None = None,
        limit: int = 6,
        max_total_chars: int | None = None,
    ) -> list[KnowledgeHit]:
        if limit <= 0 or not self.chunks:
            return []
        allowed = _VALID_AUDIENCES if audiences is None else audiences
        unknown = allowed - _VALID_AUDIENCES
        if unknown:
            raise ValueError(f"不支持的知识可见级别：{', '.join(sorted(unknown))}")

        query_tokens = Counter(_tokenize(query))
        candidates: list[tuple[int, KnowledgeHit]] = []
        for index, chunk in enumerate(self.chunks):
            if chunk.audience not in allowed:
                continue
            score = self._bm25_score(index, query_tokens)
            if score > 0 or chunk.always_include:
                candidates.append((index, KnowledgeHit(chunk=chunk, score=score)))

        candidates.sort(
            key=lambda item: (
                not item[1].chunk.always_include,
                -item[1].score,
                item[1].chunk.source,
                item[1].chunk.section,
                item[0],
            )
        )
        selected: list[KnowledgeHit] = []
        used_chars = 0
        for _, hit in candidates:
            if len(selected) >= limit:
                break
            content_size = len(hit.chunk.content)
            if max_total_chars is not None and used_chars + content_size > max_total_chars:
                continue
            selected.append(hit)
            used_chars += content_size
        return selected

    def _bm25_score(self, index: int, query_tokens: Counter[str]) -> float:
        if not query_tokens:
            return 0.0
        frequencies = self._frequencies[index]
        document_length = len(self._tokens[index])
        total_documents = len(self.chunks)
        score = 0.0
        k1 = 1.5
        b = 0.75
        for token, query_frequency in query_tokens.items():
            term_frequency = frequencies.get(token, 0)
            if term_frequency == 0:
                continue
            document_frequency = self._document_frequencies[token]
            inverse_document_frequency = math.log(
                1 + (total_documents - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = term_frequency + k1 * (
                1 - b + b * document_length / self._average_length
            )
            score += (
                inverse_document_frequency
                * (term_frequency * (k1 + 1) / denominator)
                * (1 + math.log(query_frequency))
            )
        return score

    @staticmethod
    def _weighted_tokens(chunk: KnowledgeChunk) -> list[str]:
        return (
            _tokenize(chunk.content)
            + _tokenize(chunk.section) * 2
            + _tokenize(" ".join(chunk.tags)) * 3
        )

    @staticmethod
    def _build_document_frequencies(
        tokenized_documents: Iterable[list[str]],
    ) -> Counter[str]:
        frequencies: Counter[str] = Counter()
        for tokens in tokenized_documents:
            frequencies.update(set(tokens))
        return frequencies


def search_dynamic_knowledge(
    connection: sqlite3.Connection,
    *,
    world_id: str,
    query: str,
    character_ids: set[str],
    location_ids: set[str],
    limit: int = 6,
    max_total_chars: int = 8000,
) -> list[KnowledgeHit]:
    """检索已注册动态知识，并在进入BM25前执行人物与地点可见性过滤。"""

    rows = connection.execute(
        """
        SELECT k.*, e.location_id
        FROM knowledge_entries k
        JOIN world_events e ON e.id = k.source_event_id
        WHERE k.world_id = ? AND k.status = 'active'
        ORDER BY k.created_at DESC
        """,
        (world_id,),
    ).fetchall()
    chunks: list[KnowledgeChunk] = []
    single_character_id = next(iter(character_ids)) if len(character_ids) == 1 else None
    for row in rows:
        level = row["knowledge_level"]
        visible = level == "public_lore"
        if level == "local_claim" and row["location_id"] in location_ids:
            visible = True
        if level == "character_belief" and row["author_character_id"] == single_character_id:
            visible = True
        if not visible:
            continue
        try:
            tags = tuple(str(item) for item in json.loads(row["tags_json"]))
        except (json.JSONDecodeError, TypeError):
            tags = ()
        chunks.append(
            KnowledgeChunk(
                id=f"dynamic:{row['id']}",
                source="sqlite:knowledge_entries",
                section=row["title"],
                content=row["content"],
                audience="character_common",
                always_include=level == "character_belief",
                tags=tags,
            )
        )
    if not chunks:
        return []
    return WorldKnowledgeBase(chunks).search(
        query,
        audiences={"character_common"},
        limit=limit,
        max_total_chars=max_total_chars,
    )

def _display_path(path: Path, project_root: Path | None) -> str:
    if project_root is not None:
        try:
            return path.relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _parse_markdown(path: Path, source: str) -> list[KnowledgeChunk]:
    text = path.read_text(encoding="utf-8")
    metadata_match = _RAG_METADATA_PATTERN.search(text)
    metadata = _parse_metadata(metadata_match.group(1) if metadata_match else "")
    audience = metadata.get("audience", "author_hidden").lower()
    if audience not in _VALID_AUDIENCES:
        raise ValueError(f"{source} 的RAG audience无效：{audience}")
    always_include = metadata.get("always_include", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    tags = tuple(
        item.strip()
        for item in re.split(r"[,，]", metadata.get("tags", ""))
        if item.strip()
    )
    if metadata_match:
        text = text[: metadata_match.start()] + text[metadata_match.end() :]

    sections = _split_sections(text, fallback_title=path.stem)
    chunks: list[KnowledgeChunk] = []
    for section, body in sections:
        for part_index, content in enumerate(_chunk_section(section, body), start=1):
            raw_id = f"{source}|{section}|{part_index}".encode()
            chunks.append(
                KnowledgeChunk(
                    id=hashlib.sha256(raw_id).hexdigest()[:16],
                    source=source,
                    section=section,
                    content=content,
                    audience=audience,
                    always_include=always_include,
                    tags=tags,
                )
            )
    return chunks


def _parse_metadata(raw_metadata: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in raw_metadata.split(";"):
        key, separator, value = field.partition("=")
        if separator and key.strip():
            result[key.strip().lower()] = value.strip()
    return result


def _split_sections(text: str, fallback_title: str) -> list[tuple[str, str]]:
    headings: list[str] = []
    current_lines: list[str] = []
    current_section = fallback_title
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_section, body))

    for line in text.splitlines():
        match = _HEADING_PATTERN.match(line)
        if not match:
            current_lines.append(line)
            continue
        flush()
        current_lines = []
        level = len(match.group(1))
        title = match.group(2).strip()
        headings[level - 1 :] = [title]
        current_section = " > ".join(headings)
    flush()
    return sections


def _chunk_section(section: str, body: str, target_chars: int = 1400) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
    parts: list[str] = []
    current: list[str] = []
    current_length = 0

    def flush() -> None:
        nonlocal current, current_length
        if current:
            parts.append(f"{section}\n\n" + "\n\n".join(current))
            current = []
            current_length = 0

    for block in blocks:
        if len(block) > target_chars:
            flush()
            for start in range(0, len(block), target_chars):
                parts.append(f"{section}\n\n{block[start : start + target_chars]}")
            continue
        added_length = len(block) + (2 if current else 0)
        if current and current_length + added_length > target_chars:
            flush()
        current.append(block)
        current_length += added_length
    flush()
    return parts


def _tokenize(text: str) -> list[str]:
    normalized = text.casefold()
    tokens = _ASCII_TOKEN_PATTERN.findall(normalized)
    for sequence in _CJK_SEQUENCE_PATTERN.findall(normalized):
        if len(sequence) == 1:
            tokens.append(sequence)
            continue
        tokens.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
        if len(sequence) >= 3:
            tokens.extend(sequence[index : index + 3] for index in range(len(sequence) - 2))
    return tokens
