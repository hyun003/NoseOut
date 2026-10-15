"""고른 폴더의 변경을 감시해 색인에 자동 반영한다 (6단계 첫 항목).

원본 DigUp은 macOS FSEvents로 폴더를 감시한다. 여기서는 추가 패키지 없이 Win32 ReadDirectoryChangesW를 ctypes로 부른다
(하위 폴더 포함, overlapped I/O라 종료할 때 기다리지 않고 취소할 수 있다).

- DirectoryWatcher: 고른 폴더 하나를 자기 스레드에서 감시한다. 이벤트 버퍼가 넘치면(overflow) 그 폴더 전체를 다시
  확인하라고 알리고, 폴더가 사라지거나 열리지 않으면 삭제로 보지 않고 다시 열릴 때까지 기다린 뒤 전체를 다시 확인한다.
- ChangeBatcher: 연속 이벤트를 경로별로 모은다. 마지막 이벤트 뒤 QUIET초 동안 조용하고 크기·수정 시각이 그대로일 때만
  내보낸다(복사·저장 중인 파일을 반쯤 읽지 않게). 잠긴 파일은 점점 긴 간격으로 다시 시도한다. 시계를 주입해 시험한다.
- WatchService: 화면 스레드에서 위 둘을 묶는다. 실제 색인은 기존 IndexService 스레드가 하므로 수동 색인과 순서대로
  돌고, 일시정지·중지 체크포인트도 같다. 색인 중·일시정지 중·중지 뒤에는 변경을 모아 두기만 한다.

사용자 파일은 읽기만 한다. 감시 핸들은 FILE_SHARE_READ|WRITE|DELETE로 열어 다른 프로그램의 쓰기·지우기를 막지 않는다.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sqlite3
import stat
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from . import rules, scope
from .crawler import root_problem

log = logging.getLogger("noseout")

QUIET = 0.4                   # 마지막 이벤트 뒤 이만큼 조용해야 읽는다 (초)
RETRY_DELAYS = (1, 2, 4, 8, 15, 30)   # 잠긴·저장 중인 파일 다시 시도 간격. 다 쓰면 다음 색인 때 확인한다
MAX_PATHS = 5000              # 모은 경로가 이보다 많으면 그 폴더 전체 다시 확인으로 바꾼다
MAX_BATCH = 500               # 한 번에 넘기는 경로 수
ENGINE_IDLE = 60.0            # 자동 갱신 뒤 색인 엔진을 이만큼 더 띄워 두어 다음 변경을 빨리 읽는다 (초)
REOPEN_SECONDS = 2.0          # 열리지 않는 폴더를 다시 열어 보는 간격
TICK_MS = 100
NAME_QUIET = 0.15             # 이름 목록 갱신: 이름·존재만 보므로 파일이 다 써지기를 기다리지 않는다(초)
# 이름만 검색하는 범위(드라이브 전체 등)의 전체 다시 걷기 최소 간격(초). 드라이브 루트 감시는 앱 데이터·캐시 쓰기까지 받아
# 알림 버퍼가 넘치기 쉽고, 넘칠 때마다 드라이브 전체를 다시 걸으면 계속 디스크를 읽게 된다. 그 사이의 놓친 이름은 다음
# 다시 걷기까지 늦게 반영된다(문서에 한계로 적음).
NAME_ROOT_RESCAN_INTERVAL = 600.0

# Win32
_FILE_LIST_DIRECTORY = 0x0001
_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_BACKUP_SEMANTICS = 0x02000000
_OVERLAPPED_FLAG = 0x40000000
# 읽기 접근 시각(LAST_ACCESS)은 듣지 않는다: 색인이 파일을 읽을 때마다 이벤트가 되돌아오지 않게
NOTIFY_FILTER = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x40   # 파일 이름, 폴더 이름, 특성, 크기, 수정 시각, 만든 시각
_ERROR_IO_PENDING = 997
_ERROR_OPERATION_ABORTED = 995
_ERROR_NOTIFY_ENUM_DIR = 1022
_WAIT_OBJECT_0 = 0
_BUFFER = 64 * 1024           # 네트워크 폴더의 상한도 64KB

ACTIONS = {1: "added", 2: "removed", 3: "modified", 4: "renamed_from", 5: "renamed_to"}


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        ctypes.POINTER(_OVERLAPPED), wintypes.LPVOID]
    k.ReadDirectoryChangesW.restype = wintypes.BOOL
    k.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                      wintypes.BOOL]
    k.GetOverlappedResult.restype = wintypes.BOOL
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.ResetEvent.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


_INVALID = wintypes.HANDLE(-1).value


def parse_notifications(buffer: bytes, size: int) -> list[tuple[str, str]]:
    """FILE_NOTIFY_INFORMATION 목록 → [(동작, 고른 폴더 기준 상대 경로)]."""
    out = []
    offset = 0
    while offset < size:
        next_offset, action, length = (int.from_bytes(buffer[offset + i:offset + i + 4], "little") for i in (0, 4, 8))
        name = buffer[offset + 12:offset + 12 + length].decode("utf-16-le", errors="replace")
        out.append((ACTIONS.get(action, "modified"), name))
        if not next_offset:
            break
        offset += next_offset
    return out


def _identity(root: str) -> tuple[int, int] | None:
    """고른 폴더가 그대로인지(지워지거나 다른 폴더·링크로 바뀌지 않았는지) 확인하는 값."""
    try:
        st = os.stat(root, follow_symlinks=False)
    except OSError:
        return None
    if rules.is_link(st) or not stat.S_ISDIR(st.st_mode):
        return None
    return st.st_ino, st.st_dev


class DirectoryWatcher:
    """고른 폴더 하나를 감시한다. sink(종류, 고른 폴더, 경로, 시각)를 감시 스레드에서 부른다.
    종류: ACTIONS의 값(경로가 바뀜), "rescan"(폴더 전체를 다시 확인), "error"(로그용 이유), "ok"(다시 열림)."""

    def __init__(self, root: str, sink: Callable[[str, str, str | None, float], None]):
        self.root = root
        self.sink = sink
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"watch:{Path(root).name}", daemon=True)
        self.available = False

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        k = _kernel32()
        event = k.CreateEventW(None, True, False, None)
        buffer = ctypes.create_string_buffer(_BUFFER)
        # 처음 시작할 때의 놓친 변경은 앱이 따로 확인한다(WatchService.reconcile). 감시가 끊겼다가 실제로 다시 시작된
        # 뒤에만 전체를 다시 확인한다. 계속 실패하는 폴더(알림을 지원하지 않는 드라이브 등)는 간격을 늘려 다시 열어
        # 보되, 감시가 시작되지 않는 한 전체 다시 확인을 반복하지 않는다.
        state = {"reported": False, "missed": False}
        failures = 0

        def ready() -> None:
            if state["reported"] or state["missed"]:
                self.sink("ok", self.root, None, time.perf_counter())
                self.sink("rescan", self.root, None, time.perf_counter())
            state["reported"] = state["missed"] = False

        def report(reason: str) -> None:
            if not state["reported"]:
                state["reported"] = True
                self.sink("error", self.root, reason, time.perf_counter())

        try:
            while not self._stop.is_set():
                problem = root_problem(self.root)
                handle = None if problem else k.CreateFileW(
                    self.root, _FILE_LIST_DIRECTORY, _SHARE_ALL, None, _OPEN_EXISTING,
                    _BACKUP_SEMANTICS | _OVERLAPPED_FLAG, None)
                started = False
                if handle in (None, _INVALID, 0):
                    report(problem or f"open failed ({ctypes.get_last_error()})")
                else:
                    self.available = True
                    try:
                        started = self._listen(k, handle, event, buffer, _identity(self.root), ready, report)
                    finally:
                        k.CloseHandle(handle)
                        self.available = False
                if self._stop.is_set():
                    break
                state["missed"] = True       # 감시가 끊긴 동안의 변경은 다시 시작한 뒤 전체 확인으로 찾는다
                failures = 0 if started else failures + 1
                self._stop.wait(min(60.0, REOPEN_SECONDS * 2 ** max(0, failures - 1)))
        except Exception:  # noqa: BLE001
            log.exception("watcher failed: %s", self.root)
            self.sink("error", self.root, "watcher failed", time.perf_counter())
        finally:
            k.CloseHandle(event)

    def _listen(self, k, handle, event, buffer, identity, ready, report) -> bool:
        """감시를 시작했으면 True(그 뒤 끊김), 처음 읽기 요청부터 거부되면 False."""
        started = False
        while not self._stop.is_set():
            overlapped = _OVERLAPPED()
            overlapped.hEvent = event
            k.ResetEvent(event)
            if not k.ReadDirectoryChangesW(handle, buffer, _BUFFER, True, NOTIFY_FILTER, None,
                                           ctypes.byref(overlapped), None):
                error = ctypes.get_last_error()
                if error != _ERROR_IO_PENDING:
                    report(f"watch failed ({error})")
                    return started
            if not started:
                started = True
                ready()
            while True:
                if k.WaitForSingleObject(event, 500) == _WAIT_OBJECT_0:
                    break
                if self._stop.is_set() or _identity(self.root) != identity:
                    # 종료하거나 고른 폴더가 지워지거나 바뀜: 진행 중인 읽기를 취소하고 끝날 때까지 기다린 뒤 버퍼를 놓는다
                    k.CancelIoEx(handle, ctypes.byref(overlapped))
                    k.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(wintypes.DWORD()), True)
                    if not self._stop.is_set():
                        report("folder missing or replaced")
                    return started
            received = time.perf_counter()
            size = wintypes.DWORD()
            if not k.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(size), False):
                error = ctypes.get_last_error()
                if error == _ERROR_NOTIFY_ENUM_DIR:
                    self.sink("rescan", self.root, None, received)
                    continue
                if error != _ERROR_OPERATION_ABORTED:
                    report(f"watch failed ({error})")
                return started
            if size.value == 0:
                self.sink("rescan", self.root, None, received)   # 버퍼가 넘쳐 개별 이벤트를 잃었다
                continue
            for action, name in parse_notifications(buffer.raw, size.value):
                self.sink(action, self.root, os.path.join(self.root, name), received)


# 이벤트 묶기


def signature(path: str) -> tuple | None:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, stat.S_ISDIR(st.st_mode)


@dataclass
class _Entry:
    last: float
    sig: tuple | None
    first: float
    attempt: int = 0          # 다시 시도 횟수 (0 = 새 변경)


@dataclass
class Batch:
    targets: list[str] = field(default_factory=list)
    rescans: list[str] = field(default_factory=list)
    detected: dict[str, float] = field(default_factory=dict)   # 경로 → 처음 감지한 시각 (지연 측정용)

    def __bool__(self) -> bool:
        return bool(self.targets or self.rescans)


class ChangeBatcher:
    def __init__(self, clock: Callable[[], float] = time.perf_counter,
                 sig: Callable[[str], tuple | None] = signature):
        self.clock, self.sig = clock, sig
        self.entries: dict[str, _Entry] = {}
        self.rescans: dict[str, float] = {}
        self.given_up: set[str] = set()
        self.paths: dict[str, str] = {}      # normcase 키 → 원래 경로
        self.attempts: dict[str, int] = {}   # 다시 시도한 횟수

    def __len__(self) -> int:
        return len(self.entries) + len(self.rescans)

    def add(self, path: str, at: float | None = None) -> None:
        now = self.clock() if at is None else at
        key = os.path.normcase(path)
        entry = self.entries.get(key)
        if entry:
            entry.last, entry.attempt = max(entry.last, now), 0
        else:
            self.entries[key] = _Entry(now, self.sig(path), now)
            self.paths[key] = path
        self.given_up.discard(key)

    def rescan(self, folder: str, at: float | None = None) -> None:
        key = os.path.normcase(folder)
        self.rescans.setdefault(key, self.clock() if at is None else at)
        self.paths[key] = folder
        # 그 폴더 아래 개별 경로는 전체 확인에 들어간다
        for path in [p for p in self.entries if scope.contains(folder, p)]:
            del self.entries[path]

    def retry(self, path: str) -> bool:
        """잠긴·저장 중인 파일을 나중에 다시. 횟수를 다 쓰면 False(다음 색인 때 확인)."""
        key = os.path.normcase(path)
        attempt = self.attempts.get(key, 0)
        if attempt >= len(RETRY_DELAYS):
            self.attempts.pop(key, None)
            self.given_up.add(key)
            return False
        self.attempts[key] = attempt + 1
        now = self.clock()
        # 다시 시도할 시각을 last로 미뤄 두고, QUIET 대신 지연을 기다리게 한다
        self.entries[key] = _Entry(now + RETRY_DELAYS[attempt] - QUIET, self.sig(path), now, attempt + 1)
        self.paths[key] = path
        return True

    def succeeded(self, path: str) -> None:
        self.attempts.pop(os.path.normcase(path), None)

    def drop_outside(self, keep: Callable[[str], bool]) -> None:
        for key in [k for k in self.entries if not keep(self.paths.get(k, k))]:
            del self.entries[key]
        for key in [k for k in self.rescans if not keep(self.paths.get(k, k))]:
            del self.rescans[key]

    def clear(self) -> None:
        self.entries.clear()
        self.rescans.clear()
        self.attempts.clear()
        self.given_up.clear()

    def collapse(self, root_of: Callable[[str], str | None]) -> None:
        """경로가 너무 많이 쌓이면(오래 멈춘 뒤 등) 폴더 전체 다시 확인으로 바꾼다."""
        if len(self.entries) <= MAX_PATHS:
            return
        for key in list(self.entries):
            root = root_of(self.paths.get(key, key))
            if root:
                self.rescan(root)
        self.entries.clear()

    def waiting_retries(self) -> int:
        return sum(1 for e in self.entries.values() if e.attempt)

    def due(self) -> Batch:
        now = self.clock()
        batch = Batch()
        for key, at in list(self.rescans.items()):
            if now - at >= QUIET:
                batch.rescans.append(self.paths.get(key, key))
                batch.detected[self.paths.get(key, key)] = at
                del self.rescans[key]
        for key, entry in list(self.entries.items()):
            if len(batch.targets) >= MAX_BATCH:
                break
            if now - entry.last < QUIET:
                continue
            path = self.paths.get(key, key)
            current = self.sig(path)
            if current != entry.sig:
                # 아직 쓰는 중(크기·수정 시각이 바뀜): 한 번 더 조용할 때까지 기다린다
                entry.sig, entry.last = current, now
                continue
            batch.targets.append(path)
            batch.detected[path] = entry.first
            del self.entries[key]
        return batch


class WatchService(QObject):
    """화면 스레드에서 감시 스레드들의 이벤트를 모아, 색인이 쉬고 있을 때 자동 갱신 작업을 내보낸다."""

    dispatch = Signal(list, list)       # 바뀐 경로들, 전체를 다시 확인할 폴더들
    names_changed = Signal(list, list)  # 이름 목록용 (바뀐 경로들, 다시 걸을 폴더들): AI 갱신과 따로, 기다리지 않고
    state_changed = Signal(str)         # 사용자용 한 줄 상태
    engine_idle = Signal()              # 자동 갱신 뒤 한동안 변경이 없음: 색인 엔진을 꺼도 된다

    def __init__(self, roots: list[str], exclusions: list[str], ignore: list[str | Path] | None = None,
                 enabled: bool = True, busy: Callable[[], bool] = lambda: False,
                 watcher_factory=DirectoryWatcher, clock: Callable[[], float] = time.perf_counter,
                 index: str | Path | list | None = None, name_roots: list[str] | None = None,
                 extra_files: list[str] | None = None):
        super().__init__()
        # 색인 파일들(일반 색인, 코드 색인). "색인에 있던 경로인가"는 어느 하나에라도 있으면 참이다.
        indexes = index if isinstance(index, list) else [index] if index else []
        self.indexes = [Path(i) for i in indexes]
        self.index = self.indexes[0] if self.indexes else None
        self._readers: dict[Path, sqlite3.Connection] = {}
        self.roots, self.exclusions = list(roots), list(exclusions)
        # 이름만 검색하는 범위(드라이브 전체 등): 이름 목록에만 반영한다. AI 갱신은 고른 폴더와 '내용 분석'으로 고른 파일만.
        self.name_roots = list(name_roots or [])
        # USN으로 이름 목록을 맞추는 범위(ntfs.py): 이름 범위에 감시 핸들을 열지 않고, 그 안의 이름 변경·전체 다시 걷기를 보내지
        # 않는다(같은 변경을 두 번, 드라이브 전체를 다시 걷지 않게). AI 분석 폴더의 감시는 그대로다.
        self.usn_roots: list[str] = []
        self.extra = {os.path.normcase(os.path.abspath(f)) for f in extra_files or []}
        self._name_rescanned: dict[str, float] = {}
        self.ignore = [str(Path(p).resolve()) for p in (ignore or [])]
        self.enabled = enabled
        self.held = False                # 중지 뒤: 다음 '색인 시작'까지 자동 갱신을 하지 않는다
        self.busy = busy
        self.factory = watcher_factory
        self.batcher = ChangeBatcher(clock)
        self.clock = clock
        # 이름 목록(names.py)에 보낼 변경. AI 색인의 규칙(_relevant: 색인할 종류만, 색인 중·중지 뒤 미룸, 색인이 없으면
        # 버림)과 따로 모은다: 이름 목록은 모든 파일을 담고, AI 분석이 도는 동안에도 바로 반영해야 한다.
        self.name_paths: dict[str, str] = {}
        self.name_rescans: dict[str, str] = {}
        self.name_last = 0.0
        self.events: queue.Queue = queue.Queue()
        self.watchers: dict[str, DirectoryWatcher] = {}
        self.errors: dict[str, str] = {}
        self.timeline: dict[str, dict[str, float]] = {}   # 경로 → 첫·마지막 감지, 작업 시작 시각 (측정 스크립트용)
        self.state = ""
        self.running_job = False
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        self.idle = QTimer(self)
        self.idle.setSingleShot(True)
        self.idle.setInterval(int(ENGINE_IDLE * 1000))
        self.idle.timeout.connect(self.engine_idle)

    # 범위

    def _resolved(self, root: str) -> str:
        try:
            return str(Path(root).resolve())
        except OSError:
            return os.path.abspath(root)

    def start(self) -> None:
        self.timer.start()
        self._sync_watchers()
        self._update_state()

    def shutdown(self) -> None:
        self.timer.stop()
        self.idle.stop()
        for reader in self._readers.values():
            reader.close()
        self._readers.clear()
        for watcher in self.watchers.values():
            watcher._stop.set()
        for watcher in self.watchers.values():
            watcher.stop()
        self.watchers.clear()

    @property
    def name_roots(self) -> list[str]:
        return self._name_roots

    @name_roots.setter
    def name_roots(self, roots: list[str]) -> None:
        self._name_roots = list(roots)
        self._resolved_name_roots = [self._resolved(r) for r in self._name_roots]

    def _watched(self) -> list[str]:
        """감시 핸들을 여는 폴더: 고른 폴더와 이름 범위를 합쳐 바깥 것만(드라이브를 감시하면 그 안의 폴더는 따로 열지 않는다).
        USN이 맡은 이름 범위는 빼고, 그 안의 AI 분석 폴더는 따로 연다."""
        out: list[str] = []
        names_only = [r for r in self._resolved_name_roots if not self._usn_covered(r)]
        for root in sorted({os.path.normcase(r): r for r in self._resolved_roots + names_only}.values(),
                           key=len):
            if not any(scope.contains(o, root) for o in out):
                out.append(root)
        return out

    def _usn_covered(self, path: str) -> bool:
        return any(scope.contains(r, path) for r in self.usn_roots)

    def set_usn_roots(self, roots: list[str]) -> None:
        resolved = [self._resolved(r) for r in roots]
        if sorted(map(os.path.normcase, resolved)) == sorted(map(os.path.normcase, self.usn_roots)):
            return
        self.usn_roots = resolved
        self.name_paths = {k: p for k, p in self.name_paths.items() if not self._usn_covered(p)}
        self.name_rescans = {k: f for k, f in self.name_rescans.items() if not self._usn_covered(f)}
        self._sync_watchers()
        self._update_state()

    def set_extra(self, files: list[str]) -> None:
        self.extra = {os.path.normcase(os.path.abspath(f)) for f in files}

    def _sync_watchers(self) -> None:
        wanted = {os.path.normcase(r): r for r in self._watched()} if self.enabled else {}
        for key in [k for k in self.watchers if k not in wanted]:
            self.watchers.pop(key).stop()
            self.errors.pop(key, None)
        for key, root in wanted.items():
            if key not in self.watchers:
                watcher = self.factory(root, self._sink)
                self.watchers[key] = watcher
                watcher.start()

    def set_scope(self, roots: list[str], exclusions: list[str], name_roots: list[str] | None = None) -> None:
        """폴더 추가·제거·제외·제외 해제. 새로 보이게 된 폴더는 놓친 변경이 있을 수 있어 다시 확인한다."""
        old_roots, old_exclusions = self.roots, self.exclusions
        self.roots, self.exclusions = list(roots), list(exclusions)
        if name_roots is not None:
            self.name_roots = list(name_roots)
        self._sync_watchers()
        self.batcher.drop_outside(self.ai_allows)
        self.name_paths = {k: p for k, p in self.name_paths.items() if self.allows(p)}
        if self.enabled:
            added = [r for r in self.roots if not any(os.path.normcase(r) == os.path.normcase(o) for o in old_roots)]
            revealed = [e for e in old_exclusions
                        if not scope.excluded(e, self.exclusions) and scope.excluded(e, self.roots)]
            for folder in added + revealed:
                self.batcher.rescan(self._resolved(folder))
        self._update_state()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.batcher.clear()
            self.name_paths.clear()
            self.name_rescans.clear()
        self._sync_watchers()
        if enabled:
            self.reconcile(names=True)   # 꺼져 있던 동안의 변경을 확인한다
        self._update_state()

    def reconcile(self, folders: list[str] | None = None, names: bool = False) -> None:
        """앱 시작·감시 재개 때 놓친 변경을 확인한다. 실제로는 이미 색인한 폴더만 다시 확인한다(IndexService).
        `names`: 이름 목록도 다시 걷는다(감시 재개). 앱 시작 때는 앱이 이름 목록 전체 확인을 따로 부른다."""
        if not self.enabled:
            return
        for folder in folders if folders is not None else self.roots:
            self.batcher.rescan(self._resolved(folder))
            if names:
                self.name_rescans[os.path.normcase(self._resolved(folder))] = self._resolved(folder)
                self.name_last = self.clock()
        if names and folders is None:
            for folder in self._resolved_name_roots:
                self.name_rescans[os.path.normcase(folder)] = folder
        if names:
            self.name_rescans = {k: f for k, f in self.name_rescans.items() if not self._usn_covered(f)}
        self._update_state()

    @property
    def roots(self) -> list[str]:
        return self._roots

    @roots.setter
    def roots(self, roots: list[str]) -> None:
        self._roots = list(roots)
        self._resolved_roots = [self._resolved(r) for r in self._roots]   # 이벤트마다 resolve하지 않게

    def allows(self, path: str) -> bool:
        """이름 목록의 범위(고른 폴더 + 이름 범위). 무시 목록(설정 폴더, 시스템 폴더)은 이벤트 단계에서 버린다."""
        if any(scope.contains(i, path) for i in self.ignore):
            return False
        return scope.allows(path, self._resolved_roots + self._resolved_name_roots, self.exclusions)

    def ai_allows(self, path: str) -> bool:
        """AI 갱신의 범위: 고른 폴더와 '내용 분석'으로 고른 파일만(이름 범위의 나머지는 이름 목록에만)."""
        if any(scope.contains(i, path) for i in self.ignore):
            return False
        return scope.allows(path, self._resolved_roots, self.exclusions) or \
            os.path.normcase(os.path.abspath(path)) in self.extra

    def _root_of(self, path: str) -> str | None:
        owners = [r for r in self._resolved_roots if scope.contains(r, path)]
        return max(owners, key=len) if owners else None

    # 이벤트

    def _sink(self, kind: str, root: str, path: str | None, at: float) -> None:
        self.events.put((kind, root, path, at))   # 감시 스레드: 큐에만 넣는다

    def _known(self, path: str) -> bool:
        """색인에 그 경로(폴더면 그 아래)가 있는가. 읽기 전용 연결이라 WAL 색인 쓰기를 막지 않는다.
        확인할 수 없으면 있다고 본다(지운 파일이 검색에 남는 것보다 빈 작업 하나가 낫다)."""
        if not self.indexes:
            return True
        prefix = path.rstrip("\\/") + "\\"
        for index in self.indexes:
            try:
                reader = self._readers.get(index)
                if reader is None:
                    if not index.exists():
                        continue
                    reader = self._readers[index] = sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro", uri=True,
                                                                     timeout=0.5)
                if reader.execute("SELECT 1 FROM files WHERE path = ? COLLATE NOCASE OR "
                                  "(path >= ? COLLATE NOCASE AND path < ? COLLATE NOCASE) LIMIT 1",
                                  (path, prefix, prefix[:-1] + "]")).fetchone() is not None:
                    return True
            except sqlite3.Error:
                log.warning("watch could not read the index", exc_info=True)
                return True
        return False

    def _relevant(self, action: str, path: str) -> str | None:
        """색인과 관계없는 이벤트는 버린다: 로그·임시 파일, 폴더 자체의 '수정'(안의 파일이 바뀔 때마다 생기고, 파일은
        자기 이벤트가 따로 온다), 색인에 없던 파일의 삭제. 프로젝트 표지가 바뀌면 그 폴더 전체를 다시 본다."""
        name = os.path.basename(path)
        if name in rules.PROJECT_MARKERS or rules.extension(name) in rules.PROJECT_BUNDLE_EXTENSIONS:
            return os.path.dirname(path)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return path if self._known(path) else None   # 지워짐·이름 바뀜: 색인에 있었으면 맞춘다
        except OSError:
            return path       # 열 수 없음: crawl_targets가 삭제로 보지 않고 남겨 둔다
        if stat.S_ISDIR(st.st_mode) and not rules.is_link(st):
            return None if action == "modified" else path
        # 일반 색인할 파일이나 코드 색인할 파일(코드 폴더 안이 아니면 코드 색인이 알아서 건너뛴다)
        if rules.classify(name)[0] == "index" or rules.classify_code(name)[0] == "index" or self._known(path):
            return path
        return None

    def tick(self) -> None:
        changed = False
        while True:
            try:
                kind, root, path, at = self.events.get_nowait()
            except queue.Empty:
                break
            key = os.path.normcase(root)
            if key not in self.watchers:
                continue          # 이미 목록에서 뺀 폴더
            changed = True
            if kind in ACTIONS.values() and path:
                if not self.enabled or not self.allows(path):
                    continue
                if kind != "modified" and not self._usn_covered(path):
                    # 이름 목록: 추가·삭제·이름 변경만 (내용 수정은 이름이 그대로라 큰 폴더를 다시 읽지 않는다)
                    self.name_paths[os.path.normcase(path)] = path
                    self.name_last = at
                if not self.ai_allows(path):
                    continue
                target = self._relevant(kind, path)
                if target is None or not self.ai_allows(target):
                    continue
                self.batcher.add(target, at)
                entry = self.timeline.get(os.path.normcase(target))
                if entry is None or "dispatch" in entry:
                    self.timeline[os.path.normcase(target)] = {"event": at, "last": at}
                else:
                    entry["last"] = at
            elif kind == "rescan":
                # 감시 핸들은 바깥 폴더(드라이브 등)에만 있으므로 그 안의 고른 폴더를 AI 쪽에서 다시 확인한다
                for chosen in self._resolved_roots:
                    if scope.contains(root, chosen) or scope.contains(chosen, root):
                        self.batcher.rescan(chosen if scope.contains(root, chosen) else root, at)
                if not self._usn_covered(root):
                    self.name_rescans[os.path.normcase(root)] = root
                    self.name_last = at
            elif kind == "error":
                self.errors[key] = path or ""
                log.warning("watch problem %s: %s", root, path)
            elif kind == "ok":
                self.errors.pop(key, None)
        self.batcher.collapse(self._root_of)
        self._flush_names()
        if self.indexes and not any(i.exists() for i in self.indexes) and len(self.batcher):
            # 아직 색인을 만든 적이 없다: 맞출 것이 없다 (처음 색인은 사용자가 '색인 시작'으로)
            self.batcher.clear()
            changed = True
        if self.enabled and not self.held and not self.running_job and not self.busy():
            batch = self.batcher.due()
            if batch:
                now = time.perf_counter()
                for path in batch.targets + batch.rescans:
                    self.timeline.setdefault(os.path.normcase(path), {})["dispatch"] = now
                self.running_job = True
                self.idle.stop()
                changed = True
                self.dispatch.emit(batch.targets, batch.rescans)
        if changed:
            self._update_state()

    def _flush_names(self) -> None:
        """이름 변경은 AI 작업의 busy·held·색인 유무와 상관없이 잠깐(NAME_QUIET) 모았다가 바로 내보낸다."""
        if not self.enabled or not (self.name_paths or self.name_rescans):
            return
        if self.clock() - self.name_last < NAME_QUIET:
            return
        if not self.name_paths and all(
                self.clock() - self._name_rescanned.get(k, -1e9) < NAME_ROOT_RESCAN_INTERVAL for k in self.name_rescans):
            return    # 미뤄 둔 드라이브 다시 걷기만 남음: 때가 될 때까지 기다린다
        if len(self.name_paths) > MAX_PATHS:
            # 대량 변경: 경로마다 부모 폴더를 다시 읽기보다 그 고른 폴더를 한 번 다시 걷는다
            for path in self.name_paths.values():
                root = self._name_root_of(path)
                if root:
                    self.name_rescans[os.path.normcase(root)] = root
            self.name_paths.clear()
        now = self.clock()
        due, held = {}, {}
        for key, folder in self.name_rescans.items():
            # 이름 범위(드라이브 등) 전체 다시 걷기는 NAME_ROOT_RESCAN_INTERVAL에 한 번까지. 고른 폴더는 바로.
            wide = not any(scope.contains(r, folder) for r in self._resolved_roots)
            last = self._name_rescanned.get(key)
            if wide and last is not None and now - last < NAME_ROOT_RESCAN_INTERVAL:
                held[key] = folder
            else:
                due[key] = folder
                if wide:
                    self._name_rescanned[key] = now
        targets = list(self.name_paths.values())
        self.name_paths.clear()
        self.name_rescans = held
        if targets or due:
            self.names_changed.emit(targets, list(due.values()))

    def _name_root_of(self, path: str) -> str | None:
        owners = [r for r in self._resolved_roots + self._resolved_name_roots if scope.contains(r, path)]
        return min(owners, key=len) if owners else None

    # 색인 작업과의 조정

    def manual_started(self) -> None:
        """수동 색인이 전체를 다시 살피므로 그 전에 모은 변경은 버린다. 중지로 멈춘 자동 갱신도 다시 켠다."""
        self.batcher.clear()
        self.held = False
        self.idle.stop()
        self._update_state()

    def hold(self) -> None:
        """중지 버튼: 진행 중 작업을 멈추고, 다음 '색인 시작'까지 자동 갱신을 내보내지 않는다(변경은 계속 모은다)."""
        self.held = True
        self._update_state()

    def job_finished(self, summary: dict) -> None:
        auto = summary.get("auto", False)
        if auto:
            self.running_job = False
            self.idle.start()
        if summary.get("stopped"):
            self.held = True
        done = time.perf_counter()
        for path, result in summary.get("results", {}).items():
            key = os.path.normcase(path)
            if key in self.timeline:
                self.timeline[key]["indexed"] = done
            if result.startswith("retry later"):
                if self.enabled and self.allows(path):
                    self.batcher.retry(path)
            else:
                self.batcher.succeeded(path)
        self._update_state()

    def job_failed(self) -> None:
        self.running_job = False
        self._update_state()

    def _update_state(self) -> None:
        if not self.enabled:
            text = "자동 반영 꺼짐"
        elif not self.roots:
            text = "자동 반영: 이름 범위는 변경 기록(USN)으로 반영 중" if self.usn_roots                 else "자동 반영: 감시할 폴더가 없습니다"
        elif self.errors:
            folder = next(iter(self.errors))
            name = Path(folder).name or folder
            text = f"폴더를 열 수 없어 다시 열리기를 기다립니다: {name} (기존 색인은 그대로 둡니다)"
        elif self.held:
            text = "자동 반영 멈춤 — '색인 시작'을 누르면 놓친 변경까지 확인하고 다시 감시합니다"
        elif self.batcher.waiting_retries():
            text = f"사용 중인 파일 {self.batcher.waiting_retries()}개를 잠시 뒤 다시 읽습니다"
        elif self.running_job:
            text = "자동 반영: 바뀐 파일을 색인하는 중…"
        elif len(self.batcher):
            text = f"자동 반영: 변경 {len(self.batcher)}건 대기 중" + (" (색인이 끝나면 반영)" if self.busy() else "")
        elif self.batcher.given_up:
            text = f"감시 중 · 읽지 못한 파일 {len(self.batcher.given_up)}개는 다음 색인 때 다시 확인합니다"
        else:
            text = f"감시 중: 폴더 {len(self.roots)}개" + (f" · 이름 범위 {len(self.name_roots)}개" if self.name_roots else "")                 + (" · 이름은 변경 기록(USN)으로" if self.usn_roots else "")
        if text != self.state:
            self.state = text
            self.state_changed.emit(text)
