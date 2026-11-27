import hashlib
import re
from dataclasses import dataclass


TOKEN_RE = re.compile(r"\S+")
PARAGRAPH_SPLIT_RE = re.compile(r"\n{2,}")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkData:
    page_number: int
    chunk_index: int
    content: str
    content_hash: str


def normalize_text(text):
    cleaned = (text or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _split_long_unit(text, chunk_size, overlap):
    tokens = TOKEN_RE.findall(text)
    if len(tokens) <= chunk_size:
        return [" ".join(tokens)] if tokens else []
    step = max(chunk_size - overlap, 1)
    parts = []
    for start in range(0, len(tokens), step):
        part = " ".join(tokens[start : start + chunk_size]).strip()
        if part:
            parts.append(part)
        if start + chunk_size >= len(tokens):
            break
    return parts


def _structured_units(text, chunk_size, overlap):
    paragraphs = [p.strip() for p in PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    units = []
    for paragraph in paragraphs:
        if len(TOKEN_RE.findall(paragraph)) <= chunk_size:
            units.append((paragraph, "\n\n"))
            continue
        sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(paragraph) if s.strip()]
        if len(sentences) > 1:
            for sentence in sentences:
                if len(TOKEN_RE.findall(sentence)) <= chunk_size:
                    units.append((sentence, " "))
                else:
                    for piece in _split_long_unit(sentence, chunk_size, overlap):
                        units.append((piece, " "))
        else:
            for piece in _split_long_unit(paragraph, chunk_size, overlap):
                units.append((piece, " "))
    return units


def _pack_units(units, chunk_size, overlap):
    if not units:
        return []
    # Fast path when the entire page is already split by sliding window
    if len(units) > 1 and all(
        len(TOKEN_RE.findall(u)) >= max(chunk_size - overlap, 1) for u, _ in units[:-1]
    ):
        return [u for u, _ in units]

    packed = []
    current_parts = []
    current_tokens = 0
    for unit_text, separator in units:
        unit_tokens = len(TOKEN_RE.findall(unit_text))
        if not current_parts:
            current_parts = [unit_text]
            current_tokens = unit_tokens
            continue
        if current_tokens + unit_tokens <= chunk_size:
            current_parts.append(separator + unit_text)
            current_tokens += unit_tokens
            continue

        chunk_text = "".join(current_parts).strip()
        if chunk_text:
            packed.append(chunk_text)

        if overlap > 0 and chunk_text:
            tail_tokens = TOKEN_RE.findall(chunk_text)[-overlap:]
            if tail_tokens and (len(tail_tokens) + unit_tokens) <= chunk_size:
                current_parts = [" ".join(tail_tokens), separator + unit_text]
                current_tokens = len(tail_tokens) + unit_tokens
            else:
                current_parts = [unit_text]
                current_tokens = unit_tokens
        else:
            current_parts = [unit_text]
            current_tokens = unit_tokens

    if current_parts:
        chunk_text = "".join(current_parts).strip()
        if chunk_text:
            packed.append(chunk_text)
    return packed


def chunk_pages(page_texts, chunk_size=650, overlap=120):
    chunks = []
    global_index = 0
    chunk_size = max(int(chunk_size), 1)
    overlap = min(max(int(overlap), 0), chunk_size - 1) if chunk_size > 1 else 0

    for page_number, page_text in enumerate(page_texts, start=1):
        text = normalize_text(page_text)
        if not text:
            continue

        units = _structured_units(text, chunk_size, overlap)
        page_chunks = _pack_units(units, chunk_size, overlap)

        for content in page_chunks:
            if not content:
                continue
            chunks.append(
                ChunkData(
                    page_number=page_number,
                    chunk_index=global_index,
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )
            )
            global_index += 1

    return chunks
