"""하이브리드 검색: 뜻(EmbeddingGemma 2 벡터) + 단어(FTS5). 원본 Searcher.swift를 옮겼다.
코드 색인(code.sqlite)의 검색기(`code=True`)만 원본의 코드 동작을 쓴다: 이름은 한 구절로 찾고, 단어 가중치는
CODE_WORD_WEIGHT, 결과마다 열 줄(focus)과 코드 문맥을 정한다. 일반 검색의 점수·setsScale은 그대로다.

- 뜻: 모든 벡터와 코사인 → 모달(text/image)별 평균을 빼고 전체 표준편차로 나눈 z → 파일별 최고 구간
- 단어: 검색어의 단어를 FTS5로 찾고, 파일마다 검색어 단어 중 몇 %가 있는지(coverage)
- 점수 = 뜻 가중치 × max(z, 0) + 단어 가중치 × coverage^1.5 (+ 질의가 말한 종류면 1)
- 후보: z ≥ 2(눈에 띄는 뜻) 또는 단어 절반 이상
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

from . import code as code_files
from . import search_text
from .indexer import BIG_PDF_PAGES, DOC_STEP, PDF_STEP
from .store import DIM, IndexStore

WORD_WEIGHT = 2.0
# 코드의 단어는 어디에나 있다("app", "running"): 원본이 실제 코드 평가(2026-10-09, 27개 질의)로 정한 값. 코드 색인에만.
CODE_WORD_WEIGHT = 1.0
Z_GATE = 2.0
BLOCK_ROWS = 16384

STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or", "my", "me", "that", "this",
    "these", "those", "from", "about", "where", "what", "which", "who", "when", "is", "was", "are", "were", "be",
    "it", "its", "some", "any", "find", "show", "file", "files", "photo", "photos", "picture", "image", "images",
    "screenshot", "screenshots", "video", "videos", "pdf", "document", "documents", "recording",
    # 한국어 (원본은 벵골어·아랍어; 이 PC 사용자를 위해 한국어로 바꿈)
    "그", "이", "저", "것", "및", "또는", "에서", "있는", "관한", "대한", "찾아줘", "찾기", "보여줘",
    "사진", "이미지", "그림", "스크린샷", "캡처", "화면", "문서", "파일", "영상", "동영상", "녹음",
}

KIND_WORDS = {
    "screenshot": "screenshot", "screenshots": "screenshot", "photo": "image", "photos": "image",
    "picture": "image", "pictures": "image", "image": "image", "images": "image", "pdf": "pdf", "pdfs": "pdf",
    "paper": "pdf", "papers": "pdf", "document": "doc", "documents": "doc", "note": "doc", "notes": "doc",
    "video": "video", "videos": "video", "clip": "video", "movie": "video", "audio": "audio", "song": "audio",
    "podcast": "audio", "voice": "audio", "sound": "audio", "recording": "audio",
    "영상": "video", "동영상": "video", "비디오": "video", "녹음": "audio", "음성": "audio", "노래": "audio",
    "오디오": "audio",
    "스크린샷": "screenshot", "캡처": "screenshot", "사진": "image", "이미지": "image", "그림": "image",
    "문서": "doc", "메모": "doc", "노트": "doc",
}


@dataclass
class Hit:
    path: str
    kind: str
    score: float
    z: float | None
    cosine: float | None
    keyword_rank: int | None
    segment: str
    segment_id: int
    loc: float | None
    excerpt: str | None
    info: dict
    loc_end: float | None = None
    focus: int | None = None                         # 코드: 열 줄 (정의 줄이나 검색어가 든 첫 줄)
    context: list[tuple[int, str]] | None = None     # 코드: 보여 줄 (줄 번호, 줄)


# 한국어 검색어 끝의 조사. 문서 쪽은 접두어 검색("청구서*" → "청구서를")으로 맞추지만, 검색어에 조사가 붙으면
# ("비용은") 조사 없는 문서 단어("비용")를 못 찾는다. 긴 것부터 떼고, 두 글자 이상 남을 때만 뗀다.
KO_PARTICLES = ("에서는", "으로는", "에게서", "에서", "에게", "으로", "까지", "부터", "처럼", "보다", "은", "는", "이",
                "가", "을", "를", "에", "의", "도", "와", "과", "로", "만")


def strip_particle(word: str) -> str:
    if not search_text.inflects(word):
        return word
    for particle in KO_PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def terms(text: str) -> list[str]:
    seen: set[str] = set()
    out = []
    for word in search_text.words(text):
        word = strip_particle(word)
        if len(word) >= 2 and word not in STOPWORDS and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def patterns(words: list[str], prefix_last: bool = False) -> list[str]:
    return [f'"{w}"' + ("*" if (prefix_last and i == len(words) - 1) or search_text.inflects(w) else "")
            for i, w in enumerate(words)]


def is_lookup(words: list[str]) -> bool:
    """코드·번호("INV-4821", "4815162342")로만 된 검색어: 단어를 두 배, 뜻을 절반으로."""
    return bool(words) and all(any(c.isdigit() for c in w) and (any(c.isalpha() for c in w) or len(w) >= 4)
                               for w in words)


def kind_hints(text: str) -> set[str]:
    hints = set()
    for word in search_text.words(text):
        kind = KIND_WORDS.get(word)
        if kind is None:
            kind = next((k for key, k in KIND_WORDS.items()
                         if search_text.inflects(key) and len(key) >= 2 and word.startswith(key)), None)
        if kind:
            hints.add(kind)
    return hints


def meaning(scores: np.ndarray, modality: np.ndarray, sets_scale: np.ndarray | None = None) -> np.ndarray:
    """원본 Searcher.meaning과 같은 식:
        c = 코사인 − (그 모달의 평균, 단 모달이 20개 미만이면 전체 평균)
        z = (c − mean(c)) / max(std(c), 0.02)
    작은 모달이 섞이면 mean(c)가 0이 아니므로 마지막에 한 번 더 중심을 맞춘다(원본도 같다).
    평균과 표준편차는 `sets_scale`인 행에서만 구한다: 긴 파일의 뒷부분(나중 단계에서 읽은 페이지·청크)은 점수는 받지만
    척도를 움직이지 않는다. 책 한 권의 수백 쪽이 모든 검색의 텍스트 평균을 끌어내리지 않게 (원본 setsScale)."""
    scale = np.ones(len(scores), bool) if sets_scale is None else sets_scale
    if not scale.any():
        scale = np.ones(len(scores), bool)
    overall = float(scores[scale].mean()) if len(scores) else 0.0
    centered = scores.astype(np.float64).copy()
    for m in np.unique(modality):
        mask = modality == m
        counted = mask & scale
        centered[mask] -= scores[counted].mean() if counted.sum() >= 20 else overall
    deviation = max(float(centered[scale].std()), 0.02)
    return (centered - centered[scale].mean()) / deviation


class Searcher:
    def __init__(self, store: IndexStore, code: bool = False):
        self.store = store
        self.code = code
        self.reload()

    def reload(self) -> None:
        rows = self.store.db.execute(
            """SELECT s.id, s.file, s.modality, s.vec, s.kind, s.loc, f.kind,
                      CAST(json_extract(f.info, '$.pages') AS INTEGER)
               FROM segments s JOIN files f ON f.id = s.file WHERE s.vec IS NOT NULL ORDER BY s.id""").fetchall()
        self.row_segment = np.array([r[0] for r in rows], dtype=np.int64)
        self.row_file = np.array([r[1] for r in rows], dtype=np.int64)
        self.row_modality = np.array([r[2] for r in rows])
        # 긴 파일의 뒷부분(첫 단계 뒤에 읽은 것)인지: 원본 Searcher.reload와 같은 판정
        self.row_sets_scale = np.array([
            not (seg_kind != "chunk" and (pages or 0) > BIG_PDF_PAGES and (loc or 0) > PDF_STEP
                 if file_kind == "pdf" else seg_kind == "chunk" and (loc or 0) > DOC_STEP)
            for _, _, _, _, seg_kind, loc, file_kind, pages in rows], dtype=bool)
        self.matrix = np.empty((len(rows), DIM), np.float16)
        for i, r in enumerate(rows):
            self.matrix[i] = np.frombuffer(r[3], dtype=np.float16)
        self.file_kind = dict(self.store.db.execute("SELECT id, kind FROM files"))

    def search(self, text: str, query_vector: np.ndarray | None, kinds: set[str] | None = None, limit: int = 20,
               z_gate: float = Z_GATE, prefix_last: bool = False, allowed_files: set[int] | None = None) -> list[Hit]:
        def allowed(file):
            return ((kinds is None or self.file_kind.get(file) in kinds)
                    and (allowed_files is None or file in allowed_files))

        if allowed_files is not None:
            # 결과 상한 뒤에 제외하면 남아야 할 후보도 사라진다. 점수·setsScale은 전체 행 기준을 유지한다.
            self.store.db.create_function("noseout_allowed", 1, lambda file: file in allowed_files)

        best: dict[int, tuple[float, float, int]] = {}   # file → (z, cosine, row)
        if query_vector is not None and len(self.row_file):
            scores = self._scores(np.asarray(query_vector, np.float32))
            z = meaning(scores, self.row_modality, self.row_sets_scale)
            for row in np.argsort(-z):
                file = int(self.row_file[row])
                if file not in best and allowed(file):
                    best[file] = (float(z[row]), float(scores[row]), int(row))

        words = terms(text)
        name = code_files.identifier(text) if self.code else None
        # 코드의 이름("retry_with_backoff")은 쓴 그대로 붙은 한 구절로 찾는다 (원본 CodeLookup.phrase)
        pats = [code_files.phrase(name)] if name else patterns(words, prefix_last)
        for i, w in enumerate(words if not name else []):  # 어디에도 없는 단어는 더 긴 단어의 앞부분으로 (원본과 같음)
            if len(w) >= 3 and not pats[i].endswith("*") and not self._indexed(pats[i]):
                pats[i] += "*"
        keyword: dict[int, tuple[int, int, str | None]] = {}   # file → (rank, segment, snippet)
        coverage: dict[int, float] = {}
        if pats:
            query = " OR ".join(pats)
            # 파일마다 가장 잘 맞는 구간 하나로 순위를 매긴다. 구간 단위로 상위 N개를 자르면 페이지가 많은 PDF 하나가
            # 자리를 다 차지해 다른 파일이 후보에서 빠진다(Codex 검토). 종류 필터도 자르기 전에 SQL에서 건다.
            kind_filter, params = "", [query]
            if kinds is not None:
                kind_filter = f" AND f.kind IN ({','.join('?' * len(kinds))})"
                params += sorted(kinds)
            if allowed_files is not None:
                kind_filter += " AND noseout_allowed(f.id)"
            # bm25()는 집계 안에서 바로 쓸 수 없어, 구간 점수를 먼저 구한 뒤(MATERIALIZED) 파일별 최솟값을 낸다
            ranked = self.store.db.execute(
                f"""WITH m AS MATERIALIZED (SELECT rowid AS id, bm25(fts, 3.0, 1.0) AS r FROM fts WHERE fts MATCH ?)
                    SELECT s.file, MIN(m.r) AS rank FROM m
                    JOIN segments s ON s.id = m.id JOIN files f ON f.id = s.file
                    WHERE 1{kind_filter} GROUP BY s.file ORDER BY rank, s.file LIMIT 100""",
                params).fetchall()
            for file, _ in ranked:
                segment, snippet = self.store.db.execute(
                    """SELECT s.id, snippet(fts, 1, '«', '»', '…', 12) FROM fts JOIN segments s ON s.id = fts.rowid
                       WHERE fts MATCH ? AND s.file = ? ORDER BY bm25(fts, 3.0, 1.0) LIMIT 1""",
                    (query, file)).fetchone()
                keyword[file] = (len(keyword) + 1, segment, snippet)
            if keyword:
                ids = ",".join(str(f) for f in keyword)
                for p in pats:
                    for (file,) in self.store.db.execute(
                            f"""SELECT DISTINCT s.file FROM fts JOIN segments s ON s.id = fts.rowid
                                WHERE fts MATCH ? AND s.file IN ({ids})""", (p,)):
                        coverage[file] = coverage.get(file, 0) + 1 / len(pats)

        lookup = is_lookup(words) or name is not None
        word_weight = CODE_WORD_WEIGHT if self.code else WORD_WEIGHT
        meaning_weight, words_weight = (0.5, word_weight * 2) if lookup else (1.0, word_weight)
        hinted = kind_hints(text)
        candidates = {f for f, (z, _, _) in best.items() if z >= z_gate} | {f for f, c in coverage.items() if c >= 0.5}
        scored = {}
        for f in candidates:
            score = meaning_weight * max(best.get(f, (0,))[0], 0) + words_weight * coverage.get(f, 0) ** 1.5
            if self.file_kind.get(f) in hinted:
                score += 1
            scored[f] = score

        hits = []
        for rank, (f, score) in enumerate(sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]):
            semantic = best.get(f)
            kw = keyword.get(f)
            use_meaning = semantic is not None and (semantic[0] >= z_gate or kw is None)
            segment_id = int(self.row_segment[semantic[2]]) if use_meaning else kw[1]
            focus = None
            if self.code:
                # 이름의 정의는 상위 10개에서만 찾는다: 큰 저장소에서 흔한 이름은 수백 묶음에 있다 (원본과 같음)
                segment_id, focus = self._code_place(f, segment_id, name if rank < 10 else None, text, words,
                                                     (kw is not None and kw[1] == segment_id) or name is not None)
            path, kind, info = self.store.db.execute("SELECT path, kind, info FROM files WHERE id = ?", (f,)).fetchone()
            seg_kind, loc, loc_end, stored = self.store.db.execute(
                "SELECT kind, loc, loc_end, excerpt FROM segments WHERE id = ?", (segment_id,)).fetchone()
            # 단어 발췌는 가리키는 구간에서 나온 것일 때만 (5쪽을 가리키며 2쪽 문장을 보이지 않게)
            snippet = kw[2] if kw and kw[1] == segment_id and kw[2] and "«" in kw[2] else None
            hit = Hit(path, kind, score, semantic[0] if semantic else None, semantic[1] if semantic else None,
                      kw[0] if kw else None, seg_kind, segment_id, loc, snippet or stored,
                      json.loads(info) if info else {})
            if self.code:
                hit.loc_end, hit.focus = loc_end, focus
                body = self.segment_text(segment_id)
                if body is not None and loc is not None:
                    hit.context = code_files.context(body, int(loc), focus)
            hits.append(hit)
        return hits

    def segment_text(self, segment_id: int) -> str | None:
        """구간의 원문(정규화로 바뀐 경우 원래 글자). 코드 문맥 보기용."""
        row = self.store.db.execute("SELECT coalesce(original, body) FROM fts WHERE rowid = ?", (segment_id,)).fetchone()
        return row[0] if row else None

    def _code_place(self, file: int, segment: int, name: list[str] | None, query: str, words: list[str],
                    words_here: bool) -> tuple[int, int | None]:
        """코드: 가리킬 묶음과 열 줄 (원본 Searcher.codePlace). 이름을 찾으면 그것을 정의하는 묶음(쓰는 곳 말고)과 그 줄,
        아니면 검색어 단어가 이 묶음에서 맞았을 때 그 단어가 든 첫 줄, 뜻으로만 맞았으면 묶음의 첫 줄(None)."""
        typed = query.strip().replace("()", "")
        if name:
            best = None
            for seg, loc, body in self.store.db.execute(
                    """SELECT s.id, s.loc, coalesce(fts.original, fts.body) FROM fts JOIN segments s ON s.id = fts.rowid
                       WHERE fts MATCH ? AND s.file = ? ORDER BY s.loc LIMIT 300""",
                    (code_files.phrase(name), file)):
                if loc is None or body is None:
                    continue
                found = code_files.definition(typed, body, int(loc))
                if found and found[1] > (best[2] if best else 0):
                    best = (seg, found[0], found[1])
                    if found[1] == 2:
                        break
            if best:
                return best[0], best[1]
        if not (words_here or name):
            return segment, None
        body = self.segment_text(segment)
        loc = self.store.db.execute("SELECT loc FROM segments WHERE id = ?", (segment,)).fetchone()
        if body is None or not loc or loc[0] is None:
            return segment, None
        return segment, code_files.first_line([typed.lower()] if name else words, body, int(loc[0]))

    def _scores(self, query: np.ndarray) -> np.ndarray:
        """모든 벡터와의 코사인(모두 단위 길이). 원본처럼 블록씩 float32로 바꿔 곱한다: 10만 구간에서도 검색마다
        전체 float32 사본(300 MB)을 만들지 않는다."""
        scores = np.empty(len(self.matrix), np.float32)
        for start in range(0, len(self.matrix), BLOCK_ROWS):
            block = self.matrix[start:start + BLOCK_ROWS].astype(np.float32)
            scores[start:start + len(block)] = block @ query
        return scores

    def _indexed(self, pattern: str) -> bool:
        return self.store.db.execute("SELECT 1 FROM fts WHERE fts MATCH ? LIMIT 1", (pattern,)).fetchone() is not None


def strong_count(hits: list[Hit], ratio: float = 0.5) -> int:
    if not hits or hits[0].score <= 0:
        return len(hits)
    return sum(1 for h in hits if h.score >= hits[0].score * ratio)
