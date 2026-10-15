"""화면이 멈추지 않도록 검색과 색인을 각자의 QThread에서 돌린다(5단계).

- SearchService: 텍스트 전용 엔진(mmproj 없이, 원본의 검색 헬퍼와 같은 역할)과 색인 연결을 자기 스레드에서 가진다.
  화면은 요청 번호와 함께 검색을 보내고, 가장 최근 요청만 처리·표시한다.
- 코드 검색(6단계): "code:"로 시작하는 검색만 코드 색인(code.sqlite)을 본다(원본 CodeQuery). 코드 색인은 코드 폴더의
  코드 파일만 담고, 일반 색인과 따로 만든다.
- IndexService: 색인할 때만 전체 엔진(텍스트+이미지+음성)을 띄운다. 파일마다 진행을 알리고, 중지하면 지금 파일이 끝난 뒤
  멈춘다(파일마다 커밋하므로 읽은 것은 남는다). 앱을 끌 때 오래 걸리면 엔진을 먼저 끊어 지금 파일을 되돌리고 끝낸다.
사용자에게는 이해하기 쉬운 한국어 안내만 보낸다. 자세한 오류는 로그 파일에 남긴다.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import requests
from PySide6.QtCore import QObject, Signal, Slot

from . import code as code_files
from . import engine, names, ntfs, prompts, rules, scope, stats
from .crawler import root_problem
from .indexer import CODE_COMPONENTS, IncompatibleIndex, Indexer, version_problem
from .searcher import Searcher
from .store import IndexStore

log = logging.getLogger("noseout")


def friendly(error: BaseException) -> str:
    """예외를 사용자용 한국어 안내로. traceback은 보여 주지 않는다(로그에만)."""
    if isinstance(error, IncompatibleIndex):
        name = "code.sqlite" if "code.sqlite" in str(error) else "index.sqlite"
        what = "코드 색인" if name == "code.sqlite" else "이 색인"
        return (f"{what}은 다른 모델·엔진으로 만들어져 함께 쓸 수 없습니다. 기존 색인은 그대로 두었습니다. "
                f"설정 폴더의 {name}를 다른 곳으로 옮기거나 지운 뒤 다시 색인하세요.")
    if isinstance(error, FileNotFoundError) and str(getattr(error, "filename", "") or "").lower().endswith(
            ("llama-server.exe", ".gguf")):
        return "검색 엔진 파일(llama.cpp 또는 모델)을 찾지 못했습니다. vendor\\llama와 models 폴더를 확인하세요."
    if isinstance(error, (TimeoutError, RuntimeError)) and "llama-server" in str(error):
        return "검색 엔진을 시작하지 못했습니다. GPU 메모리나 엔진 파일을 확인한 뒤 다시 시도하세요."
    if isinstance(error, requests.RequestException):
        return "검색 엔진과의 연결이 끊겼습니다. 다시 시도하세요. 색인하던 파일은 다음 색인 때 이어집니다."
    if isinstance(error, ValueError) and ("refusing to index" in str(error) or "not a folder" in str(error)):
        return folder_message(str(error))
    return "예상하지 못한 오류가 났습니다. 자세한 내용은 설정 폴더의 app.log에 남겼습니다."


def folder_message(problem: str) -> str:
    if "drive root" in problem:
        return "드라이브 전체는 고를 수 없습니다. 검색할 폴더를 골라 주세요."
    if "whole user folder" in problem:
        return "사용자 폴더 전체는 고를 수 없습니다. 문서·사진처럼 검색할 하위 폴더를 골라 주세요."
    if "system folder" in problem:
        return "Windows·프로그램·앱 데이터 폴더는 고를 수 없습니다."
    if "link or junction" in problem:
        return "바로 가기(링크·정션) 폴더는 고를 수 없습니다. 실제 폴더를 골라 주세요."
    if "not a folder" in problem:
        return "폴더가 없거나 열 수 없습니다."
    return "이 폴더는 고를 수 없습니다."


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, round(seconds))}초"
    if seconds < 5400:
        return f"{round(seconds / 60)}분"
    return f"{seconds / 3600:.1f}시간"


def name_folder_message(problem: str) -> str:
    if "link or junction" in problem:
        return "바로 가기(링크·정션) 폴더는 고를 수 없습니다. 실제 폴더나 드라이브를 골라 주세요."
    if "system folder" in problem:
        return "Windows·프로그램·앱 데이터 폴더는 이름 검색 범위로도 고를 수 없습니다."
    return "폴더가 없거나 열 수 없습니다."


def check_name_folder(path: str) -> str | None:
    """이름 검색 범위로 고를 때의 검사: 드라이브 루트·사용자 폴더 전체 허용(이름·경로만), 시스템 폴더·링크는 거부."""
    problem = rules.name_root_problem(path)
    return name_folder_message(problem) if problem else None


FILE_PROBLEMS = {
    "missing": "파일이 없습니다(옮겨졌거나 지워짐).", "link": "링크·정션을 거친 파일은 분석하지 않습니다.",
    "folder": "폴더는 통째로 분석에 추가할 수 없습니다. 검색 폴더로 추가하세요.",
    "hidden": "숨김 파일은 분석하지 않습니다.", "secret": "키·비밀번호 파일은 분석하지 않습니다.",
    "unsupported": "이 종류는 내용 분석을 지원하지 않습니다(문서·PDF·그림·음성·영상만).",
    "system": "Windows·프로그램·앱 데이터 폴더의 파일은 분석하지 않습니다.",
    "developer": "개발·숨김 폴더(node_modules, .venv 등) 안의 파일은 분석하지 않습니다.",
    "cloud": "클라우드에만 있는 파일(OneDrive 온디맨드)은 내려받지 않으므로 분석하지 않습니다.",
}


def check_file(path: str) -> str | None:
    from .crawler import file_problem

    problem = file_problem(path)
    return FILE_PROBLEMS.get(problem, problem) if problem else None


def check_folder(path: str) -> str | None:
    """폴더를 고를 때의 검사: 색인(crawler)과 같은 규칙. 문제가 있으면 사용자용 안내."""
    problem = root_problem(path)
    return folder_message(problem) if problem else None


class SearchService(QObject):
    ready = Signal(str)                  # 상태 문구
    refreshed = Signal(str)              # 색인 중 새로 읽은 뒤의 상태 문구 (검색을 다시 보내지 않는다)
    results = Signal(int, str, list)     # 요청 번호, 검색어, Hit 목록
    failed = Signal(int, str)            # 요청 번호(-1 = 시작 실패), 안내

    def __init__(self, db: Path, config: engine.ServerConfig | None = None, roots: list[str] | None = None,
                 exclusions: list[str] | None = None, code_db: Path | None = None,
                 code_roots: list[str] | None = None):
        super().__init__()
        self.db = Path(db)
        self.code_db = Path(code_db) if code_db else self.db.with_name("code.sqlite")
        self.code_roots = code_roots   # None: 범위 제한 없음(기존 API), 앱은 목록을 넘긴다
        self.code_store: IndexStore | None = None
        self.code_searcher: Searcher | None = None
        self.allowed_code: set[int] | None = None
        self.incompatible: dict[str, str] = {}   # "main"/"code" → 사용자 안내 (엔진이 다른 색인은 검색에 쓰지 않는다)
        self.config = config or engine.ServerConfig(media_model=None)   # 검색은 텍스트 부분만(약 0.3 GB)
        self.latest = 0                  # 화면이 마지막으로 보낸 요청 번호 (오래된 요청은 건너뜀)
        self.embedder: engine.Embedder | None = None
        self.store: IndexStore | None = None
        self.searcher: Searcher | None = None
        self._stop = threading.Event()
        self.roots = roots  # None은 기존 서비스 API(범위 제한 없음), 앱은 빈 목록도 명시해 아무 폴더도 검색하지 않는다
        self.exclusions = exclusions or []
        self.allowed_files: set[int] | None = None
        self.refresh_ms: list[float] = []   # 색인 중 다시 읽는 데 걸린 시간 (측정용)
        self.extra_files: set[str] = set()  # '내용 분석'으로 하나씩 고른 파일 (normcase)

    @Slot()
    def start(self) -> None:
        try:
            self.embedder = engine.Embedder(self.config, should_stop=self._stop.is_set)
            if self._stop.is_set():
                self.shutdown()
                return
            self._open()
        except Exception as error:  # noqa: BLE001
            self.shutdown()
            log.exception("search engine start failed")
            self.failed.emit(-1, friendly(error))
            return

    def _checked(self, path: Path, key: str, components=None) -> IndexStore | None:
        """색인을 열되, 이 프로그램의 벡터와 섞을 수 없으면(엔진이 다름 등) 닫고 안내만 남긴다. 색인은 바꾸지 않는다."""
        store = IndexStore(path)
        problem = version_problem(store, components)
        if problem is None:
            self.incompatible.pop(key, None)
            return store
        store.close()
        log.warning("index %s not usable for search: %s", path.name, problem)
        self.incompatible[key] = friendly(IncompatibleIndex(f"{problem}; the index at {path.name} was left unchanged"))
        return None

    def _open(self, announce: bool = True) -> None:
        if self.searcher is None and self.db.exists():
            self.store = self._checked(self.db, "main")
            self.searcher = Searcher(self.store) if self.store else None
        if self.code_searcher is None and self.code_db.exists():
            self.code_store = self._checked(self.code_db, "code", CODE_COMPONENTS)
            self.code_searcher = Searcher(self.code_store, code=True) if self.code_store else None
        self._scope_files()
        if announce:
            self.ready.emit(self._status())

    def _status(self) -> str:
        code = ""
        if self.code_searcher is not None:
            count = sum(state == "done" and (self.allowed_code is None or file in self.allowed_code)
                        for file, state in self.code_store.db.execute("SELECT id, state FROM files"))
            code = f" · 코드 파일 {count}개 ('code:'로 검색)"
        if "main" in self.incompatible or "code" in self.incompatible:
            return " ".join(self.incompatible.values())
        if self.searcher is None and code:
            return code.removeprefix(" · ") + " · 일반 파일 색인은 아직 없습니다"
        if self.searcher is None:
            return "아직 색인이 없습니다. '검색 폴더'에서 폴더를 고르고 색인을 시작하세요."
        files = sum(state == "done" and (self.allowed_files is None or file in self.allowed_files)
                    for file, state in self.store.db.execute("SELECT id, state FROM files"))
        return f"검색할 수 있는 파일 {files}개" + code

    def _scope_files(self) -> None:
        if self.store:
            # '내용 분석'으로 하나씩 고른 파일은 고른 폴더 밖이어도 검색한다(그 파일만, 부모 폴더는 아님)
            self.allowed_files = None if self.roots is None else {
                file for file, path in self.store.db.execute("SELECT id, path FROM files")
                if scope.allows(path, self.roots, self.exclusions) or os.path.normcase(path) in self.extra_files}
        if self.code_store:
            self.allowed_code = None if self.code_roots is None else {
                file for file, path in self.code_store.db.execute("SELECT id, path FROM files")
                if scope.allows(path, self.code_roots, self.exclusions)}

    @Slot(list)
    def set_extra(self, files: list) -> None:
        self.extra_files = {os.path.normcase(os.path.abspath(f)) for f in files}
        self._scope_files()

    @Slot(list, list)
    def set_scope(self, roots: list, exclusions: list) -> None:
        self.roots, self.exclusions = list(roots), list(exclusions)
        self._scope_files()

    @Slot(list)
    def set_code_scope(self, code_roots: list) -> None:
        self.code_roots = list(code_roots)
        self._scope_files()

    @Slot()
    def reload(self) -> None:
        """색인이 바뀐 뒤 벡터를 다시 읽는다."""
        try:
            if self.searcher is None or (self.code_searcher is None and self.code_db.exists()):
                self._open()
            if self.embedder is not None:
                for searcher in (self.searcher, self.code_searcher):
                    if searcher is not None:
                        searcher.reload()
                self._open()
        except Exception as error:  # noqa: BLE001
            log.exception("search index reload failed")
            self.failed.emit(-1, friendly(error))

    @Slot()
    def refresh(self) -> None:
        """AI 색인이 도는 동안 주기적으로 부른다: 끝낸 파일부터 의미 검색에 나오게 벡터를 다시 읽는다. 이 연결은 색인 쓰기와
        다른 연결이라 커밋된 구간만 보인다(파일·긴 파일 단계마다 커밋). 화면의 검색을 다시 보내지 않고(ready 대신 refreshed)
        목록이 바뀌는 시간을 measure_names.py로 잰다."""
        if self.embedder is None or self._stop.is_set():
            return
        started = time.perf_counter()
        try:
            if self.searcher is None or (self.code_searcher is None and self.code_db.exists()):
                self._open(announce=False)
            else:
                for searcher in (self.searcher, self.code_searcher):
                    if searcher is not None:
                        searcher.reload()
                self._scope_files()
            self.refresh_ms.append((time.perf_counter() - started) * 1000)
            self.refreshed.emit(self._status())
        except Exception:  # noqa: BLE001
            log.exception("search index refresh failed")

    @Slot(int, str, object)
    def search(self, request: int, text: str, kinds: set[str] | None = None) -> None:
        if request < self.latest or self.embedder is None or self._stop.is_set():
            return
        code = code_files.query_text(text)
        if code is not None:
            self._search_code(request, text, code)
            return
        if self.searcher is None and "main" in self.incompatible:
            self.failed.emit(request, self.incompatible["main"])
            return
        if self.searcher is None:
            self.results.emit(request, text, [])
            return
        try:
            vector = self.embedder.embed_texts([prompts.query(text)])[0]
            if request < self.latest:
                return
            self.results.emit(request, text, self.searcher.search(text, vector, kinds=kinds, limit=30,
                                                                 allowed_files=self.allowed_files))
        except Exception as error:  # noqa: BLE001
            log.exception("search failed")
            self.failed.emit(request, friendly(error))

    def _search_code(self, request: int, text: str, query: str) -> None:
        """코드 검색: 코드 색인만, 원본처럼 코드용 검색 프롬프트로 (종류 필터는 쓰지 않는다)."""
        if query and self.code_searcher is None and "code" in self.incompatible:
            self.failed.emit(request, self.incompatible["code"])
            return
        if not query or self.code_searcher is None:
            self.results.emit(request, text, [])
            return
        try:
            vector = self.embedder.embed_texts([prompts.code_query(query)])[0]
            if request < self.latest:
                return
            self.results.emit(request, text, self.code_searcher.search(query, vector, limit=30,
                                                                      allowed_files=self.allowed_code))
        except Exception as error:  # noqa: BLE001
            log.exception("code search failed")
            self.failed.emit(request, friendly(error))

    @Slot()
    def shutdown(self) -> None:
        if self.embedder:
            self.embedder.close()
            self.embedder = None
        for store in (self.store, self.code_store):
            if store:
                store.close()
        self.store = self.code_store = None
        self.searcher = self.code_searcher = None

    def request_stop(self) -> None:
        """화면 스레드에서 진행 중인 HTTP·엔진 시작을 취소한다. SQLite는 소유 스레드에서 닫는다."""
        self._stop.set()
        if self.embedder:
            self.embedder.close()


class NameService(QObject):
    """이름 목록(names.sqlite)을 자기 스레드에서 등록·갱신한다. 엔진을 쓰지 않으므로 엔진 시작·실패·버전 불일치·AI 색인 진행과
    무관하다. 폴더를 고르면 바로 등록하고, 앱 시작·'색인 시작' 때 전체를 다시 맞추고, 감시 이벤트는 AI 갱신을 기다리지 않고
    반영한다(색인 스레드와 따로 돈다)."""

    progress = Signal(int, str)   # 지금까지 새로 등록한 수, 걷는 폴더 (0.25초에 한 번까지)
    changed = Signal()            # 커밋했다: 검색 창이 이름 결과를 새로 고친다
    paused_changed = Signal(bool)
    finished = Signal(dict)
    failed = Signal(str)
    method_state = Signal(dict)   # 이름 목록 방식(자동·빠른 NTFS·폴더 순회)과 볼륨별 상태 (설정 화면, 감시)
    stage = Signal(str)           # "checking"(저장된 목록으로 검색하며 변경 확인) | "registering"

    def __init__(self, db: Path, volume_factory=None, volume_of=None, helper=None):
        super().__init__()
        self.db = Path(db)
        self.index: names.NameIndex | None = None
        self.method = "auto"
        self.ntfs: ntfs.NtfsNames | None = None
        # 시험에서 가짜 볼륨·도우미로 바꾼다. 기본은 실제 API(일반 권한)와 UAC 도우미.
        self._volume_factory = volume_factory or ntfs.Volume
        self._volume_of = volume_of or ntfs.volume_of
        self._helper = helper if helper is not None else ntfs.Helper()
        self.roots: list[str] = []
        self.exclusions: list[str] = []
        self.code_roots: list[str] = []
        self.name_roots: list[str] = []     # 이름만 검색하는 범위(드라이브 전체 등)
        self._stop = threading.Event()      # 앱 종료
        self._cancel = threading.Event()    # 지금 전체 걷기만 중지 (다음 scan에서 처음부터 다시 맞춘다)
        self._control = threading.Condition()
        self._pause = False
        self.paused = False
        self._progress_at = 0.0
        self._stage = ""
        self.busy = False

    def request_pause(self) -> None:
        with self._control:
            self._pause = True

    def resume(self) -> None:
        with self._control:
            self._pause = False
            self._control.notify_all()

    def request_cancel(self) -> None:
        """화면에서 부른다: 지금 걷기를 폴더 경계에서 멈춘다. 이미 등록한 이름은 남는다(커밋 단위)."""
        self._cancel.set()
        self.resume()

    def _should_stop(self) -> bool:
        """폴더 경계마다 불린다. 일시정지면 여기서 기다린다(걸은 자리를 잃지 않는다). 기다리는 동안 이름 검색은 다른
        스레드라 계속된다."""
        with self._control:
            if self._pause and not (self._stop.is_set() or self._cancel.is_set()):
                self.paused = True
                self.paused_changed.emit(True)
                while self._pause and not (self._stop.is_set() or self._cancel.is_set()):
                    self._control.wait(0.5)
                self.paused = False
                self.paused_changed.emit(False)
        return self._stop.is_set() or self._cancel.is_set()

    def _progress(self, added: int, folder: str) -> None:
        if self._stage != "registering":
            self._stage = "registering"
            self.stage.emit("registering")
        now = time.perf_counter()
        if now - self._progress_at >= 0.25:
            self._progress_at = now
            self.progress.emit(added, folder)

    def _open(self) -> names.NameIndex:
        if self.index is None:
            self.index = names.NameIndex(self.db)
            self.index.on_commit = self.changed.emit
        return self.index

    def request_stop(self) -> None:
        self._stop.set()
        with self._control:
            self._control.notify_all()

    def _run(self, work) -> None:
        if self._stop.is_set():
            return
        self.busy = True
        try:
            report = work(self._open())
            self.finished.emit({"added": report.added, "changed": report.changed, "removed": report.removed,
                                "unavailable": list(report.unavailable), "trouble": report.trouble,
                                "seconds": report.seconds, "stopped": report.stopped, **self._open().counts()})
        except Exception as error:  # noqa: BLE001
            log.exception("name list update failed")
            self.failed.emit("파일 이름 목록을 갱신하지 못했습니다. 자세한 내용은 app.log에 남겼습니다."
                             if not isinstance(error, (OSError, ValueError)) else friendly(error))
        finally:
            self.busy = False

    @Slot(list, list, list, list)
    def scan(self, roots: list, exclusions: list, code_roots: list, name_roots: list) -> None:
        self.scan_with(roots, exclusions, code_roots, name_roots, {})

    @Slot(list, list, list, list, object)
    def scan_with(self, roots: list, exclusions: list, code_roots: list, name_roots: list, options: dict) -> None:
        """고른 범위 전체를 맞춘다(처음 등록, 앱 시작, 폴더·제외 목록 변경, '색인 시작', '다시 등록').
        options: method(auto|ntfs|walk), prompt(이번에 UAC를 띄워도 되는가: 사용자가 누른 등록만), force(저장 상태를 무시하고 다시 등록).
        - walk: 기존처럼 폴더 순회. 검색·코드 폴더를 먼저, 이름 범위(드라이브 등)를 나중에 걷는다.
        - auto·ntfs: USN을 쓸 수 있는 볼륨은 저장한 위치부터 따라잡기만 하고(저장된 목록으로 바로 검색), 처음이거나 기록을 잃은
          볼륨만 다시 등록한다(ntfs + prompt면 MFT, 아니면 폴더 순회). 나머지(네트워크 등)는 폴더 순회. 일시정지·중지할 수 있다."""
        self.roots, self.exclusions = list(roots), list(exclusions)
        self.code_roots, self.name_roots = list(code_roots), list(name_roots)
        self.method = options.get("method", self.method)
        self._cancel.clear()
        self._run(lambda index: self._sync_all(index, bool(options.get("prompt")), bool(options.get("force"))))

    def _walk(self, index: names.NameIndex, report, only: list[str] | None) -> None:
        index.scan(self.roots, self.exclusions, should_stop=self._should_stop, progress=self._progress,
                   code_roots=self.code_roots, name_roots=self.name_roots, only=only, report=report)

    def _sync_all(self, index: names.NameIndex, prompt: bool, force: bool):
        started = time.perf_counter()
        report = names.ScanReport()
        self._stage = "checking"
        self.stage.emit("checking")
        index.set_scope(self.roots, self.exclusions, self.code_roots, self.name_roots)
        if self.method == "walk":
            for state in index.volume_states():
                index.forget_volume(state["root"])   # 폴더 순회로 돌아감: 감시 핸들을 다시 연다
            self._close_ntfs()
            self._walk(index, report, None)
            statuses = {}
        else:
            engine = self._engine(index)
            result = engine.sync(self.method, report, allow_prompt=prompt, should_stop=self._should_stop,
                                 progress=self._progress, walk=lambda roots: self._walk(index, report, roots),
                                 force=force)
            if result.walk_roots and not report.stopped:
                self._walk(index, report, result.walk_roots)
            live = {os.path.normcase(r) for r in names.outer_roots(index.roots)}
            for state in index.volume_states():
                if not any(os.path.normcase(r) in live for r in state["roots"]) or \
                        not any(scope.contains(state["root"], r) for r in index.roots):
                    index.forget_volume(state["root"])   # 이제 그 볼륨에 고른 범위가 없다
            statuses = engine.statuses
        report.trouble = index.db.execute("SELECT COUNT(*) FROM trouble").fetchone()[0]
        report.seconds = time.perf_counter() - started
        self._emit_method(index, statuses)
        return report

    def _engine(self, index: names.NameIndex) -> ntfs.NtfsNames:
        if self.ntfs is None or self.ntfs.index is not index:
            self._close_ntfs()
            self.ntfs = ntfs.NtfsNames(index, self._volume_factory, self._volume_of, self._helper)
        return self.ntfs

    def _close_ntfs(self) -> None:
        if self.ntfs is not None:
            self.ntfs.close()
            self.ntfs = None

    def _emit_method(self, index: names.NameIndex, statuses: dict) -> None:
        states = index.volume_states()
        self.method_state.emit({
            "method": self.method,
            "usn_roots": ntfs.usn_roots(states),
            "volumes": {s["root"]: {"complete": s["complete"], "registered_by": s["registered_by"] or "",
                                    "detail": s["detail"] or ""} for s in states},
            "roots": {r: {"method": v.method, "detail": v.detail, "registered_by": v.registered_by,
                          "unresolved": v.unresolved, "outside": v.outside} for r, v in statuses.items()}})

    @Slot()
    def poll(self) -> None:
        """실행 중 반영(앱 타이머가 약 1초마다, 등록 중이 아닐 때). 변경 기록을 잃은 볼륨은 그 범위만 폴더 순회로 다시 등록한다
        (자동으로 UAC를 띄우지 않는다)."""
        if self.method == "walk" or self.index is None or self.busy or self._stop.is_set():
            return
        engine = self._engine(self.index)
        report = names.ScanReport()
        try:
            lost = engine.poll(report, should_stop=self._stop.is_set)
        except Exception:  # noqa: BLE001
            log.exception("USN poll failed")
            return
        if lost and not self._stop.is_set():
            self.scan_with(self.roots, self.exclusions, self.code_roots, self.name_roots, {"method": self.method})

    @Slot(list, list)
    def apply(self, targets: list, rescans: list) -> None:
        """감시가 알려 준 변경만 맞춘다. 범위는 마지막 scan의 것."""
        if self.index is None:
            self._open().set_scope(self.roots, self.exclusions, self.code_roots, self.name_roots)
        self._run(lambda index: index.apply(list(targets), list(rescans), should_stop=self._stop.is_set))

    @Slot()
    def shutdown(self) -> None:
        self._close_ntfs()
        if self.index is not None:
            self.index.close()
            self.index = None


class NameSearchService(QObject):
    """이름 검색 전용 스레드: names.sqlite를 읽기만 하는 자기 연결로 찾는다. AI 검색 스레드(엔진 시작에 묶임)와 이름 등록
    스레드(쓰기)와 따로 돌아 서로를 기다리지 않는다(WAL)."""

    results = Signal(int, str, list)   # 요청 번호, 검색어, NameHit 목록

    def __init__(self, db: Path, roots: list[str] | None = None, exclusions: list[str] | None = None):
        super().__init__()
        self.db = Path(db)
        self.index: names.NameIndex | None = None
        self.roots = list(roots or [])
        self.exclusions = list(exclusions or [])
        self.latest = 0

    @Slot(int, str, object)
    def search(self, request: int, text: str, kinds: set[str] | None = None) -> None:
        if request < self.latest:
            return
        if code_files.query_text(text) is not None or not self.roots:
            self.results.emit(request, text, [])   # 코드 검색은 코드 색인만 본다
            return
        try:
            if self.index is None:
                self.index = names.NameIndex(self.db, readonly=True)
            hits = self.index.search(text, kinds=kinds, roots=self.roots, exclusions=self.exclusions)
        except Exception:  # noqa: BLE001
            # 이름 목록을 읽지 못해도 AI 검색은 계속된다. 다음 검색에서 다시 연다.
            log.exception("name search failed")
            self.shutdown()
            hits = []
        self.results.emit(request, text, hits)

    @Slot(list, list)
    def set_scope(self, roots: list, exclusions: list) -> None:
        self.roots, self.exclusions = list(roots), list(exclusions)

    @Slot()
    def shutdown(self) -> None:
        if self.index is not None:
            self.index.close()
            self.index = None


class IndexService(QObject):
    progress = Signal(int, int, str, str)   # 처리한 수, 전체, 경로, 결과
    status = Signal(str)
    finished = Signal(dict)                 # 요약
    failed = Signal(str)
    paused_changed = Signal(bool)
    planned = Signal(int)                   # 읽을 파일 목록을 정했다(남은 분석 수를 색인에서 셀 수 있다)

    def __init__(self, db: Path, embedder_factory=None, code_db: Path | None = None):
        super().__init__()
        self.db = Path(db)
        self.code_db = Path(code_db) if code_db else self.db.with_name("code.sqlite")
        self.code_roots: list[str] = []   # 코드 폴더 (코드 색인, 6단계)
        self.extra_files: list[str] = []  # '내용 분석'으로 하나씩 고른 파일 (이름 결과에서)
        self._stop = threading.Event()
        self._factory = embedder_factory or (lambda: engine.Embedder(should_stop=self._stop.is_set))
        self._embedder = None
        self.running = False
        self.paused = False
        self._pause = False
        self._control = threading.Condition()
        self.exclusions: list[str] = []

    def request_stop(self) -> None:
        """화면 스레드에서 부른다: 지금 파일이 끝나면 멈춘다."""
        self._stop.set()
        with self._control:
            self._control.notify_all()

    def prepare(self) -> None:
        """큐에 넣기 전에 초기화한다: 시작 직후 누른 중지가 run에서 지워지면 안 된다."""
        self._stop.clear()
        with self._control:
            self._pause = False

    def request_pause(self) -> None:
        with self._control:
            self._pause = True

    def resume(self) -> None:
        with self._control:
            self._pause = False
            self._control.notify_all()

    def _checkpoint(self) -> None:
        with self._control:
            if self._pause and not self._stop.is_set():
                self.paused = True
                self.paused_changed.emit(True)
                while self._pause and not self._stop.is_set():
                    self._control.wait()
                self.paused = False
                self.paused_changed.emit(False)

    def abort_engine(self) -> None:
        """앱을 끌 때 오래 걸리면: 엔진을 끊어 지금 파일을 되돌리고(rollback) 빨리 끝나게 한다."""
        self.request_stop()
        closer = getattr(self._embedder, "close", None)
        if closer:
            closer()

    def _engine(self):
        """색인 엔진은 필요할 때만 띄운다. 자동 갱신 뒤 남겨 둔 엔진이 있으면 다시 쓴다(새로 띄우면 약 2초)."""
        if self._embedder is None:
            self.status.emit("색인 엔진을 시작하는 중…")
            self._embedder = self._factory()
        return self._embedder

    def _close_engine(self) -> None:
        closer = getattr(self._embedder, "close", None)
        if closer:
            closer()
        self._embedder = None

    @Slot()
    def release_engine(self) -> None:
        """자동 갱신 뒤 한동안 변경이 없으면 색인 엔진(GPU 메모리)을 놓는다. 색인 스레드 큐로 받아 작업과 겹치지 않는다."""
        if not self.running:
            self._close_engine()

    @staticmethod
    def _summary(auto: bool = False) -> dict:
        return {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "done": 0, "failed": 0, "skipped": 0,
                "stopped": False, "errors": [], "results": {}, "auto": auto, "unavailable": []}

    @staticmethod
    def _add_report(summary: dict, report) -> None:
        for key in ("added", "changed", "removed", "unchanged"):
            summary[key] += getattr(report, key)
        summary["stopped"] = summary["stopped"] or report.stopped
        for path, result in report.results.items():
            summary["results"][path] = result
            state = result.split(":", 1)[0]
            if state in ("done", "partial"):
                summary["done"] += 1
            elif state == "failed":
                summary["failed"] += 1
                summary["errors"].append(f"{Path(path).name}: {result.split(':', 1)[1].strip()}")
            else:
                summary["skipped"] += 1

    def _stopped_by_engine(self, error: BaseException) -> bool:
        return self._stop.is_set() and (isinstance(error, requests.RequestException)
                                        or str(error) == "llama-server startup stopped")

    @Slot(list)
    def run(self, folders: list) -> None:
        self.running = True
        summary = self._summary()
        try:
            code_roots = list(self.code_roots)
            for folder in list(folders) + code_roots:
                problem = check_folder(folder)
                if problem:
                    raise ValueError(root_problem(folder))
            self.status.emit("파일 목록을 살피는 중…")
            # 일반 색인 다음에 코드 색인 (코드 폴더가 있을 때만 code.sqlite를 만든다)
            for db, roots, code in ((self.db, list(folders), False), (self.code_db, code_roots, True)):
                if not roots or self._stop.is_set() and summary["stopped"]:
                    continue
                if code:
                    self.status.emit("코드 파일 목록을 살피는 중…")
                with IndexStore(db) as store:
                    indexer = Indexer(store, self._engine, code=code)
                    if indexer.versions.requeued:
                        self.status.emit(f"설정이 바뀐 파일 {indexer.versions.requeued}개를 다시 읽습니다.")
                    report = indexer.sync(roots, progress=lambda *a: self.progress.emit(*a),
                                          should_stop=self._stop.is_set, checkpoint=self._checkpoint,
                                          exclusions=list(self.exclusions), on_plan=self._plan)
                self._add_report(summary, report)
                if code:
                    summary["code_done"] = sum(r.startswith("done") for r in report.results.values())
            if self.extra_files and not self._stop.is_set():
                self._files(list(self.extra_files), summary)
            self.finished.emit(summary)
        except Exception as error:  # noqa: BLE001
            if self._stopped_by_engine(error):
                summary["stopped"] = True
                self.finished.emit(summary)   # 앱 종료로 엔진을 끊은 경우: 오류가 아니다
            else:
                log.exception("indexing failed")
                self.failed.emit(friendly(error))
        finally:
            self._close_engine()
            self.running = False
            self.paused = False

    def _plan(self, count: int, seconds: float) -> None:
        self.planned.emit(count)
        if count:
            self.status.emit(f"분석할 파일 {count}개 · 예상 약 {_duration(seconds)} (최근 파일·작은 문서부터)")

    def _files(self, paths: list, summary: dict) -> None:
        with IndexStore(self.db) as store:
            indexer = Indexer(store, self._engine)
            self._add_report(summary, indexer.sync_files(paths, progress=lambda *a: self.progress.emit(*a),
                                                         should_stop=self._stop.is_set, checkpoint=self._checkpoint,
                                                         on_plan=self._plan))

    @Slot(list)
    def run_files(self, paths: list) -> None:
        """이름 결과에서 '내용 분석'을 고른 파일만 읽는다. 같은 색인 스레드라 다른 작업과 겹치지 않는다."""
        self.running = True
        summary = self._summary()
        summary["files"] = True
        try:
            self._files(list(paths), summary)
            self.finished.emit(summary)
        except Exception as error:  # noqa: BLE001
            if self._stopped_by_engine(error):
                summary["stopped"] = True
                self.finished.emit(summary)
            else:
                log.exception("analysing chosen files failed")
                self.failed.emit(friendly(error))
        finally:
            self._close_engine()
            self.running = False
            self.paused = False

    def _changes(self, db: Path, roots: list, targets: list, rescans: list, code: bool, summary: dict) -> None:
        usable = [r for r in roots if not root_problem(r)]
        summary["unavailable"] += [r for r in roots if r not in usable]
        mine = [t for t in targets if scope.allows(t, usable, self.exclusions)]   # 이 색인의 폴더 안 변경만
        with IndexStore(db) as store:
            indexer = Indexer(store, self._engine, code=code)
            options = dict(progress=lambda *a: self.progress.emit(*a), should_stop=self._stop.is_set,
                           checkpoint=self._checkpoint, exclusions=list(self.exclusions))
            for folder in rescans:
                if self._stop.is_set():
                    summary["stopped"] = True
                    return
                if not store.files_under(str(Path(folder).resolve())) or not scope.allows(folder, usable,
                                                                                        self.exclusions):
                    continue
                if any(scope.contains(folder, r) and scope.contains(r, folder) for r in usable):
                    self.status.emit("놓친 변경이 있는지 확인하는 중…")
                    self._add_report(summary, indexer.sync([folder], **options))
                else:
                    self._add_report(summary, indexer.sync_paths(usable, [folder], **options))
            if mine and not self._stop.is_set():
                self._add_report(summary, indexer.sync_paths(usable, mine, **options))
            elif mine:
                summary["stopped"] = True
            # '내용 분석'으로 고른 파일의 변경 (고른 폴더 밖이라 위에서 빠진다)
            chosen = {os.path.normcase(os.path.abspath(f)) for f in self.extra_files}
            touched = [t for t in targets if os.path.normcase(os.path.abspath(t)) in chosen]
            if touched and not code and not self._stop.is_set():
                self._add_report(summary, indexer.sync_files(touched, progress=options["progress"],
                                                             should_stop=self._stop.is_set,
                                                             checkpoint=self._checkpoint))

    @Slot(list, list, list)
    def run_changes(self, roots: list, targets: list, rescans: list) -> None:
        """감시가 알려 준 변경만 반영한다(6단계). 같은 색인 스레드에서 돌므로 수동 색인과 겹치지 않고, 일시정지·중지도
        같은 체크포인트를 쓴다. `rescans`(놓친 변경 확인)는 이미 색인한 폴더만 다시 살핀다: 처음 색인은 사용자가 시작한다.
        열 수 없는 고른 폴더는 건너뛰고 그 색인은 지우지 않는다. 엔진은 끄지 않고 남겨 다음 변경을 빨리 읽는다."""
        self.running = True
        summary = self._summary(auto=True)
        try:
            # 일반 색인과 코드 색인 각각: 아직 만든 적이 없는 색인은 자동 갱신이 만들지 않는다(처음 색인은 사용자가)
            for db, folders, code in ((self.db, list(roots), False), (self.code_db, list(self.code_roots), True)):
                if (folders or (not code and self.extra_files)) and db.exists():
                    self._changes(db, folders, targets, rescans, code, summary)
            self.finished.emit(summary)
        except Exception as error:  # noqa: BLE001
            self._close_engine()      # 끊긴 엔진은 다시 쓰지 않는다
            if self._stopped_by_engine(error):
                summary["stopped"] = True
                self.finished.emit(summary)
            else:
                log.exception("automatic indexing failed")
                self.failed.emit(friendly(error))
        finally:
            if self._stop.is_set():
                self._close_engine()  # 중지·종료: 엔진도 끈다 (수동 색인의 중지와 같게)
            self.running = False
            self.paused = False


class StatsService(QObject):
    """색인 현황(stats.collect)을 자기 스레드에서 센다: 큰 색인에서도 화면이 멈추지 않게. 색인은 읽기만 한다."""

    ready = Signal(dict)

    @Slot(object)
    def collect(self, request: dict) -> None:
        try:
            data = stats.collect(**request)
        except Exception as error:  # noqa: BLE001
            log.exception("index stats failed")
            data = {"error": str(error)}
        self.ready.emit(data)
