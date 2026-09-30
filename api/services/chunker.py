from __future__ import annotations
import threading
from typing import Any
from models.schemas import IngestConfig

from langchain_text_splitters import CharacterTextSplitter, RecursiveCharacterTextSplitter

MAX_OVERLAP_WINDOWS = 10_000
MAX_OVERLAP_OUTPUT_CHARACTERS = 10_000_000

_semantic_model = None
_semantic_model_lock = threading.Lock()


def _get_semantic_model():
    global _semantic_model
    if _semantic_model is None:
        with _semantic_model_lock:
            if _semantic_model is None:
                from sentence_transformers import SentenceTransformer
                _semantic_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _semantic_model


def _enforce_min_chunk_size(chunks: list[str], min_size: int) -> list[str]:
    if not chunks or min_size <= 0:
        return chunks
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) < min_size and result:
            result[-1] = result[-1] + " " + chunk
        else:
            result.append(chunk)
    return [c for c in result if c.strip()] or chunks


def chunk_fixed(text: str, chunk_size: int, min_chunk_size: int) -> list[str]:
    splitter = CharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=0, separator="")
    chunks = splitter.split_text(text)
    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_overlap(text: str, chunk_size: int, chunk_overlap: int, min_chunk_size: int) -> list[str]:
    # This strategy promises character overlap, independently of paragraph or
    # word boundaries. A separator-only splitter cannot bound a long paragraph.
    for name, value in (("chunk_size", chunk_size), ("chunk_overlap", chunk_overlap),
                        ("min_chunk_size", min_chunk_size)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_size must be positive and 0 <= chunk_overlap < chunk_size")
    if min_chunk_size < 0:
        raise ValueError("min_chunk_size must be nonnegative")
    if not text.strip():
        return []

    stride = chunk_size - chunk_overlap
    windows = 1 if len(text) <= chunk_size else 1 + (len(text) - chunk_size + stride - 1) // stride
    # Count repeated overlap before allocating slices. The count and payload
    # limits are conservative before the optional tail merge removes overlap.
    output_characters = len(text) + (windows - 1) * chunk_overlap
    if windows > MAX_OVERLAP_WINDOWS or output_characters > MAX_OVERLAP_OUTPUT_CHARACTERS:
        raise ValueError(
            f"Overlap output exceeds per-file limit: {windows} pre-merge windows "
            f"(maximum {MAX_OVERLAP_WINDOWS}), {output_characters} characters "
            f"(maximum {MAX_OVERLAP_OUTPUT_CHARACTERS}). Reduce overlap or input size.")

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - chunk_overlap

    # The tail repeats the preceding window's overlap. Append only its new
    # suffix, preserving coverage without duplicating those repeated bytes.
    # Only this last window can be undersized. A short whole document stays
    # one short chunk; minimum size is a preference, not fabricated content.
    if len(chunks) > 1 and len(chunks[-1]) < min_chunk_size:
        tail = chunks.pop()
        chunks[-1] += tail[chunk_overlap:]
    return chunks


def chunk_language(
    text: str, chunk_size: int, chunk_overlap_size: int, min_chunk_size: int
) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap_size,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_text(text)
    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_context_aware(
    elements: list[Any], chunk_size: int, min_chunk_size: int
) -> list[str]:
    chunks: list[str] = []
    buffer = ""

    for el in elements:
        category = getattr(el, "category", "NarrativeText")
        text = str(el).strip()
        if not text:
            continue

        if category == "Table":
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""
            chunks.append(text)
            continue

        if buffer and len(buffer) + len(text) + 1 > chunk_size:
            chunks.append(buffer.strip())
            buffer = text
        else:
            buffer = (buffer + " " + text).strip() if buffer else text

    if buffer.strip():
        chunks.append(buffer.strip())

    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_semantic(
    text: str, similarity_threshold: float, min_chunk_size: int
) -> list[str]:
    import numpy as np

    model = _get_semantic_model()

    raw_sentences = [s.strip() for s in text.replace("\n", " ").split(". ") if s.strip()]
    if not raw_sentences:
        return [text] if text.strip() else []

    embeddings = model.encode(raw_sentences, convert_to_numpy=True)

    chunks: list[str] = []
    current: list[str] = [raw_sentences[0]]

    for i in range(1, len(raw_sentences)):
        sim = float(
            np.dot(embeddings[i - 1], embeddings[i])
            / (np.linalg.norm(embeddings[i - 1]) * np.linalg.norm(embeddings[i]) + 1e-10)
        )
        if sim >= similarity_threshold:
            current.append(raw_sentences[i])
        else:
            chunks.append(". ".join(current) + ".")
            current = [raw_sentences[i]]

    if current:
        chunks.append(". ".join(current) + ".")

    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk(
    text: str,
    strategy: str,
    chunk_size: int = 1000,
    chunk_overlap_size: int = 200,
    similarity_threshold: float = 0.85,
    min_chunk_size: int = 100,
    elements: list[Any] | None = None,
) -> list[str]:
    config = IngestConfig(chunking_strategy=strategy, chunk_size=chunk_size,
                          chunk_overlap=chunk_overlap_size, similarity_threshold=similarity_threshold,
                          min_chunk_size=min_chunk_size)
    strategy, chunk_size, chunk_overlap_size = config.chunking_strategy, config.chunk_size, config.chunk_overlap
    min_chunk_size = config.min_chunk_size
    similarity_threshold = config.similarity_threshold if config.similarity_threshold is not None else 0.85
    if strategy == "fixed":
        return chunk_fixed(text, chunk_size, min_chunk_size)
    elif strategy == "overlap":
        return chunk_overlap(text, chunk_size, chunk_overlap_size, min_chunk_size)
    elif strategy == "language":
        return chunk_language(text, chunk_size, chunk_overlap_size, min_chunk_size)
    elif strategy == "context_aware":
        if elements is None:
            return chunk_language(text, chunk_size, 0, min_chunk_size)
        return chunk_context_aware(elements, chunk_size, min_chunk_size)
    elif strategy == "semantic":
        return chunk_semantic(text, similarity_threshold, min_chunk_size)
    else:
        raise ValueError(f"Unsupported chunking strategy: {strategy!r}")
