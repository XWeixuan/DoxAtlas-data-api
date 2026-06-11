from __future__ import annotations

import re
from typing import Any, Iterable, Optional


_META_CHARSET_RE = re.compile(br"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9_\-]+)", re.I)
_MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|â.|ï¼|è[\x80-\xbf]?.?|æ[\x80-\xbf]?.?|å[\x80-\xbf]?.?)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _declared_charset(headers: Any, content: bytes) -> Optional[str]:
    content_type = ""
    try:
        content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")
    except Exception:
        content_type = ""
    match = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type, flags=re.I)
    if match:
        return match.group(1)
    meta = _META_CHARSET_RE.search(content[:4096])
    if meta:
        return meta.group(1).decode("ascii", "ignore")
    return None


def _score_chinese_text(text: str) -> float:
    if not text:
        return -1000.0
    sample = text[:20000]
    length = max(len(sample), 1)
    chinese = len(re.findall(r"[\u4e00-\u9fff]", sample))
    mojibake = len(_MOJIBAKE_RE.findall(sample))
    replacements = sample.count("\ufffd")
    controls = len(_CONTROL_RE.findall(sample))
    punctuation = sample.count("，") + sample.count("。") + sample.count("：")
    return (chinese * 3.0 + punctuation * 0.5) - (mojibake * 8.0 + replacements * 12.0 + controls * 10.0) - (length * 0.0001)


def decode_chinese_response(response: Any, *, fallback_encodings: Optional[Iterable[str]] = None) -> str:
    content = bytes(getattr(response, "content", b"") or b"")
    if not content:
        return ""

    candidates: list[str] = []
    declared = _declared_charset(getattr(response, "headers", {}) or {}, content)
    for value in (
        declared,
        getattr(response, "encoding", None),
        getattr(response, "apparent_encoding", None),
        *(fallback_encodings or ()),
        "utf-8",
        "gb18030",
        "gbk",
        "gb2312",
        "big5",
        "cp950",
    ):
        if value:
            normalized = str(value).strip().lower()
            if normalized in {"iso-8859-1", "latin-1", "latin1", "windows-1252"}:
                continue
            if normalized not in candidates:
                candidates.append(normalized)

    best_text = ""
    best_score = -100000.0
    for encoding in candidates:
        try:
            decoded = content.decode(encoding, errors="strict")
        except Exception:
            continue
        decoded = repair_mojibake_text(decoded)
        score = _score_chinese_text(decoded)
        if score > best_score:
            best_text = decoded
            best_score = score

    if best_text:
        return best_text

    decoded = content.decode("utf-8", errors="replace")
    return repair_mojibake_text(decoded)


def repair_mojibake_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    best = text
    best_score = _score_chinese_text(text)
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = text.encode(encoding, errors="strict").decode("utf-8", errors="strict")
        except Exception:
            continue
        score = _score_chinese_text(repaired)
        if score > best_score + 5:
            best = repaired
            best_score = score
    return best


def clean_chinese_text(value: Any) -> str:
    text = repair_mojibake_text(value)
    text = _CONTROL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_garbled(value: Any) -> bool:
    text = str(value or "")
    if len(text.strip()) < 20:
        return False
    repaired = repair_mojibake_text(text)
    score = _score_chinese_text(repaired)
    sample = repaired[:2000]
    mojibake_hits = len(_MOJIBAKE_RE.findall(sample))
    chinese_hits = len(re.findall(r"[\u4e00-\u9fff]", sample))
    return mojibake_hits >= 4 and mojibake_hits > chinese_hits
