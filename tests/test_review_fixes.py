"""Codex 검토(ca753ed)에서 확인된 문제의 회귀 테스트. 모델 없이 가짜 임베더로 돈다."""

import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import requests

from noseout import crawler, extractors, rules, searcher as searcher_module
from noseout.indexer import Indexer
from noseout.searcher import Searcher, meaning
from noseout.store import DIM, IndexStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from make_test_data import wrap, write_text_pdf  # noqa: E402

LONG = ("The quarterly maintenance report lists every pump, valve and sensor checked on site, "
        "with notes on wear, replacement dates and the technician who signed off. ") * 3


class FakeEmbedder:
    """임베딩 대신 정해진 단위 벡터를 준다. fail_*로 오류를 흉내 낸다."""

    def __init__(self, fail_texts=None, fail_images=None):
        self.fail_texts, self.fail_images = fail_texts, fail_images
        self.rng = np.random.default_rng(0)

    def _unit(self):
        v = self.rng.normal(size=DIM).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed_texts(self, texts):
        if self.fail_texts:
            raise self.fail_texts
        return np.stack([self._unit() for _ in texts])

    def embed_image(self, path, budget=280):
        if self.fail_images:
            raise self.fail_images
        return self._unit()


def folder_with(tmp_path, files: dict[str, str]) -> Path:
    root = tmp_path / "chosen"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


# 1. critical: PDF 임시 폴더


def test_pdf_temp_never_deletes_existing_folder(tmp_path, test_data):
    db = tmp_path / "index.sqlite"
    victim = tmp_path / "index.tmp" / "1"        # 예전 코드가 rmtree하던 경로
    victim.mkdir(parents=True)
    (victim / "keep.txt").write_text("user data", encoding="utf-8")
    root = tmp_path / "chosen"
    root.mkdir()
    (root / "scan.pdf").write_bytes((test_data / "documents" / "엘리베이터_점검_안내.pdf").read_bytes())
    with IndexStore(db) as store:
        report = Indexer(store, FakeEmbedder()).sync([root])
    assert report.results[str((root / "scan.pdf").resolve())].startswith("done")
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert not list(tmp_path.glob("noseout-pdf-*"))   # 이번에 만든 임시 폴더는 지웠다


# 2, 3. high: 링크인 루트, AppData


def test_root_that_is_a_junction_is_refused(tmp_path):
    target = folder_with(tmp_path, {"a.txt": "hello"})
    link = tmp_path / "link"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("can't create a junction here")
    with pytest.raises(ValueError, match="link or junction"):
        crawler.crawl([link])


def test_appdata_is_forbidden():
    appdata = Path.home() / "AppData"
    assert rules.forbidden_root_reason(appdata).startswith("system folder")
    with pytest.raises(ValueError, match="system folder"):
        crawler.crawl([appdata])


# 4. high: 다른 폴더의 pending


def test_sync_reads_only_the_chosen_folders(tmp_path):
    a = folder_with(tmp_path / "a", {"a.txt": "alpha " * 50})
    b = folder_with(tmp_path / "b", {"b.txt": "beta " * 50})
    with IndexStore(tmp_path / "index.sqlite") as store:
        st = os.stat(a / "a.txt")
        store.upsert_file(str((a / "a.txt").resolve()), "doc", st.st_size, st.st_mtime, st.st_ino, st.st_dev)
        store.commit()                                  # A 색인이 중단되어 pending으로 남은 상태
        report = Indexer(store, FakeEmbedder()).sync([b])
        assert list(report.results) == [str((b / "b.txt").resolve())]
        state = store.db.execute("SELECT state FROM files WHERE path LIKE '%a.txt'").fetchone()[0]
        assert state == "pending"


# 5. high: 일시적 실패와 엔진 장애


def test_engine_failure_stops_sync_and_keeps_files_pending(tmp_path):
    root = folder_with(tmp_path, {"one.txt": "first " * 50, "two.txt": "second " * 50})
    with IndexStore(tmp_path / "index.sqlite") as store:
        with pytest.raises(requests.ConnectionError):
            Indexer(store, FakeEmbedder(fail_texts=requests.ConnectionError("server down"))).sync([root])
        states = [s for (s,) in store.db.execute("SELECT state FROM files")]
        assert states == ["pending", "pending"]
        assert store.counts()["segments"] == 0        # 반쯤 넣은 구간이 남지 않는다
        report = Indexer(store, FakeEmbedder()).sync([root])   # 엔진이 돌아오면 이어서
        assert sorted(r for r in report.results.values()) == ["done: 1 chunks", "done: 1 chunks"]


