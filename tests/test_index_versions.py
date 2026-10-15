"""U2: 종류별 색인 버전과 이전 형식 색인 호환 (모델 없이, 가짜 임베더).

이전 형식 색인은 pytest 임시 폴더(.tmp)에 만든다: 현재 코드로 만든 뒤, 조사(docs/progress.md U2)에서 실제 2·3단계 코드로
확인한 차이대로 되돌린다 — OCR 구간이 없고, 스캔 페이지에 글자가 없고, meta는 설정 문자열 하나다.
실제 .index/는 쓰지 않는다.
"""

import shutil
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
import requests

from noseout import extractors, indexer as indexer_module
from noseout.indexer import (ENGINE, INDEX_FORMAT, LEGACY_STAGE2_3, LEGACY_STAGE4, VECTOR_SOURCE_MARKER,
                               IncompatibleIndex, Indexer, current_components)
from noseout.searcher import Searcher
from noseout.store import DIM, IndexStore
from test_review_fixes import LONG, FakeEmbedder, write_text_pdf
from make_test_data import wrap  # noqa: E402  (test_review_fixes가 scripts/를 sys.path에 넣는다)


class CountingEmbedder(FakeEmbedder):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = Counter()

    def embed_texts(self, texts):
        self.calls["text"] += len(texts)
        return super().embed_texts(texts)

    def embed_image(self, path, budget=280):
        self.calls["image"] += 1
        return super().embed_image(path, budget)

    def embed_audio(self, path):
        self.calls["audio"] += 1
        v = self.rng.normal(size=DIM).astype(np.float32)
        return v / np.linalg.norm(v)


@pytest.fixture
def folder(tmp_path, test_data):
    """스크린샷, 스캔 PDF, 텍스트 PDF, HTML, txt, 사진이 든 작은 합성 폴더 (test_data에서 복사)."""
    root = tmp_path / "chosen"
    (root / "shots").mkdir(parents=True)
    shutil.copy2(test_data / "pictures" / "Screenshots" / "Screenshot 2026-10-01 093000.png", root / "shots")
    shutil.copy2(test_data / "documents" / "엘리베이터_점검_안내.pdf", root)
    shutil.copy2(test_data / "documents" / "kickoff_meeting.html", root)
    shutil.copy2(test_data / "documents" / "tokyo_trip.md", root)
    shutil.copy2(test_data / "pictures" / "stop_sign.jpg", root)
    write_text_pdf(root / "lease.pdf", [wrap(LONG), wrap(LONG)], "Lease")
    return root


def build(root: Path, db: Path) -> IndexStore:
    store = IndexStore(db)
    Indexer(store, FakeEmbedder()).sync([root])
    return store


def downgrade_to_stage2_3(store: IndexStore) -> None:
    """2·3단계 코드가 만든 색인과 같게: OCR 키워드가 없고, meta는 옛 설정 문자열 하나."""
    db = store.db
    db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE kind IN ('ocr', 'page_image'))")
    db.execute("DELETE FROM segments WHERE kind = 'ocr'")
    db.execute("UPDATE segments SET excerpt = NULL WHERE kind = 'page_image'")
    db.execute("DELETE FROM meta WHERE key IN ('index_format', 'engine', 'components')")
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('vector_source', ?)", (LEGACY_STAGE2_3,))
    db.commit()


def snapshot(store: IndexStore) -> list[str]:
    return list(store.db.iterdump())


def vectors_by_file(store: IndexStore) -> dict[str, list[bytes]]:
    out = {}
    for path, vec in store.db.execute(
            "SELECT f.path, s.vec FROM segments s JOIN files f ON f.id = s.file WHERE s.vec IS NOT NULL ORDER BY s.id"):
        out.setdefault(Path(path).name, []).append(vec)
    return out


def structure(store: IndexStore) -> dict:
    out = {}
    for fid, path, state in store.db.execute("SELECT id, path, state FROM files"):
        kinds = Counter(k for (k,) in store.db.execute("SELECT kind FROM segments WHERE file = ?", (fid,)))
        words = store.db.execute("SELECT COUNT(*) FROM fts JOIN segments s ON s.id = fts.rowid "
                                 "WHERE s.file = ? AND s.kind != 'name'", (fid,)).fetchone()[0]
        out[Path(path).name] = (state, dict(kinds), words)
    return out


def test_new_index_records_engine_and_components(folder, tmp_path):
    with build(folder, tmp_path / "i.sqlite") as store:
        assert store.meta("index_format") == INDEX_FORMAT and store.meta("engine") == ENGINE
        assert store.meta("vector_source") == VECTOR_SOURCE_MARKER   # 옛 코드가 이 색인을 쓰지 못하게
        indexer = Indexer(store, CountingEmbedder())
        assert indexer.versions.changed_components == [] and indexer.versions.requeued == 0


def test_current_index_reopens_without_any_work(folder, tmp_path):
    with build(folder, tmp_path / "i.sqlite") as store:
        before = vectors_by_file(store)
        embedder = CountingEmbedder()
        report = Indexer(store, embedder).sync([folder])
        assert report.unchanged == 6 and report.results == {} and not embedder.calls
        assert vectors_by_file(store) == before


