"""6단계 첫 항목: 폴더 감시와 증분 색인 연동. 합성 파일과 pytest 임시 폴더(.tmp)의 별도 설정·색인만 사용한다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ctypes  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from ctypes import wintypes  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402

from noseout import app, crawler, services, watcher  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.store import IndexStore  # noqa: E402
from test_review_fixes import FakeEmbedder  # noqa: E402
from test_ui import qt, small_folder, wait_for  # noqa: E402,F401


def states(store: IndexStore) -> dict[str, str]:
    return {Path(p).name: s for p, s in store.db.execute("SELECT path, state FROM files")}


# 바뀐 경로만 맞추기 (crawler.crawl_targets + Indexer.sync_paths)


def test_sync_paths_add_modify_delete_and_rename(tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    with IndexStore(tmp_path / "index.sqlite") as store:
        indexer = Indexer(store, FakeEmbedder())
        indexer.sync([root])
        (root / "new.md").write_text("A brand new note about lighthouses.", encoding="utf-8")
        report = indexer.sync_paths([root], [root / "new.md"])
        assert report.added == 1 and states(store)["new.md"] == "done"
        time.sleep(0.05)
        (root / "new.md").write_text("Changed note about lighthouses and harbours.", encoding="utf-8")
        report = indexer.sync_paths([root], [root / "new.md"])
        assert report.changed == 1 and report.added == 0
        os.rename(root / "new.md", root / "renamed.md")
        report = indexer.sync_paths([root], [root / "new.md", root / "renamed.md"])
        assert report.removed == 1 and report.added == 1
        assert "new.md" not in states(store) and states(store)["renamed.md"] == "done"
        os.remove(root / "renamed.md")
        assert indexer.sync_paths([root], [root / "renamed.md"]).removed == 1
        # 다른 파일은 건드리지 않는다
        assert sorted(states(store)) == sorted(p.name for p in root.iterdir())


def test_sync_paths_folder_rename_and_delete(tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    trip = root / "trip"
    trip.mkdir()
    shutil.copy2(test_data / "documents" / "tokyo_trip.md", trip / "a.md")
    with IndexStore(tmp_path / "index.sqlite") as store:
        indexer = Indexer(store, FakeEmbedder())
        indexer.sync([root])
        os.rename(trip, root / "travel")
        report = indexer.sync_paths([root], [trip, root / "travel", root / "travel" / "a.md"])
        assert report.removed == 1 and report.added == 1   # 상위 폴더가 대상이면 안의 파일은 한 번만 센다
        assert store.file_at(str(root / "travel" / "a.md")) and not store.file_at(str(trip / "a.md"))
        shutil.rmtree(root / "travel")
        assert indexer.sync_paths([root], [root / "travel"]).removed == 1


def test_unreadable_paths_and_missing_root_are_not_deletions(tmp_path, test_data, monkeypatch):
    root = small_folder(tmp_path, test_data)
    sub = root / "locked"
    sub.mkdir()
    shutil.copy2(test_data / "documents" / "tokyo_trip.md", sub / "a.md")
    with IndexStore(tmp_path / "index.sqlite") as store:
        indexer = Indexer(store, FakeEmbedder())
        indexer.sync([root])
        count = len(states(store))
        real = os.lstat

        def denied(path, *a, **k):
            if Path(path) == sub or Path(path) == sub / "a.md":
                raise PermissionError(5, "Access is denied")
            return real(path, *a, **k)

        monkeypatch.setattr(crawler.os, "lstat", denied)
        # 각각 따로: 상위 폴더 접근 거부, 대상 폴더 접근 거부
        for targets in ([sub / "a.md"], [sub]):
            report = indexer.sync_paths([root], targets)
            assert report.removed == 0 and len(states(store)) == count
        monkeypatch.setattr(crawler.os, "lstat", real)
        # 고른 폴더 자체가 없어져도(드라이브 분리 등) 지우지 않는다: 파일 이벤트, 폴더 전체 확인 각각
        moved = tmp_path / "moved-away"
        os.rename(root, moved)
        for targets in ([root / "tokyo_trip.md"], [root]):
            report = indexer.sync_paths([root], targets)
            assert report.removed == 0 and len(states(store)) == count


def test_targets_follow_the_same_rules_as_a_full_crawl(tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    (root / "private").mkdir()
    (root / "private" / "note.md").write_text("excluded note", encoding="utf-8")
    (root / "code").mkdir()
    (root / "code" / "package.json").write_text("{}", encoding="utf-8")
    (root / "code" / "readme.md").write_text("project readme", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.md").write_text("dependency", encoding="utf-8")
    (root / "id_rsa.txt").write_text("secret", encoding="utf-8")
    targets = [root / "private" / "note.md", root / "code" / "readme.md", root / "node_modules" / "x.md",
               root / "id_rsa.txt"]
    found, _ = crawler.crawl_targets([root], targets, exclusions=[root / "private"])
    full = crawler.crawl([root], exclusions=[root / "private"])
    assert found.files == [] and not any(Path(f.path).name in ("note.md", "readme.md", "x.md", "id_rsa.txt")
                                         for f in full.files)
    # 폴더 밖·다른 고른 폴더가 아닌 경로는 살피지 않는다
    found, covered = crawler.crawl_targets([root], [tmp_path / "elsewhere.md"])
    assert not found.files and not covered


def test_target_under_a_junction_is_not_followed(tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "far.md").write_text("outside the chosen folder", encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("can't create a junction here")
    found, _ = crawler.crawl_targets([root], [root / "link" / "far.md", root / "link"])
    assert found.files == []


# 이벤트 묶기 (가짜 시계)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_batcher_waits_for_quiet_and_stable_files():
    clock, sigs = Clock(), {"C:\\r\\a.txt": (1, 1, False)}
    batcher = watcher.ChangeBatcher(clock, sig=lambda p: sigs.get(p))
    batcher.add("C:\\r\\a.txt")
    clock.now += 0.1
    batcher.add("C:\\r\\a.txt")                 # 연속 이벤트는 하나로
    assert not batcher.due()
    clock.now += watcher.QUIET
    sigs["C:\\r\\a.txt"] = (5, 2, False)         # 아직 쓰는 중: 크기가 바뀌었다
    assert not batcher.due()
    clock.now += watcher.QUIET
    batch = batcher.due()
    assert batch.targets == ["C:\\r\\a.txt"] and len(batcher) == 0


def test_batcher_retries_locked_files_with_backoff_then_gives_up():
    clock = Clock()
    batcher = watcher.ChangeBatcher(clock, sig=lambda p: None)
    for delay in watcher.RETRY_DELAYS:
        assert batcher.retry("C:\\r\\locked.docx")
        clock.now += delay - 0.05
        assert not batcher.due()
        clock.now += 0.05
        assert batcher.due().targets == ["C:\\r\\locked.docx"]
    assert not batcher.retry("C:\\r\\locked.docx") and batcher.given_up


def test_batcher_rescan_absorbs_paths_and_collapses_floods(monkeypatch):
    clock = Clock()
    batcher = watcher.ChangeBatcher(clock, sig=lambda p: None)
    batcher.add("C:\\r\\sub\\a.txt")
    batcher.rescan("C:\\r")
    assert not batcher.entries
    clock.now += watcher.QUIET
    assert batcher.due().rescans == ["C:\\r"]
    monkeypatch.setattr(watcher, "MAX_PATHS", 3)
    for n in range(5):
        batcher.add(f"C:\\r\\{n}.txt")
    batcher.collapse(lambda p: "C:\\r")
    assert not batcher.entries and list(batcher.rescans) == ["c:\\r"]


def test_parse_notifications():
    def record(action, name, last=False):
        data = name.encode("utf-16-le")
        size = 12 + len(data)
        size += (-size) % 4
        head = (0 if last else size).to_bytes(4, "little") + action.to_bytes(4, "little") + len(data).to_bytes(4, "little")
        return (head + data).ljust(size, b"\0")

    buffer = record(4, "old.md") + record(5, "sub\\새 이름.md") + record(2, "gone.pdf", last=True)
    assert watcher.parse_notifications(buffer, len(buffer)) == [
        ("renamed_from", "old.md"), ("renamed_to", "sub\\새 이름.md"), ("removed", "gone.pdf")]


def test_real_watcher_events_root_loss_and_failing_folder(tmp_path, monkeypatch):
    root = tmp_path / "chosen"
    root.mkdir()
    events = []
    real = watcher.DirectoryWatcher(str(root), lambda *a: events.append(a[:3]))
    real.start()
    try:
        assert wait_for(lambda: real.available, 3)
        (root / "a.md").write_text("x", encoding="utf-8")
        assert wait_for(lambda: ("added", str(root), str(root / "a.md")) in events, 3)
        shutil.rmtree(root)                               # 고른 폴더가 사라짐: 오류 한 번, 다시 생기면 전체 확인
        assert wait_for(lambda: [e[0] for e in events].count("error") == 1, 5)
        root.mkdir()
        assert wait_for(lambda: events[-2:] == [("ok", str(root), None), ("rescan", str(root), None)], 10)
    finally:
        real.stop()
    assert not real.alive

    # 변경 알림을 거부하는 폴더(일부 네트워크 드라이브 등): 다시 열어 보되 전체 다시 확인을 반복하지 않는다
    class Refusing:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def ReadDirectoryChangesW(self, *args):  # noqa: N802
            ctypes.set_last_error(1)                  # ERROR_INVALID_FUNCTION
            return 0

    original = watcher._kernel32
    monkeypatch.setattr(watcher, "_kernel32", lambda: Refusing(original()))
    monkeypatch.setattr(watcher, "REOPEN_SECONDS", 0.02)
    events.clear()
    refusing = watcher.DirectoryWatcher(str(root), lambda *a: events.append(a[:3]))
    refusing.start()
    time.sleep(1.0)
    refusing.stop()
    assert [e[0] for e in events] == ["error"]


def test_watching_blocks_parent_rename_until_watching_stops(tmp_path):
    """설정 화면 안내의 근거: 감시 중에는 상위 폴더 이름 변경이 거부되고, 감시를 멈추면 된다."""
    root = tmp_path / "outer" / "chosen"
    root.mkdir(parents=True)
    real = watcher.DirectoryWatcher(str(root), lambda *a: None)
    real.start()
    try:
        assert wait_for(lambda: real.available, 3)
        with pytest.raises(PermissionError):
            os.rename(tmp_path / "outer", tmp_path / "moved")
    finally:
        real.stop()
    os.rename(tmp_path / "outer", tmp_path / "moved")
    assert (tmp_path / "moved" / "chosen").is_dir()


# WatchService (가짜 감시자)


class FakeWatcher:
    made: list = []

    def __init__(self, root, sink):
        self.root, self.sink, self.stopped = root, sink, False
        FakeWatcher.made.append(self)

    def start(self):
        pass

    def stop(self, timeout=5.0):
        self.stopped = True

    _stop = threading.Event()


def service(tmp_path, roots, exclusions=(), busy=lambda: False, index=None, ignore=()):
    FakeWatcher.made = []
    clock = Clock()
    svc = watcher.WatchService([str(r) for r in roots], list(exclusions), ignore=list(ignore), busy=busy,
                               watcher_factory=FakeWatcher, clock=clock, index=index)
    svc.batcher.clock = clock
    svc._sync_watchers()
    return svc, clock


def test_watch_service_filters_noise_and_own_files(qt, tmp_path):
    root = (tmp_path / "chosen").resolve()
    (root / "own").mkdir(parents=True)
    (root / "sub").mkdir()
    (root / "copied").mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    (root / "app.log").write_text("x", encoding="utf-8")
    (root / "own" / "index.sqlite-wal").write_text("x", encoding="utf-8")
    with IndexStore(tmp_path / "index.sqlite") as store:
        store.upsert_file(str(root / "known.md"), "doc", 1, 1, 1, 1)
        store.commit()
    svc, clock = service(tmp_path, [root], ignore=[root / "own"], index=tmp_path / "index.sqlite")
    sink = FakeWatcher.made[0].sink
    for action, name in [("modified", "a.md"), ("modified", "app.log"), ("modified", "own\\index.sqlite-wal"),
                         ("modified", "sub"), ("removed", "temp~1.tmp"), ("removed", "known.md"),
                         ("added", "copied"), ("modified", "package.json")]:
        sink(action, str(root), str(root / name), clock.now)
    svc.tick()
    # 폴더 자체의 '수정'(sub)은 버리고, 새로 들어온 폴더(copied)는 통째로 살핀다
    assert sorted(Path(p).name for p in svc.batcher.paths.values()) == ["a.md", "chosen", "copied", "known.md"]
    svc.shutdown()


def test_watch_service_defers_while_busy_held_or_disabled(qt, tmp_path):
    root = (tmp_path / "chosen").resolve()
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    busy = [True]
    svc, clock = service(tmp_path, [root], busy=lambda: busy[0])
    sent = []
    svc.dispatch.connect(lambda t, r: sent.append((t, r)))
    FakeWatcher.made[0].sink("added", str(root), str(root / "a.md"), clock.now)
    svc.tick()
    clock.now += 1
    svc.tick()
    assert not sent and "색인이 끝나면" in svc.state       # 수동 색인·일시정지 중: 모아 두기만
    busy[0] = False
    svc.hold()
    svc.tick()
    assert not sent and "멈춤" in svc.state              # 중지 뒤: 다음 '색인 시작'까지
    svc.held = False
    svc.tick()
    assert sent == [([str(root / "a.md")], [])] and svc.running_job
    svc.tick()
    assert len(sent) == 1                                 # 작업이 끝나기 전에는 다음 작업을 보내지 않는다
    svc.job_finished({"auto": True, "results": {str(root / "a.md"): "retry later: locked"}})
    assert svc.batcher.waiting_retries() == 1 and "다시 읽습니다" in svc.state
    svc.manual_started()                                  # 수동 색인이 전체를 다시 본다
    assert len(svc.batcher) == 0
    svc.set_enabled(False)
    assert FakeWatcher.made[0].stopped and svc.state == "자동 반영 꺼짐"
    svc.shutdown()


def test_no_index_means_no_automatic_jobs_and_noop_keeps_status(qt, tmp_path):
    root = (tmp_path / "chosen").resolve()
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    svc, clock = service(tmp_path, [root], index=tmp_path / "index.sqlite")
    sent = []
    svc.dispatch.connect(lambda t, r: sent.append((t, r)))
    svc.reconcile()                                       # 앱 시작: 색인을 만든 적이 없으면 맞출 것이 없다
    FakeWatcher.made[0].sink("added", str(root), str(root / "a.md"), clock.now)
    clock.now += 1
    svc.tick()
    assert not sent and len(svc.batcher) == 0
    svc.shutdown()
    dialog = app.FoldersDialog([str(root)])
    dialog.state.setText("색인을 마쳤습니다.")
    dialog.indexing_started(auto=True)
    dialog.indexing_finished(services.IndexService._summary(auto=True))
    assert dialog.state.text() == "색인을 마쳤습니다." and dialog.start_button.isEnabled()
    # 부모 없는 대화상자가 Python 종료 단계까지 남으면 QApplication보다 늦게 정리되며 프로세스가 간헐적으로 죽는다
    # (0xC0000005/0xC0000409, docs/progress.md). 테스트 안에서 지운다.
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_watch_service_follows_scope_changes(qt, tmp_path):
    a, b = (tmp_path / "a").resolve(), (tmp_path / "b").resolve()
    for folder in (a, b, a / "private"):
        folder.mkdir()
    svc, clock = service(tmp_path, [a], exclusions=[str(a / "private")])
    FakeWatcher.made[0].sink("added", str(a), str(a / "private" / "x.md"), clock.now)
    svc.tick()
    assert len(svc.batcher) == 0                          # 제외 폴더의 이벤트는 버린다
    svc.set_scope([str(a), str(b)], [])                   # 폴더 추가, 제외 해제
    assert {os.path.normcase(str(b)), os.path.normcase(str(a / "private"))} == set(svc.batcher.rescans)
    assert len(svc.watchers) == 2
    first = FakeWatcher.made[0]
    svc.set_scope([str(b)], [])                           # 폴더 제거: 감시 중지, 그 폴더의 대기 변경도 버린다
    assert first.stopped and set(svc.batcher.rescans) == {os.path.normcase(str(b))}
    svc.shutdown()


# IndexService.run_changes


def test_run_changes_skips_never_indexed_folders_and_missing_index(qt, tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    db = tmp_path / "index.sqlite"
    service_ = services.IndexService(db, embedder_factory=FakeEmbedder)
    finished = []
    service_.finished.connect(finished.append)
    service_.run_changes([str(root)], [str(root / "tokyo_trip.md")], [str(root)])
    assert not db.exists() and finished[-1]["auto"]       # 색인이 없으면 만들지 않는다
    with IndexStore(db):
        pass
    service_.run_changes([str(root)], [], [str(root)])    # 처음 색인은 사용자가 시작한다
    assert finished[-1]["done"] == 0
    service_.run([str(root)])
    (root / "late.md").write_text("Notes written while the app was closed.", encoding="utf-8")
    service_.run_changes([str(root)], [], [str(root)])
    assert finished[-1]["added"] == 1 and finished[-1]["done"] == 1
    assert service_._embedder is not None                 # 다음 변경을 빨리 읽게 엔진을 남겨 둔다
    service_.release_engine()
    assert service_._embedder is None
    shutil.rmtree(root)
    service_.run_changes([str(root)], [str(root / "late.md")], [str(root)])
    assert finished[-1]["removed"] == 0 and finished[-1]["unavailable"] == [str(root)]


# 앱 전체 (실제 감시 스레드, 가짜 엔진)


def lock_file(path: Path):
    """다른 프로그램이 저장 중인 파일처럼 공유 없이 연다(읽기도 막힘)."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE]
    handle = k.CreateFileW(str(path), 0x40000000, 0, None, 2, 0x80, None)   # GENERIC_WRITE, 공유 없음, CREATE_ALWAYS
    assert handle not in (None, wintypes.HANDLE(-1).value)
    data = b"Quarterly lighthouse maintenance schedule and keeper rota."
    k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                            wintypes.LPVOID]
    k.WriteFile(handle, data, len(data), ctypes.byref(wintypes.DWORD()), None)
    return lambda: k.CloseHandle(handle)