def test_locked_file_is_retried_next_sync(tmp_path, monkeypatch):
    root = folder_with(tmp_path, {"one.txt": "first " * 50})
    with IndexStore(tmp_path / "index.sqlite") as store:
        real = extractors.read_text
        monkeypatch.setattr(extractors, "read_text", lambda p: (_ for _ in ()).throw(PermissionError(13, "locked")))
        first = Indexer(store, FakeEmbedder()).sync([root])
        assert list(first.results.values())[0].startswith("retry later")
        monkeypatch.setattr(extractors, "read_text", real)
        second = Indexer(store, FakeEmbedder()).sync([root])
        assert list(second.results.values()) == ["done: 1 chunks"]


# 6. medium: 읽지 못한 폴더를 삭제로 보지 않음


def test_unreadable_folder_keeps_its_index(tmp_path, monkeypatch):
    root = folder_with(tmp_path, {"sub/inner.txt": "inner " * 50, "top.txt": "top " * 50})
    with IndexStore(tmp_path / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root])
        real = os.scandir

        def denied(path):
            if Path(path).name == "sub":
                raise PermissionError(5, "Access is denied")
            return real(path)

        monkeypatch.setattr(crawler.os, "scandir", denied)
        report = Indexer(store, FakeEmbedder()).sync([root])
        assert report.removed == 0
        assert store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


# 7, 8. medium: 깨진 PDF 페이지, 실패 시 되돌리기