def test_stage4_index_is_accepted_without_reading_anything(folder, tmp_path):
    """4단계 형식(지금 .index/의 색인과 같은 형식): 문서·이미지·음성·영상 조건이 모두 같다."""
    with build(folder, tmp_path / "i.sqlite") as store:
        store.db.execute("DELETE FROM meta WHERE key IN ('index_format', 'engine', 'components')")
        store.db.execute("UPDATE meta SET value = ? WHERE key = 'vector_source'", (LEGACY_STAGE4,))
        store.db.commit()
        before = vectors_by_file(store)
        embedder = CountingEmbedder()
        indexer = Indexer(store, embedder)
        assert indexer.versions.migrated_from == "stage4"
        assert (indexer.versions.changed_components, indexer.versions.requeued) == ([], 0)
        assert indexer.sync([folder]).results == {} and not embedder.calls
        assert vectors_by_file(store) == before
        assert store.meta("index_format") == INDEX_FORMAT


def test_stage2_3_index_rereads_only_what_changed(folder, tmp_path):
    with build(folder, tmp_path / "fresh.sqlite") as fresh:
        expected = structure(fresh)
    with build(folder, tmp_path / "old.sqlite") as store:
        downgrade_to_stage2_3(store)
        old_vectors = vectors_by_file(store)
        embedder = CountingEmbedder()
        indexer = Indexer(store, embedder)
        assert indexer.versions.migrated_from == "stage2-3"
        assert sorted(indexer.versions.changed_components) == [
            "audio.vectors", "doc.html", "pdf.ocr", "screenshot.ocr", "video.vectors"]
        pending = sorted(Path(p).name for (p,) in store.db.execute("SELECT path FROM files WHERE state = 'pending'"))
        assert pending == ["Screenshot 2026-10-01 093000.png", "kickoff_meeting.html", "엘리베이터_점검_안내.pdf"]
        # 다시 읽기 전에도 이전 구간은 그대로 있어 검색된다
        assert vectors_by_file(store) == old_vectors
        assert Searcher(store).search("kickoff", None)

        report = indexer.sync([folder])
        assert sorted(Path(p).name for p in report.results) == pending
        # 다시 읽은 것만 임베딩: 스크린샷 그림 1, 스캔 페이지 그림 1, HTML 청크 1 (처음부터면 텍스트 PDF·md·사진까지)
        assert embedder.calls == Counter({"image": 2, "text": 1})
        assert structure(store) == expected                                  # 새로 만든 색인과 같은 구조
        untouched = ["lease.pdf", "tokyo_trip.md", "stop_sign.jpg"]
        after = vectors_by_file(store)
        assert all(after[name] == old_vectors[name] for name in untouched)   # 나머지 벡터는 그대로
        assert Searcher(store).search("CARD_DECLINED_51", None)              # 이제 OCR 키워드가 있다


def test_stage2_index_with_zero_file_ids_rereads_everything(folder, tmp_path):
    """2단계 색인은 file ID를 0으로 저장했다(DirEntry.stat). 받아들이되 모든 파일이 바뀐 것으로 보고 다시 읽는다."""
    with build(folder, tmp_path / "old.sqlite") as store:
        downgrade_to_stage2_3(store)
        store.db.execute("UPDATE files SET inode = 0")
        store.db.commit()
        report = Indexer(store, CountingEmbedder()).sync([folder])
        assert report.changed == 6 and report.unchanged == 0


@pytest.mark.parametrize("meta", [
    {"vector_source": "embeddinggemma-2@bfcd298 q8_0 llama.cpp-b11461 photo280 shot280 page280 chunk1800/200"},
    {"vector_source": "some other model"},
    {"index_format": INDEX_FORMAT, "engine": "embeddinggemma-2@other q4_0 llama.cpp-b12000", "components": "{}"},
    {"index_format": "99", "engine": ENGINE, "components": "{}"},
    {},
], ids=["unknown-legacy-settings", "unknown-legacy-model", "other-engine", "newer-format", "no-record-but-vectors"])
def test_incompatible_index_is_refused_and_left_unchanged(folder, tmp_path, meta):
    with build(folder, tmp_path / "i.sqlite") as store:
        store.db.execute("DELETE FROM meta")
        store.db.executemany("INSERT INTO meta(key, value) VALUES(?, ?)", list(meta.items()))
        store.db.commit()
        before = snapshot(store)
        with pytest.raises(IncompatibleIndex) as refused:
            Indexer(store, CountingEmbedder())
        assert "other vectors" in str(refused.value) and "left unchanged" in str(refused.value)
        assert "another --db" in str(refused.value)                           # 어떻게 하면 되는지 알려 준다
        assert snapshot(store) == before                                       # meta·파일·구간·벡터 모두 그대로
    assert (tmp_path / "i.sqlite").exists()                                    # 지우지 않는다


