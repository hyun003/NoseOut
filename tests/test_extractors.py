"""문서·PDF·이미지에서 내용 꺼내기 (모델 없이)."""

import pytest

from noseout import extractors


def test_chunks_short_text_is_one_chunk():
    assert extractors.chunks("  hello \t world  ") == ["hello world"]
    assert extractors.chunks("   ") == []


def test_chunks_overlap_and_size():
    words = [f"word{i}" for i in range(2000)]
    text = " ".join(words)
    pieces = extractors.chunks(text)
    assert len(pieces) > 1
    assert all(len(p) <= extractors.CHUNK_SIZE for p in pieces)
    # 겹침: 다음 조각은 앞 조각 끝부분의 단어로 시작한다
    for a, b in zip(pieces, pieces[1:]):
        assert b.split()[0] in a.split()[-40:]
    # 빠지는 단어가 없다
    seen = set(" ".join(pieces).split())
    assert seen == set(words)
    # 단어 중간에서 끊지 않는다
    assert all(p.split()[0] in words and p.split()[-1] in words for p in pieces)


def test_chunks_limit():
    assert len(extractors.chunks("lorem ipsum " * 5000, limit=3)) == 3


def test_read_text_encodings(tmp_path):
    (tmp_path / "a.txt").write_bytes("주차 요금".encode("cp949"))
    (tmp_path / "b.txt").write_bytes("﻿요금 BOM".encode("utf-8"))
    (tmp_path / "c.txt").write_text("UTF-16 메모", encoding="utf-16")
    assert extractors.read_text(str(tmp_path / "a.txt")) == "주차 요금"
    assert extractors.read_text(str(tmp_path / "b.txt")) == "요금 BOM"
    assert extractors.read_text(str(tmp_path / "c.txt")) == "UTF-16 메모"


def test_html_and_docx(test_data):
    page = extractors.read_text(str(test_data / "documents" / "kickoff_meeting.html"))
    assert "Project Aurora kickoff meeting" in page and "offline mode" in page
    assert "console.log" not in page and "font-family" not in page
    invoice = extractors.read_text(str(test_data / "documents" / "invoice_INV-4821.docx"))
    assert "INVOICE INV-4821" in invoice
    assert "Web hosting (12 months)\t$480.00" in invoice   # 표도 읽는다


def test_pdf_text_pages(test_data, tmp_path):
    pages, total, title = extractors.pdf_pages(str(test_data / "documents" / "lease_agreement.pdf"), tmp_path)
    assert total == 3 and title == "Residential Lease - 48 Maple Street"
    assert [p.number for p in pages] == [1, 2, 3]
    assert all(p.image_path is None for p in pages)
    assert "SECTION 7. PETS" in pages[1].text


def test_pdf_scanned_page_is_rendered(test_data, tmp_path):
    pages, total, _ = extractors.pdf_pages(str(test_data / "documents" / "엘리베이터_점검_안내.pdf"), tmp_path)
    assert total == 1 and pages[0].text == ""
    from PIL import Image

    with Image.open(pages[0].image_path) as image:
        assert max(image.size) == extractors.RENDER_PIXELS


def test_bad_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 broken")
    with pytest.raises(extractors.ExtractError):
        extractors.pdf_pages(str(bad), tmp_path)


def test_image_info(test_data):
    rotated = extractors.image_info(str(test_data / "pictures" / "IMG_0042.jpg"))
    assert (rotated.width, rotated.height) == (600, 800)          # 저장은 800×600, EXIF 회전 적용
    assert rotated.taken == "2026:05:17 14:03:00"
    heic = extractors.image_info(str(test_data / "pictures" / "sunset_beach.heic"))
    assert (heic.width, heic.height) == (1200, 800)
    with pytest.raises(extractors.ExtractError, match="small image"):
        extractors.image_info(str(test_data / "pictures" / "app_icon.png"))