def db_state(db: Path, name: str) -> str | None:
    import sqlite3

    with sqlite3.connect(db) as con:
        row = con.execute("SELECT state FROM files WHERE path LIKE ?", (f"%\\{name}",)).fetchone()
    return row[0] if row else None


@pytest.fixture
def watched_app(qt, tmp_path, test_data, monkeypatch):
    monkeypatch.setattr(services.SearchService, "start", lambda self: self.ready.emit("준비됨"))
    root = small_folder(tmp_path, test_data)
    base = tmp_path / "home"
    app.Settings([str(root)]).save(base / "settings.json")
    made = []

    def make():
        application = app.App(qt, base, index_factory=FakeEmbedder, register_hotkey=False)
        made.append(application)
        application.start()
        return application

    yield make, root, base
    for application in made:
        application.quit()


def test_app_reflects_changes_automatically(watched_app):
    make, root, base = watched_app
    application = make()
    application.start_indexing([str(root)])
    assert wait_for(lambda: "색인을 마쳤습니다" in application.folders.state.text(), 10)
    db = base / "index.sqlite"
    (root / "harbour.md").write_text("Notes about the old harbour lighthouse.", encoding="utf-8")
    assert wait_for(lambda: db_state(db, "harbour.md") == "done", 10), application.folders.state.text()
    assert wait_for(lambda: "자동 반영 완료" in application.folders.state.text(), 5)
    os.rename(root / "harbour.md", root / "port.md")
    assert wait_for(lambda: db_state(db, "port.md") == "done" and db_state(db, "harbour.md") is None, 10)
    os.remove(root / "port.md")
    assert wait_for(lambda: db_state(db, "port.md") is None, 10)
    # 잠긴 파일(저장 중)은 실패로 남기지 않고 잠시 뒤 다시 읽는다
    release = lock_file(root / "locked.txt")
    try:
        assert wait_for(lambda: application.watch.batcher.waiting_retries() == 1, 10), application.watch.state
        assert db_state(db, "locked.txt") == "pending"
    finally:
        release()
    assert wait_for(lambda: db_state(db, "locked.txt") == "done", 15), application.watch.state
    assert wait_for(lambda: application.watch.state.startswith("감시 중"), 5), application.watch.state
    # 자기 설정 폴더(색인·로그)의 쓰기로는 작업이 생기지 않는다: 감시 대상 밖이다
    assert not application.watch.running_job and len(application.watch.batcher) == 0


