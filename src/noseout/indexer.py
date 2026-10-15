"""고른 폴더를 색인과 맞춘다: 새 파일·바뀐 파일은 다시 읽고, 없어진 파일은 지운다. 원본 Indexer.swift의 문서·이미지·PDF
부분을 따른다(음성·영상·OCR·코드는 이후 단계). 사용자 파일은 읽기만 하고, 쓰는 곳은 색인 파일과 그 옆 임시 폴더뿐이다."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from . import code as code_files
from . import crawler, extractors, media, ocr, prompts, scope, search_text
from .engine import Embedder
from .store import IndexStore, signed64

PHOTO_BUDGET = 140
SCREENSHOT_BUDGET = 280
PAGE_BUDGET = 280
FRAME_BUDGET = 70
TEXT_BATCH = 16
# 긴 파일 나눠 읽기 (원본 IndexOptions): BIG_PDF_PAGES쪽 이하 PDF는 한 번에, 더 긴 PDF는 PDF_STEP쪽씩,
# 문서는 DOC_STEP청크(약 64,000자)씩. 모든 파일의 첫 단계를 먼저 하고, 나머지는 finish_long_files에서.
BIG_PDF_PAGES = 50
PDF_STEP = 30
DOC_STEP = 40
# 색인 버전 (U2, docs/progress.md). 4단계까지는 설정 문자열 하나(meta vector_source)를 통째로 비교했는데, 그러면
# 문서·이미지 벡터가 그대로인데도 음성·영상 설정이 더해졌다는 이유로 색인을 거부했고(4단계), 반대로 HTML 추출이
# 바뀌어 벡터 입력이 달라졌는데도 알아채지 못했다(3단계). 그래서 둘로 나눈다:
# - ENGINE: 모든 벡터를 만든 모델·양자화·llama.cpp. 다르면 섞을 수 없으므로 색인을 건드리지 않고 거부한다.
# - COMPONENTS: 종류별로 벡터나 키워드를 만드는 조건. 하나가 바뀌면 그 구성요소에 해당하는 파일만 pending으로 돌려
#   다음 sync에서 다시 읽는다. 다시 읽기 전까지 기존 구간은 지우지 않는다(검색에 남는다).
# 추출·OCR·임베딩 입력을 바꾸면 해당 구성요소의 버전을 올린다(AGENTS.md).
INDEX_FORMAT = "2"
ENGINE = "embeddinggemma-2@bfcd298 q8_0 llama.cpp-b11461"
_HTML = "(lower(path) LIKE '%.html' OR lower(path) LIKE '%.htm')"
_SCANNED_PDF = "kind = 'pdf' AND id IN (SELECT file FROM segments WHERE kind = 'page_image')"
# 이름: (현재 버전, 그 구성요소가 바뀌면 다시 읽을 파일을 고르는 SQL 조건)
COMPONENTS: dict[str, tuple[str, str]] = {
    "image.vectors": (f"photo{PHOTO_BUDGET}", "kind = 'image'"),
    "screenshot.vectors": (f"shot{SCREENSHOT_BUDGET}", "kind = 'screenshot'"),
    "screenshot.ocr": ("winocr1", "kind = 'screenshot'"),
    "pdf.vectors": (f"page{PAGE_BUDGET} text{extractors.PAGE_TEXT_LIMIT} render{extractors.RENDER_PIXELS}",
                    "kind = 'pdf'"),
    "pdf.ocr": ("winocr1", _SCANNED_PDF),
    "doc.vectors": (f"chunk{extractors.CHUNK_SIZE}/{extractors.CHUNK_OVERLAP}", "kind = 'doc'"),
    "doc.html": ("htmlparser1", f"kind = 'doc' AND {_HTML}"),
    "audio.vectors": (f"audio{media.AUDIO_WINDOW:g}/{media.AUDIO_HOP:g}s", "kind = 'audio'"),
    "video.vectors": (f"frame{FRAME_BUDGET}/{media.FRAME_INTERVAL:g}s audio{media.AUDIO_WINDOW:g}/{media.AUDIO_HOP:g}s",
                      "kind = 'video'"),
}
# 코드 색인(code.sqlite, 6단계)의 구성요소. 일반 색인의 표와 따로 둔다: 두 색인은 서로의 파일을 갖지 않는다.
# "code1": 원본 IndexOptions.fingerprint와 같은 뜻(줄 묶음, 저장소 기준 경로를 제목으로). 읽는 방법을 바꾸면 올린다.
CODE_COMPONENTS: dict[str, tuple[str, str]] = {
    "code.vectors": (f"code1 lines{code_files.STRETCH}", "kind = 'code'"),
}
# 4단계까지의 설정 문자열 (형식 1). 이 두 개만 형식 2로 옮긴다. 그 밖의 문자열은 거부한다.
LEGACY_STAGE2_3 = f"{ENGINE} photo140 shot280 page280 chunk1800/200"
LEGACY_STAGE4 = f"{LEGACY_STAGE2_3} frame70/3s audio30/25s"
LEGACY_COMPONENTS = {
    # 2·3단계(ca753ed, aa9199c): 문서·이미지·PDF 벡터 조건은 지금과 같다(코드 diff로 확인). OCR이 없었고, 2단계의 HTML
    # 추출은 지금과 달랐다(3단계 색인과 문자열로 구분할 수 없어 HTML은 다시 읽는다). 음성·영상은 건너뛰어 색인에 없다.
    LEGACY_STAGE2_3: {"image.vectors": "photo140", "screenshot.vectors": "shot280", "screenshot.ocr": "none",
                      "pdf.vectors": "page280 text4000 render1400", "pdf.ocr": "none",
                      "doc.vectors": "chunk1800/200", "doc.html": "regex0",
                      "audio.vectors": "none", "video.vectors": "none"},
    # 4단계(f672ed5): 지금과 같다.
    LEGACY_STAGE4: {"image.vectors": "photo140", "screenshot.vectors": "shot280", "screenshot.ocr": "winocr1",
                    "pdf.vectors": "page280 text4000 render1400", "pdf.ocr": "winocr1",
                    "doc.vectors": "chunk1800/200", "doc.html": "htmlparser1",
                    "audio.vectors": "audio30/25s", "video.vectors": "frame70/3s audio30/25s"},
}
# 형식 2 색인에 옛 코드가 실수로 쓰지 않도록 vector_source 자리에 남기는 표지 (옛 코드는 다른 문자열이라 거부한다)
VECTOR_SOURCE_MARKER = f"index-format-{INDEX_FORMAT}; see meta 'engine' and 'components'"


# 분석 순서 (2026-10-14, 드라이브 전체 이름 검색 + 선택 범위 AI 분석). 원본은 종류 순서(스크린샷·그림·PDF·문서)로 읽었다.
# 선택 범위가 넓어지면 큰 영상·음성 몇 개가 앞을 막아 문서 대부분이 늦게 검색된다. 그래서 파일마다 분석 비용(초)을 대략
# 추정하고, 최근에 바뀐 파일은 비용을 낮게 쳐서 "최근 파일·작은 문서 먼저, 큰 영상·음성 나중"으로 읽는다. 읽는 순서만
# 바꾼다: 벡터·점수·위치는 순서와 무관하다(전후 평가 동일, docs/progress.md). 값은 이 PC(RTX 4070 SUPER)의 대략치다.
COST_BASE = {"image": 0.15, "screenshot": 0.45, "pdf": 0.4, "doc": 0.1, "audio": 0.5, "video": 1.0}
COST_PER_MB = {"image": 0.02, "screenshot": 0.02, "pdf": 0.6, "doc": 2.0,
               "audio": 0.6,     # 약 1MB/분, 30초 창(25초 간격)마다 약 0.25초
               "video": 0.5}     # 약 2MB/분 이상, 3초 간격 장면 + 소리 창
COST_CAP = {"pdf": 12.0, "doc": 6.0}   # 긴 PDF·문서는 첫 단계만 먼저 읽는다(나머지는 finish_long_files)
RECENT_WEIGHT = ((7, 4.0), (90, 2.0))   # (며칠 안에 바뀜, 비용을 나눌 값)


def estimated_seconds(kind: str, size: int) -> float:
    cost = COST_BASE.get(kind, 0.2) + COST_PER_MB.get(kind, 0.1) * size / 1_000_000
    return min(cost, COST_CAP[kind]) if kind in COST_CAP else cost


def priority(kind: str, size: int, mtime: float, now: float) -> float:
    """작을수록 먼저. 추정 비용을 최근성으로 나눈다."""
    age_days = max(0.0, (now - mtime) / 86400)
    weight = next((w for days, w in RECENT_WEIGHT if age_days <= days), 1.0)
    return estimated_seconds(kind, size) / weight


class IncompatibleIndex(RuntimeError):
    """다른 벡터로 만든 색인. 색인 파일은 바꾸지 않았다."""


@dataclass
class VersionCheck:
    created: bool = False
    migrated_from: str | None = None         # "stage2-3" | "stage4" | None
    changed_components: list[str] = field(default_factory=list)
    requeued: int = 0


def current_components(components: dict[str, tuple[str, str]] | None = None) -> dict[str, str]:
    return {name: version for name, (version, _) in (components or COMPONENTS).items()}


def check_versions(store: IndexStore, components: dict[str, tuple[str, str]] | None = None) -> VersionCheck:
    """색인의 버전을 확인하고, 호환되면 바뀐 구성요소의 파일만 pending으로 돌린다(한 트랜잭션). 엔진이 다르거나
    모르는 옛 형식이면 IncompatibleIndex를 내고 색인은 그대로 둔다."""
    components = components or COMPONENTS
    result = VersionCheck()
    db = store.db
    fmt, legacy = store.meta("index_format"), store.meta("vector_source")
    if fmt is None and legacy is None:
        # 버전 기록이 없는데 벡터가 이미 있으면 어떤 조건으로 만든 것인지 알 수 없다: 새 색인으로 보지 않는다
        if db.execute("SELECT 1 FROM segments WHERE vec IS NOT NULL LIMIT 1").fetchone():
            raise IncompatibleIndex(_refusal(store, "no version record, but vectors are present"))
        _write_versions(store, components=components)
        result.created = True
        return result
    if fmt == INDEX_FORMAT:
        engine = store.meta("engine")
        if engine != ENGINE:
            raise IncompatibleIndex(_refusal(store, f"engine {engine!r}"))
        stored = json.loads(store.meta("components") or "{}")
    elif fmt is None and legacy in LEGACY_COMPONENTS and components is COMPONENTS:
        stored = LEGACY_COMPONENTS[legacy]
        result.migrated_from = "stage2-3" if legacy == LEGACY_STAGE2_3 else "stage4"
    else:
        raise IncompatibleIndex(_refusal(store, f"format {fmt!r}, vectors {legacy!r}"))
    result.changed_components = [name for name, version in current_components(components).items()
                                 if stored.get(name) != version]
    try:
        for name in result.changed_components:
            # 다시 읽을 파일만 pending으로. 구간은 지우지 않는다: 다시 읽을 때 새 구간으로 바뀐다(Indexer._index)
            result.requeued += db.execute(
                f"UPDATE files SET state = 'pending' WHERE state != 'pending' AND ({components[name][1]})").rowcount
        _write_versions(store, commit=False, components=components)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return result


def version_problem(store: IndexStore, components: dict[str, tuple[str, str]] | None = None) -> str | None:
    """이 프로그램의 벡터와 섞을 수 없는 색인이면 그 이유, 아니면 None. check_versions와 같은 판정이지만 색인을 읽기만
    한다(검색 서비스가 열 때 쓴다: 엔진이 다른 색인의 벡터를 안내 없이 비교하지 않게, 6단계 최종 점검)."""
    components = components or COMPONENTS
    fmt, legacy = store.meta("index_format"), store.meta("vector_source")
    if fmt is None and legacy is None:
        vectors = store.db.execute("SELECT 1 FROM segments WHERE vec IS NOT NULL LIMIT 1").fetchone()
        return "no version record, but vectors are present" if vectors else None
    if fmt == INDEX_FORMAT:
        engine = store.meta("engine")
        return None if engine == ENGINE else f"engine {engine!r}"
    if fmt is None and legacy in LEGACY_COMPONENTS and components is COMPONENTS:
        return None
    return f"format {fmt!r}, vectors {legacy!r}"


def _write_versions(store: IndexStore, commit: bool = True, components: dict | None = None) -> None:
    rows = [("index_format", INDEX_FORMAT), ("engine", ENGINE),
            ("components", json.dumps(current_components(components), sort_keys=True)),
            ("vector_source", VECTOR_SOURCE_MARKER)]
    store.db.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", rows)
    if commit:
        store.db.commit()


def _refusal(store: IndexStore, found: str) -> str:
    return (f"index was made with other vectors ({found}; this program uses {ENGINE!r}). The index at {store.path.name} "
            f"was left unchanged. Make a new index file with another --db path; delete the old one yourself only "
            f"after the new one is ready.")


@dataclass
class SyncReport:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    results: dict[str, str] = field(default_factory=dict)   # path → done/skipped/failed: 이유
    seconds: float = 0.0
    stopped: bool = False                                     # should_stop으로 중간에 멈춤 (남은 파일은 pending)
    planned: int = 0                                          # 이번에 읽을 파일 수
    estimated: float = 0.0                                    # 그 추정 분석 시간(초)


class Indexer:
    def __init__(self, store: IndexStore, embedder: Embedder | Callable[[], Embedder], code: bool = False):
        """`code`: 코드 색인(code.sqlite)을 맞춘다. 코드 파일만 걷고(crawler code=True) 줄 묶음으로 읽는다."""
        self.store = store
        self._embedder = embedder
        self.code = code
        self.versions = check_versions(store, CODE_COMPONENTS if code else COMPONENTS)

    @property
    def embedder(self) -> Embedder:
        if callable(self._embedder) and not isinstance(self._embedder, Embedder):
            self._embedder = self._embedder()   # 필요할 때만 서버를 띄운다
        return self._embedder

    def sync(self, roots: list[str | Path], finish_long: bool = True,
             progress: Callable[[int, int, str, str], None] | None = None,
             should_stop: Callable[[], bool] | None = None,
             checkpoint: Callable[[], None] | None = None,
             exclusions: list[str | Path] | None = None,
             on_plan: Callable[[int, float], None] | None = None) -> SyncReport:
        """`on_plan(파일 수, 추정 초)`은 읽기 전에 한 번 불린다. `progress(처리한 수, 전체 수, 경로, 결과)`는 파일(긴 파일은 단계)마다 불린다. `should_stop()`이 참이면 다음
        파일 앞에서 멈춘다: 파일마다 커밋하므로 이미 읽은 것은 남고, 남은 파일은 pending으로 다음 sync에서 이어진다."""
        started = time.perf_counter()
        exclusions = exclusions or []
        found = crawler.crawl(roots, exclusions=exclusions, code=self.code)
        known: dict[str, tuple[int, int, float, int]] = {}
        for root in roots:
            for path, row in self.store.files_under(str(Path(root).resolve())).items():
                known[os.path.normcase(path)] = row
        return self._apply(found, known, exclusions, started, finish_long, progress, should_stop, checkpoint, on_plan)

    def sync_files(self, paths: list[str | Path],
                   progress: Callable[[int, int, str, str], None] | None = None,
                   should_stop: Callable[[], bool] | None = None,
                   checkpoint: Callable[[], None] | None = None,
                   on_plan: Callable[[int, float], None] | None = None) -> SyncReport:
        """이름 결과에서 '내용 분석'으로 하나씩 고른 파일만 맞춘다(폴더는 승인하지 않는다). 규칙은 crawler.file_problem.
        없어졌거나 규칙에 맞지 않게 된 파일은 색인에서 지운다. 열 수 없는 파일은 지우지 않는다."""
        started = time.perf_counter()
        found = crawler.crawl_files(paths)
        known: dict[str, tuple[int, int, float, int]] = {}
        for path in paths:
            exact = self.store.file_at(os.path.abspath(path))
            if exact:
                known[os.path.normcase(exact[0])] = exact[1]
        return self._apply(found, known, [], started, True, progress, should_stop, checkpoint, on_plan)

    def sync_paths(self, roots: list[str | Path], targets: list[str | Path],
                   progress: Callable[[int, int, str, str], None] | None = None,
                   should_stop: Callable[[], bool] | None = None,
                   checkpoint: Callable[[], None] | None = None,
                   exclusions: list[str | Path] | None = None) -> SyncReport:
        """감시가 알려 준 경로(파일·폴더)만 sync와 같은 규칙으로 맞춘다(6단계). 새 파일·바뀐 파일은 다시 읽고, 없어진
        경로의 색인만 지운다. 열 수 없는 경로·고른 폴더는 지우지 않는다(crawler.crawl_targets)."""
        started = time.perf_counter()
        exclusions = exclusions or []
        found, covered = crawler.crawl_targets(roots, targets, exclusions=exclusions, code=self.code)
        known: dict[str, tuple[int, int, float, int]] = {}
        for path in covered:
            for under, row in self.store.files_under(path).items():
                known[os.path.normcase(under)] = row
            exact = self.store.file_at(path)
            if exact:
                known[os.path.normcase(exact[0])] = exact[1]
        return self._apply(found, known, exclusions, started, True, progress, should_stop, checkpoint)

    def _apply(self, found: crawler.CrawlResult, known: dict[str, tuple[int, int, float, int]],
               exclusions: list[str | Path], started: float, finish_long: bool,
               progress: Callable[[int, int, str, str], None] | None,
               should_stop: Callable[[], bool] | None,
               checkpoint: Callable[[], None] | None,
               on_plan: Callable[[int, float], None] | None = None) -> SyncReport:
        report = SyncReport()
        report.skipped = found.skipped
        # Windows 경로는 대소문자를 가리지 않는다: 폴더 이름의 대소문자만 바뀐 경우도 같은 파일로 본다
        seen = {os.path.normcase(f.path) for f in found.files}
        for f in found.files:
            old = known.get(os.path.normcase(f.path))
            if old and old[1] == f.size and old[2] == f.mtime and old[3] == signed64(f.file_id):
                report.unchanged += 1
                continue
            self.store.upsert_file(f.path, f.kind, f.size, f.mtime, f.file_id, f.device)
            if old:
                report.changed += 1
            else:
                report.added += 1
        # 읽지 못한 폴더(접근 거부 등) 아래의 파일은 지워진 것이 아니므로 색인에 남겨 둔다
        unreadable = {os.path.normcase(p) for p in found.unreadable}
        under = tuple(p.rstrip("\\") + "\\" for p in unreadable)
        for path, (file_id, *_rest) in known.items():
            if scope.excluded(path, exclusions):
                continue  # 제외는 삭제가 아니다. 해제 후 같은 벡터를 쓸 수 있게 보존한다
            if path not in seen and path not in unreadable and not path.startswith(under):
                self.store.remove_file(file_id)
                report.removed += 1
        self.store.commit()
        # 이번에 고른 폴더에서 찾은 파일만 읽는다 (다른 폴더의 대기 파일은 그 폴더를 sync할 때)
        todo = [row for row in self.store.pending() if os.path.normcase(row[1]) in seen]
        sizes = dict((id_, (size, mtime)) for id_, size, mtime in
                     self.store.db.execute("SELECT id, size, mtime FROM files WHERE state = 'pending'"))
        now = time.time()
        todo.sort(key=lambda row: (priority(row[2], *sizes.get(row[0], (0, now)), now), row[1]))
        report.planned = len(todo)
        report.estimated = sum(estimated_seconds(kind, sizes.get(id_, (0, 0))[0]) for id_, _, kind in todo)
        if on_plan:
            on_plan(report.planned, report.estimated)
        for done, (file_id, path, kind) in enumerate(todo, 1):
            if checkpoint:
                checkpoint()  # 이전 파일 커밋 뒤, 다음 트랜잭션을 열기 전에 기다린다
            if should_stop and should_stop():
                report.stopped = True
                break
            if checkpoint:
                try:
                    self._refresh_before_step(file_id, path, kind)
                except OSError as error:
                    report.results[path] = f"retry later: {error}"
                    continue
            report.results[path] = self._index(file_id, path, kind)
            self.store.commit()
            if progress:
                progress(done, len(todo), path, report.results[path])
        if finish_long and not report.stopped:
            results = self.finish_long_files(seen, progress=progress, should_stop=should_stop, checkpoint=checkpoint)
            report.stopped = bool(should_stop and should_stop() and self.store.partial())
            for path, result in results.items():
                report.results[path] = result
        report.seconds = time.perf_counter() - started
        return report

    def finish_long_files(self, scope: set[str] | None = None,
                          progress: Callable[[int, int, str, str], None] | None = None,
                          should_stop: Callable[[], bool] | None = None,
                          checkpoint: Callable[[], None] | None = None) -> dict[str, str]:
        """긴 PDF·문서의 나머지를 한 단계씩 읽는다 (원본 Indexer.finishLongFiles). 모든 파일의 첫 단계가 끝난 뒤에
        돌아서, 책 한 권의 뒷부분보다 다른 파일들의 앞부분이 먼저 검색된다. 단계마다 커밋하므로 중간에 멈춰도
        다음에 그 자리부터 이어진다. `scope`: normcase한 경로들, 이 밖의 파일은 건드리지 않는다."""
        results = {}
        long_files = [row for row in self.store.partial() if scope is None or os.path.normcase(row[1]) in scope]
        for done, (file_id, path, kind) in enumerate(long_files, 1):
            while True:
                if checkpoint:
                    checkpoint()
                if should_stop and should_stop():
                    return results
                changed = False
                if checkpoint:
                    try:
                        changed = self._refresh_before_step(file_id, path, kind)
                    except OSError as error:
                        results[path] = f"retry later: {error}"
                        break
                info = self.store.info(file_id)
                if changed:
                    step = None
                else:
                    read_to, read_end = int(info["read_to"]), int(info["read_end"])
                    step = (read_to, min(read_to + self._step(kind), read_end))
                result = self._index(file_id, path, kind, step=step)
                self.store.commit()
                results[path] = result
                if progress:
                    progress(done, len(long_files), path, result)
                if not result.startswith("partial"):
                    break
        return results

    def _refresh_before_step(self, file_id: int, path: str, kind: str) -> bool:
        """일시정지 동안 바뀐 파일의 앞·뒷부분을 섞지 않는다. 그대로인 파일은 다시 임베딩하지 않는다."""
        if not scope.readable_file(path):
            raise OSError("파일이 없거나 링크·정션으로 바뀌었습니다")
        current = os.stat(path, follow_symlinks=False)
        previous = self.store.db.execute("SELECT size, mtime, inode FROM files WHERE id = ?", (file_id,)).fetchone()
        if previous == (current.st_size, current.st_mtime, signed64(current.st_ino)):
            return False
        self.store.upsert_file(path, kind, current.st_size, current.st_mtime, current.st_ino, current.st_dev)
        self.store.commit()
        return True

    def _has_content(self, file_id: int) -> bool:
        return self.store.db.execute("SELECT 1 FROM segments WHERE file = ? AND kind != 'name' LIMIT 1",
                                     (file_id,)).fetchone() is not None

    @staticmethod
    def _step(kind: str) -> int:
        return PDF_STEP if kind == "pdf" else DOC_STEP

    def _index(self, file_id: int, path: str, kind: str, step: tuple[int, int] | None = None) -> str:
        """한 파일(긴 파일은 한 단계). 실패하면 이번에 넣은 구간을 되돌린다(검색에 반쪽짜리 파일이 남지 않게)."""
        try:
            if step is None:
                # 버전이 바뀌어 다시 읽는 파일은 이전 구간을 아직 갖고 있다(check_versions가 남겨 둠). 같은 트랜잭션에서
                # 지우고 새로 넣으므로, 실패하면 rollback으로 이전 구간이 그대로 돌아온다.
                self.store.clear_content(file_id)
            if kind in ("image", "screenshot"):
                return self._image(file_id, path, kind)
            if kind == "pdf":
                return self._pdf(file_id, path, step)
            if kind == "doc":
                return self._doc(file_id, path, step)
            if kind == "audio":
                return self._audio(file_id, path)
            if kind == "video":
                return self._video(file_id, path)
            if kind == "code":
                return self._code(file_id, path)
            self.store.finish(file_id, "skipped", f"{kind}: not handled")
            return "skipped"
        except requests.RequestException:
            # 엔진 장애는 파일 탓이 아니다: 되돌리고 sync를 멈춘다. 파일은 pending(긴 파일은 partial)으로 남아
            # 다음에 이어진다. (RequestException은 OSError의 하위 클래스라 아래 OSError보다 먼저 잡아야 한다)
            self.store.db.rollback()
            raise
        except extractors.ExtractError as error:
            self.store.db.rollback()
            if step is not None:
                # 긴 파일의 뒷부분이 깨졌다: 앞에서 읽은 부분은 검색되게 두고 끝낸다 (원본 stopReading)
                info = self.store.info(file_id)
                info.pop("read_to", None)
                info.pop("read_end", None)
                self.store.finish(file_id, "done", f"read to {step[0]}, not after: {error}", info)
                return f"done: stopped at {step[0]}: {error}"
            if self._has_content(file_id):
                # 버전 때문에 다시 읽던 파일(파일 자체는 그대로): 되돌린 이전 구간이 여전히 이 파일의 내용이다. 지우지 않는다.
                self.store.finish(file_id, "done", f"kept previous index, re-reading failed: {error}",
                                  self.store.info(file_id))
                return f"done: kept previous index ({error})"
            # 파일 자체의 문제: 파일이 바뀔 때까지 다시 읽지 않는다 (원본과 같음)
            left_out = isinstance(error, extractors.LeftOut) or str(error).startswith("small image")
            state = "skipped" if left_out else "failed"
            self.store.finish(file_id, state, str(error))
            return f"{state}: {error}"
        except OSError as error:
            # 사라졌거나 잠긴 파일: pending(partial)으로 두어 다음 sync에서 다시 시도한다
            self.store.db.rollback()
            return f"retry later: {error}"

    def _image(self, file_id: int, path: str, kind: str) -> str:
        info = extractors.image_info(path)
        budget = SCREENSHOT_BUDGET if kind == "screenshot" else PHOTO_BUDGET
        vector = self.embedder.embed_image(path, budget)
        self.store.add_segment(file_id, "image", "image", vector=vector)
        details = {"width": str(info.width), "height": str(info.height)}
        if info.taken:
            details["taken"] = info.taken
        if kind == "screenshot":
            # 화면 속 글자는 키워드 검색용(에러 코드, 이름을 그대로 찾게). 뜻은 그림 벡터만 (원본과 같음)
            text = ocr.text_of_image(path)
            if text:
                self.store.add_segment(file_id, "ocr", "text", excerpt=search_text.excerpt(text), text=text)
            details["ocr_chars"] = str(len(text))
            self.store.finish(file_id, "done", info=details)
            return f"done: ocr {len(text)} chars"
        self.store.finish(file_id, "done", info=details)
        return "done"

    def _pdf(self, file_id: int, path: str, step: tuple[int, int] | None) -> str:
        """BIG_PDF_PAGES쪽 이하 PDF는 한 번에, 더 긴 PDF는 처음 PDF_STEP쪽을 읽고 partial로 남긴다."""
        # 이번에 새로 만든 고유 임시 폴더만 쓰고 지운다 (기존 폴더와 이름이 겹쳐 지우는 일이 없게)
        render_to = Path(tempfile.mkdtemp(prefix="noseout-pdf-", dir=self.store.path.parent))
        try:
            start, stop = step or (0, PDF_STEP)
            pages, total, title = extractors.pdf_pages(path, render_to, start, stop)
            if step is None and PDF_STEP < total <= BIG_PDF_PAGES:   # 짧은 PDF: 나머지도 지금
                pages += extractors.pdf_pages(path, render_to, PDF_STEP, total)[0]
            if not pages and step is None:
                self.store.finish(file_id, "skipped", "no pages")
                return "skipped: no pages"
            end = min(total, extractors.MAX_PAGES)
            read_to = pages[-1].number if pages else end
            title = title or Path(path).stem
            text_pages = [p for p in pages if p.image_path is None]
            worded = [p for p in text_pages if search_text.has_words(p.text)]
            vectors = self._embed_texts([prompts.document(p.text[: extractors.PAGE_TEXT_LIMIT], title) for p in worded])
            by_page = {p.number: v for p, v in zip(worded, vectors)}
            for page in text_pages:
                self.store.add_segment(file_id, "page", "text", loc=page.number, excerpt=search_text.excerpt(page.text),
                                       text=page.text, vector=by_page.get(page.number))
            image_pages = [p for p in pages if p.image_path is not None]
            read_by_ocr = 0
            for page in image_pages:
                vector = self.embedder.embed_image(page.image_path, PAGE_BUDGET)
                # 스캔이나 그림이 대부분인 페이지: 글자는 OCR로 읽어 키워드 검색에만 (원본 macOS와 같음)
                words = page.text
                if not words:
                    words = ocr.text_of_image(page.image_path)
                    read_by_ocr += bool(words)
                self.store.add_segment(file_id, "page_image", "image", loc=page.number,
                                       excerpt=search_text.excerpt(words) if words else None,
                                       text=words, vector=vector)
            info = self.store.info(file_id) if step else {}
            info["pages"] = str(total)
            info["scanned_pages"] = str(int(info.get("scanned_pages", 0)) + len(image_pages))
            return self._finish_step(file_id, info, read_to, end, f"pages {start + 1}-{read_to} of {total}",
                                     f"{len(pages)} pages, {len(image_pages)} as images"
                                     + (f", {read_by_ocr} read by OCR" if read_by_ocr else ""))
        finally:
            shutil.rmtree(render_to, ignore_errors=True)

    def _doc(self, file_id: int, path: str, step: tuple[int, int] | None) -> str:
        """문서는 처음 DOC_STEP 청크를 읽고, 더 길면 partial로 남긴다. 청크는 매번 같은 규칙으로 자르므로
        다음 단계는 멈춘 자리에서 이어진다."""
        text = extractors.read_text(path)
        pieces = extractors.chunks(text)
        if not pieces and step is None:
            self.store.finish(file_id, "skipped", "no text")
            return "skipped: no text"
        end = min(len(pieces), extractors.MAX_CHUNKS)
        start, stop = step or (0, DOC_STEP)
        stop = min(stop, end)
        title = Path(path).stem
        worded = [i for i in range(start, stop) if search_text.has_words(pieces[i])]
        vectors = self._embed_texts([prompts.document(pieces[i], title) for i in worded])
        by_index = dict(zip(worded, vectors))
        for i in range(start, stop):
            self.store.add_segment(file_id, "chunk", "text", loc=i + 1, excerpt=search_text.excerpt(pieces[i]),
                                   text=pieces[i], vector=by_index.get(i))
        info = {"chunks": str(end), "chars": str(len(text))}
        keyword_only = (stop - start) - len(worded)
        summary = f"{stop - start} chunks" + (f", {keyword_only} keywords only" if keyword_only else "")
        return self._finish_step(file_id, info, stop, end, f"chunks {start + 1}-{stop} of {end}", summary)

    def _audio(self, file_id: int, path: str) -> str:
        """30초 창마다 벡터 하나. 검색 결과는 그 창의 시작 초를 가리킨다."""
        folder = Path(tempfile.mkdtemp(prefix="noseout-audio-", dir=self.store.path.parent))
        try:
            pieces, duration = media.audio_windows(path, folder)
            if not pieces:
                self.store.finish(file_id, "skipped", "silent or shorter than 2 s")
                return "skipped: silent or too short"
            for piece in pieces:
                self.store.add_segment(file_id, "audio", "audio", loc=piece.start, loc_end=piece.end,
                                       vector=self.embedder.embed_audio(piece.path))
            self.store.finish(file_id, "done", info={"duration": f"{duration:.1f}"})
            return f"done: {len(pieces)} windows, {int(duration)} s"
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _video(self, file_id: int, path: str) -> str:
        """장면마다 프레임 하나(70토큰)와 소리 30초 창들. 검색 결과는 그 장면·구간의 시작 초를 가리킨다."""
        folder = Path(tempfile.mkdtemp(prefix="noseout-video-", dir=self.store.path.parent))
        try:
            frames, duration, has_audio = media.keyframes(path, folder)
            sound = media.audio_windows(path, folder)[0] if has_audio else []
            if not frames and not sound:
                self.store.finish(file_id, "skipped", "no frames or sound")
                return "skipped: no frames or sound"
            for frame in frames:
                self.store.add_segment(file_id, "frame", "image", loc=frame.start, loc_end=frame.end,
                                       vector=self.embedder.embed_image(frame.path, FRAME_BUDGET))
            for piece in sound:
                self.store.add_segment(file_id, "audio", "audio", loc=piece.start, loc_end=piece.end,
                                       vector=self.embedder.embed_audio(piece.path))
            self.store.finish(file_id, "done", info={"duration": f"{duration:.1f}", "frames": str(len(frames)),
                                                     "sound_windows": str(len(sound))})
            return f"done: {len(frames)} frames, {len(sound)} sound windows, {int(duration)} s"
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _code(self, file_id: int, path: str) -> str:
        """코드 파일의 줄 묶음(노트북은 셀 묶음)마다 벡터 하나. 제목은 저장소 기준 경로 (원본 Indexer.code).
        loc·loc_end는 묶음의 첫·마지막 줄(셀)이라 결과가 그 줄에서 열린다."""
        found = code_files.read(path)
        if not found.pieces:
            self.store.finish(file_id, "skipped", "no code")
            return "skipped: no code"
        worded = [i for i, piece in enumerate(found.pieces) if search_text.has_words(piece.text)]
        vectors = self._embed_texts([prompts.document(found.pieces[i].text, found.title) for i in worded])
        by_index = dict(zip(worded, vectors))
        segment = "lines" if found.cells is None else "cells"
        for i, piece in enumerate(found.pieces):
            self.store.add_segment(file_id, segment, "text", loc=piece.first, loc_end=piece.last,
                                   excerpt=code_files.summary(piece.text), text=piece.text, vector=by_index.get(i))
        info = {"lines": str(found.lines)}
        if found.cells is not None:
            info["cells"] = str(found.cells)
        if found.repo:
            info["repo"] = found.repo
        self.store.finish(file_id, "done", info=info)
        keyword_only = len(found.pieces) - len(worded)
        return (f"done: {len(found.pieces)} stretches, "
                + (f"{found.cells} cells" if found.cells is not None else f"{found.lines} lines")
                + (f", {keyword_only} keywords only" if keyword_only else ""))

    def _finish_step(self, file_id: int, info: dict, read_to: int, end: int, span: str, summary: str) -> str:
        if read_to < end:
            info.update(read_to=str(read_to), read_end=str(end))
            self.store.finish(file_id, "partial", f"read to {read_to} of {end}", info)
            return f"partial: {span}"
        info.pop("read_to", None)
        info.pop("read_end", None)
        self.store.finish(file_id, "done", None, info)
        return f"done: {summary}"

    def _embed_texts(self, texts: list[str]) -> list:
        out = []
        for start in range(0, len(texts), TEXT_BATCH):
            out.extend(self.embedder.embed_texts(texts[start:start + TEXT_BATCH]))
        return out
