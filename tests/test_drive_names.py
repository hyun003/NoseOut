"""드라이브 전체 이름 검색 + 선택 범위 AI 분석 (2026-10-14). 실제 드라이브는 걷지 않는다: pytest 임시 폴더(.tmp)의 합성 폴더를
드라이브 루트처럼 다룬다(rules.is_drive_root, 시스템 폴더 환경 변수, 사용자 폴더를 그 안으로 돌림)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QObject, QThread, Signal  # noqa: E402

from noseout import app, crawler, indexer, names, rules, services  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.names import NameIndex  # noqa: E402
from noseout.store import IndexStore  # noqa: E402
from test_names import touch  # noqa: E402
from test_names_app import ask, named_app  # noqa: E402,F401
from test_review_fixes import FakeEmbedder  # noqa: E402
from test_ui import qt, wait_for  # noqa: E402,F401
from test_watch import FakeWatcher, service  # noqa: E402


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """합성 '드라이브': 시스템 폴더·사용자 폴더가 그 안에 있는 것처럼 환경을 돌린다."""
    root = (tmp_path / "C").resolve()
    me = root / "Users" / "me"
    # SystemRoot 같은 환경 변수는 바꾸지 않는다(Winsock 등이 깨진다): 시스템 폴더 목록 함수와 사용자 폴더만 돌린다
    system = [root / "Windows", root / "Program Files", root / "Program Files (x86)", root / "ProgramData",
              me / "AppData" / "Local", me / "AppData" / "Roaming", me / "AppData"]
    for folder in system:
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rules, "_system_folders", lambda: [str(f) for f in system])
    monkeypatch.setenv("USERPROFILE", str(me))
    real = rules.is_drive_root
    monkeypatch.setattr(rules, "is_drive_root", lambda p: os.path.normcase(str(Path(p).resolve())) ==
                        os.path.normcase(str(root)) or real(p))
    assert Path.home() == me
    return root


def listed(index: NameIndex, base: Path) -> set[str]:
    return {Path(p).relative_to(base).as_posix() for (p,) in index.db.execute("SELECT path FROM entries")}


# 이름 검색과 AI 분석의 제외 정책


def test_drive_name_policy_and_ai_policy_stay_separate(tmp_path, drive):
    for name in ("Users/me/Documents/report.docx", "Users/me/Desktop/memo.txt", "Users/me/notes_at_home.txt",
                 "Users/me/Projects/app/package.json", "Users/me/Projects/app/main.py",
                 "Users/me/Projects/app/node_modules/x/index.js", "Users/me/Projects/.venv/lib.py",
                 "Users/me/AppData/Local/cache/blob.tmp", "Users/me/AppData/Roaming/app/settings.ini",
                 "Users/me/.ssh/id_rsa", "Users/me/Documents/server.pem", "Windows/System32/kernel.dll",
                 "Program Files/Tool/tool.exe", "ProgramData/Vendor/data.bin", "Recovery/WindowsRE/winre.wim",
                 "PerfLogs/log.etl", "Windows.old/x.txt", "Data/Recovery/my_recovery_plan.txt", "Data/photo.jpg"):
        touch(drive / name)
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([], [], name_roots=[drive])
        found = listed(index, drive)
    assert {"Users/me/Documents/report.docx", "Users/me/Desktop/memo.txt", "Users/me/notes_at_home.txt",
            "Users/me/Projects/app/package.json", "Users/me/Projects/app/main.py",
            "Data/Recovery/my_recovery_plan.txt", "Data/photo.jpg"} <= found       # 사용자 폴더 전체 포함, 깊은 Recovery는 일반 폴더
    assert not {p for p in found if p.startswith(("Windows", "Program Files", "ProgramData", "Recovery", "PerfLogs",
                                                  "Users/me/AppData", "Users/me/.ssh"))}
    assert not {p for p in found if "node_modules" in p or ".venv" in p or p.endswith((".pem", "id_rsa"))}
    # 고를 때: 이름 검색은 드라이브·사용자 폴더 전체를 허용, AI 분석은 그대로 금지
    assert rules.name_root_problem(drive) is None and rules.name_root_problem(drive / "Users" / "me") is None
    assert rules.name_root_problem(drive / "Windows") and rules.name_root_problem(drive / "Users/me/AppData/Local")
    assert services.check_folder(str(drive / "Users" / "me")) and services.check_folder(str(drive / "Windows"))
    assert services.check_folder(str(drive / "Users" / "me" / "Documents")) is None
    assert crawler.root_problem(drive / "Users" / "me")                           # 크롤러(AI)도 그대로 거부
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "jump"), str(drive)], capture_output=True)
    if made.returncode == 0:
        assert rules.name_root_problem(tmp_path / "jump")                          # 정션은 이름 범위로도 거부


def test_chosen_folders_are_listed_first_and_not_walked_twice(tmp_path, drive):
    for i in range(30):
        touch(drive / "Data" / f"d{i:02d}" / "file.txt")
    touch(drive / "Users/me/Documents/plan/2024.docx")
    docs = drive / "Users" / "me" / "Documents"
    seen = []
    with NameIndex(tmp_path / "names.sqlite") as index:
        report = index.scan([docs], [], name_roots=[drive], progress=lambda n, folder: seen.append(folder))
        assert all(scope_in(docs, f) for f in seen[:2]) and not scope_in(docs, seen[-1])
        folders = index.db.execute("SELECT COUNT(*) FROM entries WHERE is_dir = 1").fetchone()[0]
        assert report.folders == folders + 1            # 모든 폴더를 한 번씩 (+ 드라이브 루트), Documents를 두 번 걷지 않음
        # 상대 경로는 드라이브 기준이라 상위 폴더 이름으로도 찾는다
        assert [Path(h.path).name for h in index.search("documents 2024")] == ["2024.docx"]


def scope_in(folder: Path, path: str) -> bool:
    return os.path.normcase(path).startswith(os.path.normcase(str(folder)))


def test_unreadable_folders_on_a_drive_are_kept(tmp_path, drive, monkeypatch):
    touch(drive / "Users" / "other" / "other_plan.txt")
    touch(drive / "Data" / "a.txt")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([], [], name_roots=[drive])
        real = os.scandir

        def denied(path):
            if os.path.normcase(str(path)).endswith(os.path.normcase("\\Users\\other")):
                raise PermissionError(5, "Access is denied")
            return real(path)

        monkeypatch.setattr(names.os, "scandir", denied)
        report = index.scan([], [], name_roots=[drive])
        assert report.removed == 0 and [h.status for h in index.search("other_plan")] == ["unknown"]


# 이름 등록: 백그라운드, 일시정지·이어하기·중지


def big_tree(root: Path, folders: int = 400) -> int:
    for i in range(folders):
        touch(root / "Data" / f"f{i:03d}" / f"name_{i:03d}.txt")
    return folders * 2


class Sender(QObject):
    scan = Signal(list, list, list, list)


def test_name_scan_pauses_resumes_and_cancels_without_losing_names(qt, tmp_path, drive):
    expected = big_tree(drive)
    service = services.NameService(tmp_path / "names.sqlite")
    sender = Sender()
    sender.scan.connect(service.scan)
    thread = QThread()
    service.moveToThread(thread)
    thread.start()
    finished, paused = [], []
    service.finished.connect(lambda s: finished.append(s))
    service.paused_changed.connect(lambda p: paused.append(p))
    try:
        service.request_pause()                                     # 첫 폴더 경계에서 멈춘다
        sender.scan.emit([], [], [], [str(drive)])
        assert wait_for(lambda: service.paused, 10)
        reader = NameIndex(tmp_path / "names.sqlite", readonly=True)
        before = len(reader.search("name", limit=1000))
        time.sleep(0.5)
        assert len(reader.search("name", limit=1000)) == before < expected / 2   # 멈춰 있는 동안 늘지 않음, 검색은 됨
        service.resume()
        assert wait_for(lambda: finished, 30)
        assert not finished[-1]["stopped"] and len(reader.search("name_", limit=1000)) == expected / 2
        # 중지: 이미 등록한 이름은 남고, 다음 등록이 나머지를 맞춘다
        service.request_pause()
        sender.scan.emit([], [], [], [str(drive)])
        assert wait_for(lambda: service.paused, 10)
        service.request_cancel()
        assert wait_for(lambda: len(finished) == 2, 10) and finished[-1]["stopped"]
        assert len(reader.search("name_", limit=1000)) == expected / 2   # 중지는 삭제가 아니다
        assert True in paused and False in paused
        reader.close()
    finally:
        service.request_stop()
        thread.quit()
        thread.wait(5000)


# AI 분석 순서: 최근 파일·작은 문서 먼저, 큰 영상·음성 나중


def test_ai_reads_recent_small_documents_first_and_big_media_last(tmp_path):
    root = tmp_path / "chosen"
    old = time.time() - 400 * 86400
    files = {"big_movie.mp4": 30_000_000, "long_talk.mp3": 20_000_000, "old_note.txt": 2000, "new_note.txt": 2000,
             "photo.jpg": 300_000, "manual.pdf": 900_000}
    for name, size in files.items():
        touch(root / name)
        with open(root / name, "wb") as f:
            f.truncate(size)
        os.utime(root / name, (old, old) if name != "new_note.txt" else None)
    order, plans = [], []
    with IndexStore(tmp_path / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root], progress=lambda d, t, path, r: order.append(Path(path).name),
                                            on_plan=lambda n, s: plans.append((n, s)))
    assert order[0] == "new_note.txt" and order[1] == "old_note.txt"
    assert order[-2:] == ["long_talk.mp3", "big_movie.mp4"]
    assert plans and plans[0][0] == 6 and plans[0][1] > indexer.estimated_seconds("video", 30_000_000)
    assert indexer.priority("doc", 2000, time.time(), time.time()) < indexer.priority("doc", 2000, old, time.time())


# 이름 결과의 '내용 분석': 그 파일만


def test_single_file_analysis_takes_only_that_file(tmp_path, drive):
    docs = drive / "Data" / "inbox"
    for name, text in (("report.md", "Quarterly harbour report"), ("sibling.md", "Another note"),
                       ("setup.exe", ""), ("id_rsa", "key")):
        touch(docs / name).write_text(text, encoding="utf-8")
    touch(drive / "Users/me/AppData/Local/app/log.md").write_text("x", encoding="utf-8")
    touch(drive / "Users/me/Projects/node_modules/readme.md").write_text("x", encoding="utf-8")
    assert crawler.file_problem(docs / "report.md") is None
    assert crawler.file_problem(drive / "Users/me/notes.md") == "missing"
    touch(drive / "Users/me/notes.md").write_text("home note", encoding="utf-8")
    assert crawler.file_problem(drive / "Users/me/notes.md") is None              # 사용자가 직접 고른 파일 하나는 허용
    assert [crawler.file_problem(p) for p in (docs / "setup.exe", docs / "id_rsa", docs,
                                              drive / "Users/me/AppData/Local/app/log.md",
                                              drive / "Users/me/Projects/node_modules/readme.md")] == \
        ["unsupported", "secret", "folder", "system", "developer"]
    with IndexStore(tmp_path / "index.sqlite") as store:
        idx = Indexer(store, FakeEmbedder())
        report = idx.sync_files([docs / "report.md"])
        assert report.added == 1
        paths = [p for (p,) in store.db.execute("SELECT path FROM files")]
        assert paths == [str(docs / "report.md")]                                 # 형제 파일·부모 폴더는 아님
        other = tmp_path / "chosen"
        touch(other / "a.md").write_text("chosen folder", encoding="utf-8")
        idx.sync([other])                                                          # 폴더 색인이 고른 파일을 지우지 않는다
        assert store.file_at(str(docs / "report.md"))
        (docs / "report.md").unlink()
        assert idx.sync_files([docs / "report.md"]).removed == 1
    search = services.SearchService(tmp_path / "index.sqlite", roots=[str(other)], exclusions=[])
    search.store = IndexStore(tmp_path / "index.sqlite")
    search.set_extra([str(docs / "sibling.md")])
    assert len(search.allowed_files) == 1                                          # 고른 폴더의 a.md만 (지운 파일은 없음)
    search.store.close()


def test_analyze_button_adds_only_the_file_and_analyses_it(named_app, tmp_path, drive):
    inbox = drive / "Data" / "inbox"
    for name in ("harbour_report.md", "harbour_sibling.md", "harbour_tool.exe"):
        touch(inbox / name).write_text("Harbour lighthouse notes", encoding="utf-8")
    docs = drive / "Users" / "me" / "Documents"
    touch(docs / "plan.md").write_text("plan", encoding="utf-8")
    application, base = named_app(folders=[str(docs)])
    application._save_name_folders([str(drive)])
    assert sorted(ask(application, "harbour")) == ["harbour_report.md", "harbour_sibling.md", "harbour_tool.exe"]
    window = application.window
    hits = {Path(h.path).name: h for h in window.name_hits}
    assert window.analyzable(hits["harbour_report.md"]) and not window.analyzable(hits["harbour_tool.exe"])
    window.table.setCurrentItem(window.items[[Path(h.path).name for h in window.hits].index("harbour_report.md")])
    assert window.analyze_button.isEnabled()
    window.analyze_button.click()
    settings = app.Settings.load(base / "settings.json")
    assert settings.extra_files == [str(inbox / "harbour_report.md")] and settings.folders == [str(docs)]
    assert wait_for(lambda: (base / "index.sqlite").exists() and _paths(base / "index.sqlite"), 15)
    assert _paths(base / "index.sqlite") == [str(inbox / "harbour_report.md")]
    assert not window.analyzable(hits["harbour_report.md"]) and window.analyzable(hits["harbour_sibling.md"])
    application.analyze_file(str(inbox / "harbour_tool.exe"))
    assert "지원하지 않습니다" in window.status.text()
    assert app.Settings.load(base / "settings.json").extra_files == [str(inbox / "harbour_report.md")]


def _paths(db: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True) as con:
        return [p for (p,) in con.execute("SELECT path FROM files")]


# 감시: 이름 범위의 변경은 이름 목록에만, AI는 고른 폴더·고른 파일만. 드라이브 다시 걷기는 간격을 둔다


def test_watch_keeps_ai_to_chosen_scope_and_throttles_drive_rescans(qt, tmp_path, drive):
    docs = drive / "Users" / "me" / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    chosen_file = touch(drive / "Data" / "chosen.md")
    touch(drive / "Data" / "other.md")
    touch(docs / "a.md")
    svc, clock = service(tmp_path, [docs])
    svc.set_scope([str(docs)], [], name_roots=[str(drive)])
    svc.set_extra([str(chosen_file)])
    assert [w.root for w in FakeWatcher.made if not w.stopped] == [str(drive)]   # 핸들은 바깥 하나만
    names_sent, ai = [], []
    svc.names_changed.connect(lambda t, r: names_sent.append((t, r)))
    svc.dispatch.connect(lambda t, r: ai.append((t, r)))
    sink = FakeWatcher.made[-1].sink
    for path in (drive / "Data" / "other.md", chosen_file, docs / "a.md"):
        sink("added", str(drive), str(path), clock.now)
    svc.tick()
    clock.now += 1
    svc.tick()
    assert sorted(Path(p).name for p in names_sent[0][0]) == ["a.md", "chosen.md", "other.md"]
    assert ai and sorted(Path(p).name for p in ai[0][0]) == ["a.md", "chosen.md"]   # 이름 범위의 나머지는 AI로 가지 않음
    # 드라이브 감시가 넘침(overflow): 이름은 드라이브 다시 걷기, AI는 그 안의 고른 폴더만 다시 확인
    svc.job_finished({"auto": True, "results": {}})
    sink("rescan", str(drive), None, clock.now)
    clock.now += 1
    svc.tick()
    assert names_sent[-1][1] == [str(drive)]
    assert os.path.normcase(str(docs)) in svc.batcher.rescans or any(str(docs) in r for _, r in ai)
    count = len(names_sent)
    sink("rescan", str(drive), None, clock.now)
    clock.now += 1
    svc.tick()
    assert len(names_sent) == count                     # 10분 안의 두 번째 드라이브 다시 걷기는 미룬다
    clock.now += 601
    svc.tick()
    assert names_sent[-1][1] == [str(drive)]
    svc.shutdown()


# 첫 설정: 고르기 전에 폴더 안을 읽지 않는다


def test_first_run_setup_lists_folders_without_reading_them(qt, tmp_path, drive, monkeypatch):
    me = drive / "Users" / "me"
    known = {}
    for label, name in (("문서", "Documents"), ("바탕 화면", "Desktop"), ("다운로드", "Downloads"), ("사진", "Pictures")):
        (me / name).mkdir(parents=True, exist_ok=True)
        touch(me / name / "private.txt")
        known[app.KNOWN_FOLDERS[label]] = str(me / name)
    (me / "Projects").mkdir()
    monkeypatch.setattr(app, "known_folder", lambda guid: known.get(guid))
    opened = []
    real_scandir, real_listdir = os.scandir, os.listdir
    monkeypatch.setattr(os, "scandir", lambda p=".": opened.append(p) or real_scandir(p))
    monkeypatch.setattr(os, "listdir", lambda p=".": opened.append(p) or real_listdir(p))
    candidates = app.setup_candidates()
    assert [label for label, _ in candidates] == ["문서", "바탕 화면", "다운로드", "사진", "작업 폴더 (Projects)"]
    assert not any(str(me) in str(p) for p in opened)                       # 폴더 안을 열지 않았다
    monkeypatch.setattr(os, "scandir", real_scandir)
    monkeypatch.setattr(os, "listdir", real_listdir)
    monkeypatch.setattr(services.SearchService, "start", lambda self: None)
    base = tmp_path / "home"
    application = app.App(qt, base, index_factory=FakeEmbedder, register_hotkey=False)
    try:
        assert application.setup is not None
        application.setup.boxes[1][0].setChecked(False)                     # 바탕 화면은 빼고
        application.setup.start_box.setChecked(False)
        application.setup.accept()
        settings = app.Settings.load(base / "settings.json")
        assert settings.folders == [str(me / "Documents"), str(me / "Downloads"), str(me / "Pictures"),
                                    str(me / "Projects")]
        assert settings.name_folders == [os.environ.get("SystemDrive", "C:") + "\\"]
        assert not application.index_active                                # 분석 시작은 체크했을 때만
    finally:
        application.watch.shutdown()     # 실제 드라이브를 감시하지 않게 바로 닫는다 (start()를 부르지 않아 감시도 시작 전)
        application.quit()
    second = app.App(qt, base, index_factory=FakeEmbedder, register_hotkey=False)
    try:
        assert second.setup is None                                        # 설정이 있으면 다시 묻지 않는다
    finally:
        second.quit()


def test_name_registration_note_does_not_hide_engine_problems(named_app, tmp_path):
    root = tmp_path / "docs"
    touch(root / "a.txt")

    def failing(self):
        self.failed.emit(-1, "검색 엔진 파일(llama.cpp 또는 모델)을 찾지 못했습니다.")

    application, _ = named_app(start=failing, folders=[str(root)])
    assert wait_for(lambda: "등록됨" in application.window.status.text(), 10)
    assert "검색 엔진 파일" in application.window.status.text()
    application._names_finished({"added": 1, "files": 2, "folders": 0})
    assert application.window.status.text().count("등록됨") == 1