def test_broken_pdf_page_fails_only_that_file(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    root = tmp_path / "chosen"
    root.mkdir()
    write_text_pdf(root / "a.pdf", [wrap(LONG)], "A")
    (root / "b.txt").write_text("next file " * 30, encoding="utf-8")

    def broken(pdf, index, render_to):
        raise pdfium.PdfiumError("Failed to load page.")

    monkeypatch.setattr(extractors, "_read_page", broken)
    with IndexStore(tmp_path / "index.sqlite") as store:
        report = Indexer(store, FakeEmbedder()).sync([root])
    results = {Path(p).name: r for p, r in report.results.items()}
    assert results["a.pdf"].startswith("failed: unreadable pdf page")
    assert results["b.txt"] == "done: 1 chunks"


def test_failed_file_leaves_no_partial_segments(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    write_text_pdf(root / "mixed.pdf", [wrap(LONG), ["short page"]], "Mixed")   # 2쪽은 글자가 적어 그림으로
    with IndexStore(tmp_path / "index.sqlite") as store:
        report = Indexer(store, FakeEmbedder(fail_images=extractors.ExtractError("bad render"))).sync([root])
        assert list(report.results.values()) == ["failed: bad render"]
        kinds = [k for (k,) in store.db.execute("SELECT kind FROM segments")]
        assert kinds == ["name"]                       # 1쪽 텍스트 구간도 되돌려졌다
        assert not Searcher(store).search("quarterly maintenance pump", None)


# 9. medium: 대소문자만 바뀐 폴더


def test_case_only_rename_keeps_one_row_per_file(tmp_path):
    root = folder_with(tmp_path, {"Notes/a.txt": "alpha " * 50})
    with IndexStore(tmp_path / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root])
        os.rename(root / "Notes", root / "notes")
        report = Indexer(store, FakeEmbedder()).sync([root])
        assert (report.added, report.removed) == (0, 0)
        assert store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


# 10. medium: 한 파일이 FTS 후보를 독차지하지 않음


def test_many_matching_pages_do_not_crowd_out_other_files(tmp_path):
    with IndexStore(tmp_path / "index.sqlite") as store:
        big = store.upsert_file(r"C:\x\big.pdf", "pdf", 1, 1.0, 1, 1)
        for page in range(1, 1101):
            store.add_segment(big, "page", "text", loc=page, text="alpha")
        doc = store.upsert_file(r"C:\x\note.txt", "doc", 1, 1.0, 2, 1)
        store.add_segment(doc, "chunk", "text", loc=1, text="alpha " + "filler words here " * 200)
        store.commit()
        s = Searcher(store)
        assert {Path(h.path).name for h in s.search("alpha", None)} == {"big.pdf", "note.txt"}
        assert [Path(h.path).name for h in s.search("alpha", None, kinds={"doc"})] == ["note.txt"]


# 11, 12. medium: HTML 이차 시간, 큰 텍스트


def test_html_without_closing_tags_is_linear(tmp_path):
    page = tmp_path / "bad.html"
    page.write_text("<script>" * 20000 + "x", encoding="utf-8")
    started = time.perf_counter()
    extractors.read_text(str(page))
    assert time.perf_counter() - started < 1.0       # 예전 정규식은 약 9초


def test_html_skips_script_and_keeps_text(tmp_path):
    page = tmp_path / "a.html"
    page.write_text("<html><head><title>T</title></head><body><p>Hello&nbsp;world</p>"
                    "<script>var x = '<p>no</p>';</script><div>Bye</div></body></html>", encoding="utf-8")
    text = extractors.read_text(str(page))
    assert "Hello\xa0world" in text and "Bye" in text and "var x" not in text and "T" not in text.split()


def test_big_text_reads_only_the_start_and_keeps_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(extractors, "MAX_TEXT_BYTES", 10)
    path = tmp_path / "big.txt"
    path.write_text("가나다라마바사", encoding="utf-8")         # 3바이트씩: 10바이트에서 글자가 잘린다
    assert extractors.read_text(str(path)) == "가나다"          # cp949로 잘못 읽지 않는다


# 13. low: 블록 단위 점수


def test_block_scores_match_full_product(tmp_path, monkeypatch):
    monkeypatch.setattr(searcher_module, "BLOCK_ROWS", 7)
    rng = np.random.default_rng(1)
    with IndexStore(tmp_path / "index.sqlite") as store:
        for i in range(30):
            f = store.upsert_file(rf"C:\x\{i}.png", "image", 1, 1.0, i, 1)
            v = rng.normal(size=DIM).astype(np.float32)
            store.add_segment(f, "image", "image", vector=v / np.linalg.norm(v))
        store.commit()
        s = Searcher(store)
        q = rng.normal(size=DIM).astype(np.float32)
        assert np.allclose(s._scores(q), s.matrix.astype(np.float32) @ q, atol=1e-5)


# meaning() 식 고정 (Codex: 문서와 식이 다르다는 지적 → 원본과 같은 식임을 수치로 고정)


def test_meaning_formula_matches_original():
    rng = np.random.default_rng(2)
    text, image = rng.normal(0.6, 0.05, 20), rng.normal(0.3, 0.05, 19)
    scores = np.concatenate([text, image]).astype(np.float32)
    modality = np.array(["text"] * 20 + ["image"] * 19)
    overall = scores.mean()
    centered = np.concatenate([text - text.mean(), image - overall])   # 20개는 자기 평균, 19개는 전체 평균
    expected = (centered - centered.mean()) / max(centered.std(), 0.02)
    assert np.allclose(meaning(scores, modality), expected, atol=1e-5)
    flat = meaning(np.full(10, 0.5, np.float32), np.array(["text"] * 10))
    assert np.allclose(flat, 0)                                        # 최소 편차 0.02로 0으로 나누지 않음


# Codex 검토 뒤 회귀 테스트에서 찾은 문제: DirEntry.stat()은 Windows에서 file ID를 0으로 준다


def test_file_id_is_real_and_fits_sqlite(tmp_path):
    root = folder_with(tmp_path, {"a.txt": "alpha " * 50})
    found = crawler.crawl([root]).files[0]
    st = os.stat(root / "a.txt")
    assert found.file_id == st.st_ino != 0
    with IndexStore(tmp_path / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root])
        inode, device = store.db.execute("SELECT inode, device FROM files").fetchone()
        assert inode != 0 and device != 0


def test_replaced_file_with_same_size_and_time_is_reindexed(tmp_path):
    root = folder_with(tmp_path, {"a.txt": "alpha " * 50})
    with IndexStore(tmp_path / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root])
        st = os.stat(root / "a.txt")
        (root / "a.txt").unlink()
        (root / "a.txt").write_text("omega " * 50, encoding="utf-8")   # 같은 크기
        os.utime(root / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns))  # 같은 수정 시각
        report = Indexer(store, FakeEmbedder()).sync([root])
        assert report.changed == 1                                     # file ID가 달라 다시 읽는다