def test_app_stop_pause_and_manual_coordination(watched_app, monkeypatch):
    make, root, base = watched_app
    application = make()
    application.start_indexing([str(root)])
    assert wait_for(lambda: "색인을 마쳤습니다" in application.folders.state.text(), 10)
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeEmbedder):
        def embed_texts(self, texts):
            entered.set()
            assert release.wait(10)
            return super().embed_texts(texts)

    application.indexing._factory = Slow
    application.release_engine.emit()
    for n in range(3):
        (root / f"note{n}.md").write_text(f"Note number {n} about tides.", encoding="utf-8")
    assert wait_for(entered.is_set, 10)
    assert application.folders.auto and application.folders.pause_button.isEnabled()
    assert not application.folders.start_button.isEnabled()      # 자동 갱신 중에는 수동 색인과 겹치지 않는다
    sent = []
    application.watch.dispatch.connect(lambda *a: sent.append(a))
    application.folders.pause_button.click()
    release.set()
    assert wait_for(lambda: application.indexing.paused, 5)
    (root / "during_pause.md").write_text("Written while paused.", encoding="utf-8")
    assert wait_for(lambda: len(application.watch.batcher) == 1, 5)
    time.sleep(watcher.QUIET + 0.3)
    application.qt.processEvents()
    assert not sent and application.indexing.paused                # 일시정지 중에는 새 작업을 보내지 않는다
    application.folders.stop_button.click()                      # 일시정지 중 중지
    assert wait_for(lambda: not application.index_active, 5)
    assert application.watch.held and "자동 반영을 중지했습니다" in application.folders.state.text()
    time.sleep(watcher.QUIET + 0.3)
    application.qt.processEvents()
    assert not application.index_active                          # 중지 뒤에는 자동으로 다시 시작하지 않는다
    application.indexing._factory = FakeEmbedder
    application.start_indexing([str(root)])                       # '색인 시작'이 남은 변경까지 확인하고 감시를 다시 켠다
    assert wait_for(lambda: "색인을 마쳤습니다" in application.folders.state.text(), 10)
    assert not application.watch.held
    db = base / "index.sqlite"
    assert all(db_state(db, f"note{n}.md") == "done" for n in range(3)) and db_state(db, "during_pause.md") == "done"


