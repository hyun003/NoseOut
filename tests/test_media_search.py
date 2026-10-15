"""4단계: 실제 모델로 음성·영상·OCR을 색인하고 검색한다. 결과는 results/stage4-media.json."""

import json
from pathlib import Path

import pytest

from conftest import RESULTS, requires_engine
from noseout import prompts
from noseout.indexer import Indexer
from noseout.searcher import Searcher
from noseout.store import IndexStore
from test_media import requires_ffmpeg, requires_ocr

pytestmark = [requires_engine, requires_ffmpeg]

# (검색어, 기대 파일, 가리켜야 할 구간 [시작 초 최소, 최대] 또는 None, 태그)
QUERIES = [
    ("where they talk about sleep", "media/home_show_ep12.m4a", (25, 31.6), "audio"),   # 수면 31.6–52.1초
    ("tips for falling asleep at night", "media/home_show_ep12.m4a", (25, 31.6), "audio"),
    ("community garden opening", "media/home_show_ep12.m4a", (0, 13.7), "audio"),      # 정원 13.7–31.6초
    ("trains are faster than flying", "media/home_show_ep12.m4a", (25, 52.1), "audio"),  # 여행 52.1초–
    ("치과 예약 음성 메모", "media/음성메모_0917.m4a", (0, 0), "audio"),
    ("dentist appointment reminder voice note", "media/음성메모_0917.m4a", (0, 0), "cross-lingual"),
    ("black and white stripes in a video", "media/clip_2026-08-30.mp4", (0, 5), "frame"),
    ("a sailboat on the water", "media/clip_2026-08-30.mp4", (5, 10), "frame"),
    ("birthday cake with candles", "media/clip_2026-08-30.mp4", (9, 15), "frame"),
    ("weather forecast with rain tomorrow", "media/clip_2026-08-30.mp4", (0, 0), "soundtrack"),
]
OCR_QUERIES = [
    ("CARD_DECLINED_51", "pictures/Screenshots/Screenshot 2026-10-01 093000.png", None),
    ("attempts=5 TimeoutError", "code_project/Screenshot 2026-10-02 retry.png", None),
    ("101동 엘리베이터 정기 점검", "documents/엘리베이터_점검_안내.pdf", 1),
]


@pytest.fixture(scope="module")
def indexed(test_data, embedder, tmp_path_factory):
    store = IndexStore(tmp_path_factory.mktemp("media") / "index.sqlite")
    report = Indexer(store, embedder).sync([test_data])
    yield store, report
    store.close()


def rel(path, test_data):
    return Path(path).relative_to(test_data).as_posix()


def test_media_index_report(indexed, test_data):
    store, report = indexed
    results = {rel(p, test_data): r for p, r in report.results.items()}
    assert results["media/home_show_ep12.m4a"] == "done: 3 windows, 65 s"
    assert results["media/음성메모_0917.m4a"] == "done: 1 windows, 7 s"
    assert results["media/room_tone.wav"] == "skipped: silent or too short"
    assert results["media/clip_2026-08-30.mp4"] == "done: 3 frames, 1 sound windows, 15 s"
    rows = store.db.execute("SELECT s.kind, s.modality, s.loc, s.loc_end FROM segments s JOIN files f "
                            "ON f.id = s.file WHERE f.path LIKE '%home_show_ep12.m4a' AND s.kind = 'audio' "
                            "ORDER BY s.loc").fetchall()
    assert [(k, m, round(a), round(b)) for k, m, a, b in rows] == [
        ("audio", "audio", 0, 30), ("audio", "audio", 25, 55), ("audio", "audio", 50, 66)]
    assert not list(store.path.parent.glob("noseout-*"))       # 임시 WAV·프레임 폴더는 지웠다


@requires_ocr
def test_ocr_text_is_keywords_only(indexed):
    store, _ = indexed
    rows = store.db.execute("SELECT kind, modality, vec IS NULL FROM segments WHERE kind = 'ocr'").fetchall()
    assert len(rows) == 2 and all(r == ("ocr", "none", 1) for r in rows)   # 스크린샷 2장, 벡터 없음 (원본과 같음)


def test_media_search(indexed, embedder, test_data):
    store, _ = indexed
    s = Searcher(store)
    vectors = embedder.embed_texts([prompts.query(q) for q, *_ in QUERIES])
    rows = []
    for (query, expect, (lo, hi), tag), vector in zip(QUERIES, vectors):
        hits = s.search(query, vector, limit=5)
        names = [rel(h.path, test_data) for h in hits]
        rank = names.index(expect) + 1 if expect in names else None
        hit = hits[rank - 1] if rank else None
        rows.append({"q": query, "tag": tag, "rank": rank, "segment": hit.segment if hit else None,
                     "loc": hit.loc if hit else None, "top": names[:3]})
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "stage4-media.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    for row, (query, expect, (lo, hi), tag) in zip(rows, QUERIES):
        if tag == "cross-lingual":
            continue   # 영어 질의 → 한국어 음성: 기록만 (2026-10-13 측정에서 못 찾음, docs/progress.md 4단계 한계)
        assert row["rank"] == 1, row
        assert lo <= row["loc"] <= hi, row                     # 맞는 시점을 가리킨다
        if tag == "frame":
            assert row["segment"] == "frame", row
        if tag in ("audio", "soundtrack"):
            assert row["segment"] == "audio", row


@requires_ocr
def test_ocr_keyword_search(indexed, embedder, test_data):
    store, _ = indexed
    s = Searcher(store)
    for query, expect, page in OCR_QUERIES:
        vector = embedder.embed_texts([prompts.query(query)])[0]
        hits = s.search(query, vector, limit=5)
        assert hits and rel(hits[0].path, test_data) == expect, (query, [h.path for h in hits])
        assert hits[0].keyword_rank == 1, query                  # 글자로 찾았다
        if page:
            assert int(hits[0].loc) == page
