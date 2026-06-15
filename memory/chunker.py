"""
memory/chunker.py

将记忆内容分割为语义 chunk，用于向量索引。

分块策略：
- 短记忆 (<512 字符) 不分割，整条作为一个 chunk
- 长记忆按 markdown 标题 / 双换行 分割为段落
- 超长段落按滑动窗口切分（max=1500 字符，overlap=150）
- 每个 chunk 附加 preamble (name + description) 以提升 embedding 质量
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_CHUNK_CHARS = 1500
OVERLAP_CHARS = 150
SHORT_THRESHOLD = 512

_HEADING_RE = re.compile(r"^#{1,4}\s", re.MULTILINE)


@dataclass
class Chunk:
    source_name: str
    chunk_index: int
    content: str
    embed_text: str
    metadata: dict = field(default_factory=dict)


def chunk_memory(
    name: str,
    description: str,
    content: str,
    metadata: dict | None = None,
) -> list[Chunk]:
    """
    将一条记忆分块。至少返回一个 Chunk。

    Args:
        name: 记忆名（kebab-case slug）
        description: 一行描述
        content: 记忆正文（markdown）
        metadata: 附加元数据（type 等）

    Returns:
        Chunk 列表，每个 chunk 带独立的 embed_text
    """
    metadata = metadata or {}
    content = content.strip()
    if not content:
        content = description

    preamble = f"{name}: {description}"

    if len(content) <= SHORT_THRESHOLD:
        return [Chunk(
            source_name=name,
            chunk_index=0,
            content=content,
            embed_text=f"{preamble}\n\n{content}",
            metadata=metadata,
        )]

    paragraphs = _split_into_paragraphs(content)
    merged = _merge_short_paragraphs(paragraphs)
    final_segments = _apply_sliding_window(merged)

    chunks: list[Chunk] = []
    for i, segment in enumerate(final_segments):
        chunks.append(Chunk(
            source_name=name,
            chunk_index=i,
            content=segment,
            embed_text=f"{preamble}\n\n{segment}",
            metadata=metadata,
        ))

    return chunks if chunks else [Chunk(
        source_name=name,
        chunk_index=0,
        content=content,
        embed_text=f"{preamble}\n\n{content}",
        metadata=metadata,
    )]


def _split_into_paragraphs(text: str) -> list[str]:
    """按 markdown 标题和双换行分割为段落。"""
    parts: list[str] = []
    lines = text.split("\n")
    current: list[str] = []

    for line in lines:
        if _HEADING_RE.match(line) and current:
            parts.append("\n".join(current).strip())
            current = [line]
        elif line.strip() == "" and current:
            joined = "\n".join(current).strip()
            if joined:
                parts.append(joined)
            current = []
        else:
            current.append(line)

    if current:
        joined = "\n".join(current).strip()
        if joined:
            parts.append(joined)

    return [p for p in parts if p]


def _merge_short_paragraphs(paragraphs: list[str]) -> list[str]:
    """合并连续的短段落，使每个 segment 尽量接近 MAX_CHUNK_CHARS。"""
    if not paragraphs:
        return []

    merged: list[str] = []
    buffer = paragraphs[0]

    for para in paragraphs[1:]:
        combined_len = len(buffer) + len(para) + 2  # +2 for \n\n
        if combined_len <= MAX_CHUNK_CHARS:
            buffer = f"{buffer}\n\n{para}"
        else:
            merged.append(buffer)
            buffer = para

    merged.append(buffer)
    return merged


def _apply_sliding_window(segments: list[str]) -> list[str]:
    """对超长 segment 应用滑动窗口切分。"""
    result: list[str] = []
    for segment in segments:
        if len(segment) <= MAX_CHUNK_CHARS:
            result.append(segment)
        else:
            result.extend(_sliding_window_split(segment))
    return result


def _sliding_window_split(text: str) -> list[str]:
    """滑动窗口切分超长文本。"""
    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + MAX_CHUNK_CHARS
        if end >= text_len:
            chunks.append(text[start:])
            break

        # 尝试在最近的换行或空格处断句
        break_pos = text.rfind("\n", start + MAX_CHUNK_CHARS // 2, end)
        if break_pos == -1:
            break_pos = text.rfind(" ", start + MAX_CHUNK_CHARS // 2, end)
        if break_pos == -1:
            break_pos = end

        chunks.append(text[start:break_pos].rstrip())
        start = break_pos - OVERLAP_CHARS
        if start < 0:
            start = 0

    return chunks
