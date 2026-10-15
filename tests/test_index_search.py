"""실제 모델로 test_data를 색인하고 검색한다. 결과는 results/stage2-search.json에 남는다."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from conftest import RESULTS, requires_engine
from noseout import prompts
from noseout.indexer import Indexer
from noseout.searcher import Searcher
from noseout.store import IndexStore

pytestmark = requires_engine

# (검색어, 기대 파일, 기대 페이지 또는 None)
QUERIES = [
    ("the clause about pets in the lease", "documents/lease_agreement.pdf", 2),
    ("when can the tenant end the lease early", "documents/lease_agreement.pdf", 3),
    ("Tokyo travel plans", "documents/tokyo_trip.md", None),
    ("Kamakura Great Buddha day trip", "documents/tokyo_trip.md", None),
    ("김치찌개 만드는 법", "documents/김치찌개_레시피.txt", None),
    ("pork and kimchi stew recipe", "documents/김치찌개_레시피.txt", None),
    ("INV-4821", "documents/invoice_INV-4821.docx", None),
    ("invoice for web hosting", "documents/invoice_INV-4821.docx", None),
    ("project kickoff meeting budget", "documents/kickoff_meeting.html", None),
    ("parking fee payment", "documents/주차_안내_cp949.txt", None),
    ("주차 요금 정산", "documents/주차_안내_cp949.txt", None),
    ("SERIAL-77Q3X", "documents/sensor_dump.txt", None),
    ("엘리베이터 점검 안내문", "documents/엘리베이터_점검_안내.pdf", 1),
    ("payment declined error", "pictures/Screenshots/Screenshot 2026-10-01 093000.png", None),
    ("a red stop sign", "pictures/stop_sign.jpg", None),
    ("night sky with a crescent moon", "pictures/night_sky.png", None),
    ("sunset over the sea", "pictures/sunset_beach.heic", None),
    ("바다 위 노을 사진", "pictures/sunset_beach.heic", None),
    ("bar chart", "pictures/quarterly_chart.png", None),
    ("a red circle", "pictures/red_circle.webp", None),
    ("a green arrow pointing up", "pictures/IMG_0042.jpg", None),
    ("python retry with exponential backoff", "code_project/Screenshot 2026-10-02 retry.png", None),
]


def digest(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def indexed(test_data, embedder, tmp_path_factory):
    before = digest(test_data)
    db = tmp_path_factory.mktemp("index") / "index.sqlite"
    store = IndexStore(db)
    report = Indexer(store, embedder).sync([test_data])
    yield store, report, before
    store.close()


def test_index_report(indexed, test_data):
    store, report, before = indexed
    assert report.added == 28 and report.removed == 0   # 2단계 17 + 3단계 7 + 4단계 음성·영상 4
    results = {Path(p).relative_to(test_data).as_posix(): r for p, r in report.results.items()}
    assert results["pictures/app_icon.png"] == "skipped: small image 64x64"
    assert results["documents/lease_agreement.pdf"] == "done: 3 pages, 0 as images"
    assert results["documents/엘리베이터_점검_안내.pdf"] == "done: 1 pages, 1 as images, 1 read by OCR"  # OCR: 4단계
    assert results["documents/sensor_dump.txt"].endswith("keywords only")   # 숫자 덤프는 벡터 없음
    assert all(r == "done" or r.startswith(("done:", "skipped: small", "skipped: silent")) for r in results.values())
    counts = store.counts()
    assert counts["files"]["pdf"] == {"done": 3}
    # 긴 파일은 나눠 읽었지만 결국 끝까지 들어갔다
    pages = dict(store.db.execute(
        "SELECT f.path, COUNT(*) FROM segments s JOIN files f ON f.id = s.file WHERE s.kind IN ('page', 'chunk') "
        "GROUP BY f.id").fetchall())
    by_name = {Path(p).name: n for p, n in pages.items()}
    assert by_name["facilities_manual.pdf"] == 120 and by_name["handbook.txt"] == 64 and by_name["사내_규정.txt"] == 54
    # 건너뛴 파일은 색인에 없다
    paths = [p for (p,) in store.db.execute("SELECT path FROM files")]
    assert not any(x in p for p in paths for x in ("id_rsa", ".hidden", "node_modules", "NOTES.md", ".key"))


def test_test_data_is_untouched(indexed, test_data):
    _, _, before = indexed
    assert digest(test_data) == before


def test_search_quality(indexed, embedder, test_data):
    store, _, _ = indexed
    searcher = Searcher(store)
    vectors = embedder.embed_texts([prompts.query(q) for q, _, _ in QUERIES])
    rows, reciprocal = [], []
    for (query, expected, page), vector in zip(QUERIES, vectors):
        hits = searcher.search(query, vector, limit=10)
        names = [Path(h.path).relative_to(test_data).as_posix() for h in hits]
        rank = names.index(expected) + 1 if expected in names else None
        reciprocal.append(1 / rank if rank else 0)
        hit = hits[rank - 1] if rank else None
        rows.append({"query": query, "expected": expected, "rank": rank, "top": names[:3],
                     "page": hit.loc if hit else None, "z": round(hit.z, 2) if hit and hit.z else None,
                     "keyword_rank": hit.keyword_rank if hit else None})
        if page is not None and rank == 1:
            assert int(hit.loc) == page, f"{query}: page {hit.loc}, expected {page}"
    top1 = sum(r["rank"] == 1 for r in rows) / len(rows)
    summary = {"queries": len(rows), "top1": round(top1, 3), "mrr": round(sum(reciprocal) / len(rows), 3),
               "found_in_top10": sum(r["rank"] is not None for r in rows), "rows": rows}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "stage2-search.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    # 파일 이름에 검색어 단어가 있는 경우가 많으므로, 단어 없이 뜻만으로도 찾는지 따로 본다:
    # 번호 검색(SERIAL-…)을 빼고는 모든 정답 구간이 뜻 기준(z ≥ 2)을 넘어야 한다.
    meaning_only = [r for r in rows if r["rank"] == 1 and r["z"] is not None and r["z"] >= 2]
    summary["meaning_alone_passes_gate"] = len(meaning_only)
    (RESULTS / "stage2-search.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    assert len(meaning_only) >= len(rows) - 1
    missed = [r for r in rows if r["rank"] != 1]
    assert top1 >= 0.9, f"top-1 {top1}: {missed}"
    assert all(r["rank"] is not None and r["rank"] <= 3 for r in rows), missed


def test_nothing_found_for_nonsense(indexed, embedder):
    store, _, _ = indexed
    vector = embedder.embed_texts([prompts.query("qwzx plorbt vrenk")])[0]
    hits = Searcher(store).search("qwzx plorbt vrenk", vector)
    assert len(hits) <= 1   # 엉뚱한 검색어에 결과가 쏟아지지 않는다


def test_incremental_sync(test_data, embedder, tmp_path):
    folder = tmp_path / "copy"
    (folder / "documents").mkdir(parents=True)
    for name in ["tokyo_trip.md", "김치찌개_레시피.txt", "invoice_INV-4821.docx"]:
        shutil.copy2(test_data / "documents" / name, folder / "documents" / name)
    with IndexStore(tmp_path / "index.sqlite") as store:
        indexer = Indexer(store, embedder)
        first = indexer.sync([folder])
        assert (first.added, first.changed, first.removed) == (3, 0, 0)
        again = indexer.sync([folder])
        assert (again.added, again.changed, again.removed, again.unchanged) == (0, 0, 0, 3)
        assert again.results == {}   # 바뀐 것이 없으면 모델을 쓰지 않는다

        (folder / "documents" / "tokyo_trip.md").write_text("Osaka food tour: takoyaki and okonomiyaki.",
                                                            encoding="utf-8")
        (folder / "documents" / "김치찌개_레시피.txt").unlink()
        (folder / "documents" / "new_note.txt").write_text("Dentist appointment on Thursday at 4 pm.",
                                                            encoding="utf-8")
        third = indexer.sync([folder])
        assert (third.added, third.changed, third.removed) == (1, 1, 1)

        searcher = Searcher(store)
        vector = embedder.embed_texts([prompts.query("Osaka street food")])[0]
        assert searcher.search("Osaka street food", vector)[0].path.endswith("tokyo_trip.md")
        assert not searcher.search("김치찌개", None)      # 지운 파일의 단어는 남지 않는다


def test_other_vectors_are_not_mixed(tmp_path, embedder):
    with IndexStore(tmp_path / "index.sqlite") as store:
        store.set_meta("vector_source", "some other model")
        with pytest.raises(RuntimeError, match="other vectors"):
            Indexer(store, embedder)