def test_app_scope_changes_and_restart_catch_missed_changes(watched_app):
    make, root, base = watched_app
    application = make()
    (root / "private").mkdir()
    (root / "private" / "kept.md").write_text("Private notes on the lighthouse budget.", encoding="utf-8")
    application.start_indexing([str(root)])
    assert wait_for(lambda: "색인을 마쳤습니다" in application.folders.state.text(), 10)
    db = base / "index.sqlite"
    assert application.folders.add_exclusion(str(root / "private"))
    (root / "private" / "while_excluded.md").write_text("Added while excluded.", encoding="utf-8")
    time.sleep(watcher.QUIET + 0.5)
    application.qt.processEvents()
    assert db_state(db, "while_excluded.md") is None              # 제외 폴더는 감시에 반영하지 않는다
    application.folders.excluded_list.setCurrentRow(0)
    application.folders.remove_exclusion()                        # 제외 해제: 놓친 변경을 확인한다
    assert wait_for(lambda: db_state(db, "while_excluded.md") == "done", 10)
    application.quit()
    assert all(not w.alive for w in application.watch.watchers.values()) and not application.watch.watchers
    # 앱이 꺼져 있던 동안의 변경: 다시 켜면 확인한다
    (root / "offline.md").write_text("Written while NoseOut was closed.", encoding="utf-8")
    os.remove(root / "tokyo_trip.md")
    second = make()
    assert wait_for(lambda: db_state(db, "offline.md") == "done" and db_state(db, "tokyo_trip.md") is None, 10)
    assert json.loads((base / "settings.json").read_text("utf-8"))["watch"] is True
    assert "상위 폴더를 옮기거나 이름을 바꿀 수 없습니다" in second.folders.watch_note.text()
    assert "자동 반영을 잠시 끄세요" in second.folders.watch_note.text()
    second.folders.watch_box.setChecked(False)                    # 끄면 감시 스레드를 멈추고 설정에 남긴다
    assert not second.watch.watchers and json.loads((base / "settings.json").read_text("utf-8"))["watch"] is False
    (root / "while_off.md").write_text("Written while watching was off.", encoding="utf-8")
    time.sleep(watcher.QUIET + 0.3)
    second.qt.processEvents()
    assert db_state(db, "while_off.md") is None
    second.folders.watch_box.setChecked(True)                     # 다시 켜면 꺼져 있던 동안의 변경을 확인한다
    assert wait_for(lambda: db_state(db, "while_off.md") == "done", 10)
