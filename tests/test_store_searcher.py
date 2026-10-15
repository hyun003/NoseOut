"""색인 저장과 검색 점수 (모델 없이, 만든 벡터로)."""

import numpy as np
import pytest

from noseout.searcher import Searcher, meaning, strong_count
from noseout.store import DIM, IndexStore


def unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def store(tmp_path):
    with IndexStore(tmp_path / "index.sqlite") as s:
        yield s


def add(store, path, kind, segments):
    file_id = store.upsert_file(path, kind, 1, 1.0, hash(path) & 0xFFFF, 1)
    for seg in segments:
        store.add_segment(file_id, **seg)
    store.finish(file_id, "done")
    store.commit()
    return file_id


def test_schema_and_float16_roundtrip(store):
    v = unit(1)
    file_id = add(store, r"C:\t\a.txt", "doc", [dict(kind="chunk", modality="text", loc=1, text="hello", vector=v)])
    blob = store.db.execute("SELECT vec FROM segments WHERE kind='chunk'").fetchone()[0]
    back = np.frombuffer(blob, np.float16).astype(np.float32)
    assert len(blob) == DIM * 2 and float(back @ v) > 0.9999
    assert store.counts()["vectors"] == 1
    # 이름 구간 + 본문 구간이 FTS에 있다
    assert store.db.execute("SELECT COUNT(*) FROM fts").fetchone()[0] == 2
    # 다시 upsert하면 대기 상태, 본문 구간은 지워지고 이름만 남는다
    store.upsert_file(r"C:\t\a.txt", "doc", 2, 2.0, 1, 1)
    assert store.db.execute("SELECT state FROM files WHERE id=?", (file_id,)).fetchone()[0] == "pending"
    assert store.db.execute("SELECT kind FROM segments WHERE file=?", (file_id,)).fetchall() == [("name",)]
    store.remove_file(file_id)
    assert store.db.execute("SELECT COUNT(*) FROM fts").fetchone()[0] == 0


def test_files_under_is_prefix_safe(store):
    add(store, r"C:\data\a.txt", "doc", [])
    add(store, r"C:\data2\b.txt", "doc", [])
    assert list(store.files_under(r"C:\data")) == [r"C:\data\a.txt"]


def test_meaning_centers_each_modality():
    rng = np.random.default_rng(0)
    text = rng.normal(0.60, 0.02, 50)
    image = rng.normal(0.30, 0.02, 50)
    scores = np.concatenate([text, image]).astype(np.float32)
    modality = np.array(["text"] * 50 + ["image"] * 50)
    z = meaning(scores, modality)
    # 모달별로 평균을 뺐으므로 이미지가 텍스트에 묻히지 않는다
    assert abs(z[:50].mean()) < 0.2 and abs(z[50:].mean()) < 0.2
    assert np.argmax(z[50:]) == np.argmax(image)


def test_meaning_small_groups_use_overall_mean():
    scores = np.array([0.9, 0.1, 0.1, 0.1], np.float32)
    z = meaning(scores, np.array(["text", "text", "image", "image"]))
    assert np.argmax(z) == 0


def test_search_by_meaning_keyword_and_kind(store):
    target = unit(10)
    add(store, r"C:\t\beach.jpg", "image", [dict(kind="image", modality="image", vector=target)])
    for i in range(30):
        add(store, rf"C:\t\other{i}.png", "image", [dict(kind="image", modality="image", vector=unit(100 + i))])
    add(store, r"C:\t\invoice.docx", "doc",
        [dict(kind="chunk", modality="text", loc=1, text="INVOICE INV-4821 total 1,250", vector=unit(11))])
    add(store, r"C:\t\메모.txt", "doc",
        [dict(kind="chunk", modality="text", loc=1, text="청구서를 이번 주에 보내 주세요", vector=unit(12))])
    s = Searcher(store)

    hits = s.search("beach", target)
    assert hits[0].path.endswith("beach.jpg") and hits[0].z >= 2

    hits = s.search("INV-4821", None)
    assert hits[0].path.endswith("invoice.docx") and hits[0].keyword_rank == 1
    assert "«" in hits[0].excerpt

    hits = s.search("청구서", None)          # 한국어는 접두어로: "청구서를"을 찾는다
    assert hits and hits[0].path.endswith("메모.txt")

    hits = s.search("invoi", None)           # 어디에도 없는 단어는 더 긴 단어의 앞부분으로
    assert hits and hits[0].path.endswith("invoice.docx")

    assert s.search("zzzz qqqq", None) == []   # 없는 단어: 결과 없음 (뜻 쪽은 test_index_search에서 실제 모델로)

    hits = s.search("beach", target, kinds={"doc"})
    assert all(h.kind == "doc" for h in hits)


def test_strong_count():
    class H:
        def __init__(self, score):
            self.score = score
    assert strong_count([H(4), H(3), H(1.9), H(1)]) == 2


def test_korean_query_with_particle_finds_word_without_it(store):
    add(store, r"C:\t\안내.txt", "doc", [dict(kind="chunk", modality="text", loc=1, text="주차 비용 정산 안내")])
    hits = Searcher(store).search("비용은", None)
    assert hits and hits[0].path.endswith("안내.txt")
