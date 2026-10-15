"""스크린샷·스캔 페이지의 글자 읽기: Windows 내장 OCR(Windows.Media.Ocr, winrt 패키지).

원본은 macOS에서 Apple Vision으로 읽고, Windows·Linux판(Platform/Portable)에는 OCR이 없다. 원본처럼 읽은 글자는
키워드 검색에만 쓴다(뜻은 그림 벡터가 맡는다: 글자 벡터를 넣었더니 스크린샷이 더 맞는 결과를 밀어냈다고 원본에 적혀 있다).

언어: 한국어 인식기를 먼저 쓰고, 읽은 글자에 한글이 거의 없으면 영어 인식기로 다시 읽는다. 한국어 인식기는 영어를
"DownIoad"처럼 잘못 읽고, 영어 인식기는 한국어를 읽지 못한다(합성 스크린샷·스캔으로 확인, 2026-10-13).
OCR이 없는 환경(winrt 미설치, 인식기 없음)에서는 빈 글자를 돌려준다: 색인은 그대로 되고 키워드만 빠진다.
"""

from __future__ import annotations

import asyncio
import io

from PIL import Image, ImageOps

PRIMARY = "ko"
FALLBACK = "en-US"
MIN_HANGUL_SHARE = 0.2

_engines: dict[str, object] | None = None


def _load_engines() -> dict[str, object]:
    global _engines
    if _engines is None:
        _engines = {}
        try:
            from winrt.windows.globalization import Language
            from winrt.windows.media.ocr import OcrEngine

            for tag in (PRIMARY, FALLBACK):
                if OcrEngine.is_language_supported(Language(tag)):
                    engine = OcrEngine.try_create_from_language(Language(tag))
                    if engine is not None:
                        _engines[tag] = engine
        except ImportError:
            pass
    return _engines


def available() -> list[str]:
    return list(_load_engines())


def text_of_image(path: str) -> str:
    """그림 파일의 글자. 읽을 수 없으면 빈 문자열(OCR은 부가 기능이라 색인을 실패시키지 않는다)."""
    engines = _load_engines()
    if not engines:
        return ""
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((_max_side(), _max_side()))
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
        return asyncio.run(_read(buffer.getvalue(), engines))
    except Exception:  # noqa: BLE001 (손상된 그림, OCR 내부 오류: 글자 없음으로)
        return ""


def _max_side() -> int:
    from winrt.windows.media.ocr import OcrEngine

    return int(OcrEngine.max_image_dimension)


async def _read(png: bytes, engines: dict[str, object]) -> str:
    bitmap = await _bitmap(png)
    text = ""
    if PRIMARY in engines:
        text = await _recognize(engines[PRIMARY], bitmap)
        if hangul_share(text) >= MIN_HANGUL_SHARE:
            return text
    if FALLBACK in engines:
        return await _recognize(engines[FALLBACK], bitmap)
    return text


async def _bitmap(png: bytes):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    return await decoder.get_software_bitmap_async()


async def _recognize(engine, bitmap) -> str:
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


def hangul_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "가" <= c <= "힣") / len(letters)
