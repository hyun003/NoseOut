"""키워드 검색용 텍스트 정규화. 원본 Sources/DigUpKit/SearchText.swift를 옮기고 한국어(한글)를 더했다."""

from __future__ import annotations

import re
import unicodedata

_DROP = {0x00AD, 0x061C, 0xFEFF, 0x0640, 0x0670} | set(range(0x200C, 0x2010)) | set(range(0x202A, 0x202F)) \
    | set(range(0x2060, 0x2065)) | set(range(0x2066, 0x206A)) | set(range(0x064B, 0x0660))
_ALEF = {0x0622, 0x0623, 0x0625, 0x0671}

_INDIC = (0x0900, 0x0DFF)
_ARABIC = (0x0600, 0x06FF)
_HANGUL = [(0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F)]


def normalized(text: str) -> str:
    """NFKC, 보이지 않는 서식 문자 제거, 아랍어 표기 통일 (원본과 같음)."""
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        code = ord(ch)
        if code == 0x200B:
            out.append(" ")
        elif code in _DROP:
            continue
        elif code in _ALEF:
            out.append("ا")
        else:
            out.append(ch)
    return "".join(out)


def inflects(word: str) -> bool:
    """뒤에 어미·조사가 붙는 문자(벵골어·힌디어·아랍어, 그리고 한국어)인지. 이런 단어는 접두어로 찾는다:
    "청구서"가 "청구서를"을 찾는다."""
    for ch in word:
        code = ord(ch)
        if _INDIC[0] <= code <= _INDIC[1] or _ARABIC[0] <= code <= _ARABIC[1]:
            return True
        if any(lo <= code <= hi for lo, hi in _HANGUL):
            return True
    return False


def has_words(text: str) -> bool:
    """공백이 아닌 글자 중 1/5 이상이 문자인지. 숫자 덤프는 키워드 검색만 한다 (원본과 같음)."""
    letters = others = 0
    for ch in text:
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] in "LM":
            letters += 1
        else:
            others += 1
    return letters > 0 and letters * 4 >= others


def words(text: str) -> list[str]:
    """정규화·소문자화한 단어들: 문자(L*)·숫자(N*)·결합 기호(M*)가 이어진 것. FTS 토크나이저와 같은 기준."""
    out, current = [], []
    for ch in normalized(text).lower():
        if unicodedata.category(ch)[0] in "LNM":
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def excerpt(text: str, limit: int = 240) -> str:
    """결과 목록용 한 줄."""
    start = text[: limit * 8]
    line = re.sub(r"\s+", " ", start).strip()
    return line[:limit] + "…" if len(line) > limit or len(start) < len(text) else line
