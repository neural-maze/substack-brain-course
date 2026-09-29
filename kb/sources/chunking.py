"""Deterministic chunking — pure function, unit-tested separately from the pipeline.

Splits on whitespace (never mid-word) into chunks of at most `chunk_size`
characters, with roughly `chunk_overlap` characters repeated between
consecutive chunks so an idea spanning a chunk boundary isn't lost entirely
to one side.
"""

from __future__ import annotations


def chunk_text(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(words):
        current: list[str] = []
        length = 0
        end = start
        while end < len(words):
            added = len(words[end]) + (1 if current else 0)
            if current and length + added > chunk_size:
                break
            length += added
            current.append(words[end])
            end += 1

        if not current:
            # A single word longer than chunk_size: it still needs a home.
            current = [words[end]]
            end += 1

        chunks.append(" ".join(current))

        if end >= len(words):
            break

        # Step back from `end` by roughly `chunk_overlap` characters so the
        # next chunk repeats the tail of this one.
        overlap_words: list[str] = []
        overlap_len = 0
        j = end - 1
        while j >= start and overlap_len < chunk_overlap:
            overlap_len += len(words[j]) + 1
            overlap_words.insert(0, words[j])
            j -= 1
        start = j + 1 if overlap_words else end

    return chunks
