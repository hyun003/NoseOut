"""긴 PDF·문서 나눠 읽기 (원본 IndexOptions.bigPDFPages/pdfStep/docStep, Indexer.finishLongFiles). 모델 없이."""

from pathlib import Path

import pytest
import requests

from noseout import extractors
from noseout.indexer import BIG_PDF_PAGES, DOC_STEP, PDF_STEP, Indexer
from noseout.searcher import Searcher
from noseout.store import IndexStore
from test_review_fixes import LONG, FakeEmbedder, write_text_pdf  # scripts/를 sys.path에 넣는다
from make_test_data import wrap  # noqa: E402


def pdf_with(path: Path, pages: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_pdf(path, [wrap(f"Page {n}. {LONG}") for n in range(1, pages + 1)], path.stem)
    return path


def long_text(path: Path, chunks_wanted: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(f"Part {n}. {LONG * 4}" for n in range(chunks_wanted)), encoding="utf-8")
    return path


def page_locs(store, kind):
    return [int(loc) for (loc,) in store.db.execute(
        "SELECT loc FROM segments WHERE kind = ? ORDER BY loc", (kind,))]


@pytest.fixture
def store(tmp_path):
    with IndexStore(tmp_path / "index.sqlite") as s:
        yield s


def test_long_pdf_first_step_then_rest(tmp_path, store):
    root = tmp_path / "chosen"
    pdf_with(root / "book.pdf", 120)
    indexer = Indexer(store, FakeEmbedder())
    report = indexer.sync([root], finish_long=False)
    assert list(report.results.values()) == [f"partial: pages 1-{PDF_STEP} of 120"]
    state, detail = store.db.execute("SELECT state, detail FROM files").fetchone()
    assert (state, detail) == ("partial", f"read to {PDF_STEP} of 120")
    assert page_locs(store, "page") == list(range(1, PDF_STEP + 1))
    # 첫 단계만 읽은 상태에서도 그 페이지들은 검색된다
    assert Searcher(store).search("quarterly maintenance", None)

    results = indexer.finish_long_files()
    assert list(results.values())[0].startswith("done")
    assert page_locs(store, "page") == list(range(1, 121))          # 빠짐도 중복도 없다
    assert store.db.execute("SELECT state FROM files").fetchone()[0] == "done"
    assert "read_to" not in store.info(1)


def test_pdf_up_to_big_pdf_pages_is_read_at_once(tmp_path, store):
    root = tmp_path / "chosen"
    pdf_with(root / "report.pdf", BIG_PDF_PAGES)
    report = Indexer(store, FakeEmbedder()).sync([root], finish_long=False)
    assert list(report.results.values()) == [f"done: {BIG_PDF_PAGES} pages, 0 as images"]
    assert page_locs(store, "page") == list(range(1, BIG_PDF_PAGES + 1))


def test_long_document_steps_match_whole_chunking(tmp_path, store):
    root = tmp_path / "chosen"
    path = long_text(root / "long.txt", 45)
    whole = extractors.chunks(extractors.read_text(str(path)))
    assert len(whole) > DOC_STEP
    indexer = Indexer(store, FakeEmbedder())
    first = indexer.sync([root], finish_long=False)
    assert list(first.results.values()) == [f"partial: chunks 1-{DOC_STEP} of {len(whole)}"]
    indexer.finish_long_files()
    stored = [t for (t,) in store.db.execute(
        "SELECT fts.body FROM fts JOIN segments s ON s.id = fts.rowid WHERE s.kind = 'chunk' ORDER BY s.loc")]
    assert stored == whole                                              # 나눠 읽어도 한 번에 자른 것과 같다


def test_sync_finishes_long_files_by_default(tmp_path, store):
    root = tmp_path / "chosen"
    pdf_with(root / "book.pdf", 70)
    report = Indexer(store, FakeEmbedder()).sync([root])
    assert list(report.results.values())[0].startswith("done")
    assert len(page_locs(store, "page")) == 70


def test_engine_failure_in_a_later_step_resumes_there(tmp_path, store, monkeypatch):
    root = tmp_path / "chosen"
    pdf_with(root / "book.pdf", 100)
    Indexer(store, FakeEmbedder()).sync([root], finish_long=False)
    with pytest.raises(requests.ConnectionError):
        Indexer(store, FakeEmbedder(fail_texts=requests.ConnectionError("down"))).finish_long_files()
    assert store.db.execute("SELECT state FROM files").fetchone()[0] == "partial"
    assert page_locs(store, "page") == list(range(1, PDF_STEP + 1))     # 실패한 단계는 되돌렸다
    Indexer(store, FakeEmbedder()).finish_long_files()
    assert page_locs(store, "page") == list(range(1, 101))


def test_broken_later_pages_keep_what_was_read(tmp_path, store, monkeypatch):
    import pypdfium2 as pdfium

    root = tmp_path / "chosen"
    pdf_with(root / "book.pdf", 100)
    indexer = Indexer(store, FakeEmbedder())
    indexer.sync([root], finish_long=False)
    real = extractors._read_page

    def broken_after_60(pdf, index, render_to):
        if index >= 60:
            raise pdfium.PdfiumError("Failed to load page.")
        return real(pdf, index, render_to)

    monkeypatch.setattr(extractors, "_read_page", broken_after_60)
    results = indexer.finish_long_files()
    assert list(results.values())[0].startswith("done: stopped at 60")
    assert page_locs(store, "page") == list(range(1, 61))
    state, detail = store.db.execute("SELECT state, detail FROM files").fetchone()
    assert state == "done" and detail.startswith("read to 60, not after")


def test_changed_long_file_starts_over(tmp_path, store):
    root = tmp_path / "chosen"
    path = pdf_with(root / "book.pdf", 80)
    indexer = Indexer(store, FakeEmbedder())
    indexer.sync([root], finish_long=False)
    pdf_with(path, 60)                                                  # 다 읽기 전에 파일이 바뀌었다
    report = indexer.sync([root], finish_long=False)
    assert report.changed == 1
    assert list(report.results.values()) == [f"partial: pages 1-{PDF_STEP} of 60"]
    indexer.finish_long_files()
    assert page_locs(store, "page") == list(range(1, 61))


def test_finish_long_files_respects_scope(tmp_path, store):
    a, b = tmp_path / "a", tmp_path / "b"
    pdf_with(a / "one.pdf", 70)
    pdf_with(b / "two.pdf", 70)
    indexer = Indexer(store, FakeEmbedder())
    indexer.sync([a], finish_long=False)
    indexer.sync([b], finish_long=False)
    indexer.sync([b])                                                   # b만 끝까지
    states = dict((Path(p).name, s) for p, s in store.db.execute("SELECT path, state FROM files"))
    assert states == {"one.pdf": "partial", "two.pdf": "done"}


def test_later_steps_do_not_set_the_scale(tmp_path, store):
    root = tmp_path / "chosen"
    pdf_with(root / "book.pdf", 120)
    long_text(root / "long.txt", 45)
    Indexer(store, FakeEmbedder()).sync([root])
    s = Searcher(store)
    rows = store.db.execute("SELECT s.kind, s.loc FROM segments s WHERE s.vec IS NOT NULL ORDER BY s.id").fetchall()
    expected = [not ((kind == "page" and loc > PDF_STEP) or (kind == "chunk" and loc > DOC_STEP))
                for kind, loc in rows]
    assert s.row_sets_scale.tolist() == expected
    assert s.row_sets_scale.sum() == PDF_STEP + DOC_STEP