def test_changed_component_keeps_old_segments_until_reread_succeeds(folder, tmp_path):
    with build(folder, tmp_path / "i.sqlite") as store:
        components = current_components()
        components["doc.vectors"] = "chunk1000/100"                           # 문서 청크 조건이 달랐던 색인
        store.db.execute("UPDATE meta SET value = ? WHERE key = 'components'",
                         (__import__("json").dumps(components),))
        store.db.commit()
        old = vectors_by_file(store)
        # 엔진 장애: 문서 파일은 pending으로 남고 이전 구간은 그대로
        with pytest.raises(requests.ConnectionError):
            Indexer(store, FakeEmbedder(fail_texts=requests.ConnectionError("down"))).sync([folder])
        pending = {Path(p).name for (p,) in store.db.execute("SELECT path FROM files WHERE state = 'pending'")}
        assert {"kickoff_meeting.html", "tokyo_trip.md"} <= pending
        assert vectors_by_file(store) == old
        # 엔진이 돌아오면 문서만 다시 읽는다 (PDF는 kind가 pdf라 doc.vectors와 무관)
        embedder = CountingEmbedder()
        report = Indexer(store, embedder).sync([folder])
        assert sorted(Path(p).name for p in report.results) == sorted(pending)
        assert vectors_by_file(store)["lease.pdf"] == old["lease.pdf"]


def test_requeued_file_that_fails_keeps_its_previous_index(folder, tmp_path, monkeypatch):
    with build(folder, tmp_path / "i.sqlite") as store:
        components = current_components()
        components["pdf.vectors"] = "page140 text4000 render1400"
        store.db.execute("UPDATE meta SET value = ? WHERE key = 'components'", (__import__("json").dumps(components),))
        store.db.commit()
        old = vectors_by_file(store)

        def broken(*args, **kwargs):
            raise extractors.ExtractError("unreadable pdf page: simulated")

        monkeypatch.setattr(extractors, "pdf_pages", broken)
        report = Indexer(store, CountingEmbedder()).sync([folder])
        results = {Path(p).name: r for p, r in report.results.items()}
        assert results["lease.pdf"].startswith("done: kept previous index")
        assert vectors_by_file(store)["lease.pdf"] == old["lease.pdf"]
        state, detail = store.db.execute("SELECT state, detail FROM files WHERE path LIKE '%lease.pdf'").fetchone()
        assert state == "done" and detail.startswith("kept previous index")


def test_changed_file_still_fails_without_old_content(folder, tmp_path, monkeypatch):
    """파일 내용이 바뀐 경우는 이전 구간이 이미 틀린 내용이므로 지금처럼 failed (이전 색인을 남기지 않는다)."""
    with build(folder, tmp_path / "i.sqlite") as store:
        write_text_pdf(folder / "lease.pdf", [wrap(LONG)], "Lease v2")
        monkeypatch.setattr(extractors, "pdf_pages",
                            lambda *a, **k: (_ for _ in ()).throw(extractors.ExtractError("broken")))
        report = Indexer(store, CountingEmbedder()).sync([folder])
        assert {Path(p).name: r for p, r in report.results.items()}["lease.pdf"] == "failed: broken"


def test_legacy_table_matches_the_old_code():
    """옛 형식 문자열은 2·3·4단계 코드의 VECTOR_SOURCE 그대로다(git show로 확인한 값)."""
    assert LEGACY_STAGE2_3 == ("embeddinggemma-2@bfcd298 q8_0 llama.cpp-b11461 photo140 shot280 page280 "
                               "chunk1800/200")
    assert LEGACY_STAGE4 == LEGACY_STAGE2_3 + " frame70/3s audio30/25s"
    # 4단계와 현재의 문서·이미지·음성·영상 조건이 같다는 표의 주장
    assert indexer_module.LEGACY_COMPONENTS[LEGACY_STAGE4] == current_components()


def test_cli_explains_an_incompatible_index(folder, tmp_path):
    """CLI는 traceback 대신 안내를 출력하고 2로 끝난다. 엔진을 띄우기 전에 거부하므로 모델이 없어도 된다."""
    import os
    import subprocess
    import sys

    db = tmp_path / "i.sqlite"
    with build(folder, db) as store:
        store.db.execute("UPDATE meta SET value = 'embeddinggemma-2@other q4_0 llama.cpp-b12000' WHERE key = 'engine'")
        store.db.commit()
        before = snapshot(store)
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-m", "noseout", "--db", str(db), "index", str(folder)],
                          capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONIOENCODING": "utf-8"})
    assert done.returncode == 2
    assert "left unchanged" in done.stderr and "another --db" in done.stderr and "Traceback" not in done.stderr
    with IndexStore(db) as store:
        assert snapshot(store) == before


def test_empty_index_without_record_is_new(tmp_path):
    """버전 기록도 벡터도 없는 색인(예: 만들다 멈춘 빈 파일)은 새 색인으로 받는다."""
    with IndexStore(tmp_path / "i.sqlite") as store:
        store.db.execute("DELETE FROM meta")
        store.db.commit()
        assert Indexer(store, CountingEmbedder()).versions.created
        assert store.meta("engine") == ENGINE
