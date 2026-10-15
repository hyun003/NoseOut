"""빠른 이름 검색의 화면·서비스·감시 연결. AI 엔진 없이(FakeEmbedder, 검색 엔진 시작은 가짜) 합성 파일과 pytest 임시
폴더(.tmp)의 별도 설정·색인만 쓴다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from noseout import app, opener, services  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.names import NameHit  # noqa: E402
from noseout.store import IndexStore  # noqa: E402
from test_review_fixes import FakeEmbedder  # noqa: E402
from test_ui import FakeHit, qt, small_folder, wait_for  # noqa: E402,F401
from test_watch import FakeWatcher, service  # noqa: E402


def name_hit(path: str, kind: str = "doc", tier: int = 2, status=None, is_dir=False) -> NameHit:
    return NameHit(path, Path(path).name, is_dir, kind, 0, 0.0, tier, status)


def rows(window) -> list[list[str]]:
    return [[item.text(c) for c in range(5)] for item in window.items]


def groups(window) -> list[str]:
    return [window.table.topLevelItem(i).text(0) for i in range(window.table.topLevelItemCount())]


# 결과 화면: 두 그룹, 중복, AI 순서·위치 유지


def test_two_groups_keep_ai_order_and_locations_and_merge_duplicates(qt):
    window = app.SearchWindow()
    window.engine_is_ready("준비됨")
    window.box.setText("계약")
    window._send()
    request = window.request
    content = [FakeHit(r"C:\d\계약.pdf", "pdf", "page", 7.0, "반려동물 조항"),
               FakeHit(r"C:\d\talk.m4a", "audio", "audio", 95.0),
               FakeHit(r"C:\d\notes.md", "doc", "chunk", 3.0)]
    names_found = [name_hit(r"C:\d\계약.pdf", "pdf", 1), name_hit(r"C:\d\계약서_사본.zip", "other"),
                   name_hit(r"C:\d\계약", "folder", 0, is_dir=True)]
    window.show_name_results(request, "계약", names_found)          # 이름 결과가 먼저 온다
    assert groups(window) == ["이름 일치 · 3개", "내용 · 찾는 중…"]
    assert "이름 일치 3개" in window.status.text() and "찾는 중" in window.status.text()
    QApplication.sendEvent(window.box, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down,
                                                Qt.KeyboardModifier.NoModifier))
    assert Path(window.current_hit().path).name == "계약서_사본.zip"
    window.show_results(request, "계약", content)
    # 같은 파일(계약.pdf)은 이름 칸에서 빠지고 내용 칸에 '이름·내용'. 내용 칸의 순서·위치는 AI 결과 그대로
    assert groups(window) == ["이름 일치 · 2개", "내용 · 3개"]
    assert rows(window) == [
        ["계약서_사본.zip", "파일 · ZIP", "", r"C:\d", "이름"],
        ["계약", "폴더", "", r"C:\d", "이름"],
        ["계약.pdf", "PDF", "7쪽", r"C:\d", "이름·내용"],
        ["talk.m4a", "음성", "1:35", r"C:\d", "내용"],
        ["notes.md", "문서", "3번째 부분", r"C:\d", "내용"]]
    assert window.hits[2:] == content
    assert Path(window.current_hit().path).name == "계약서_사본.zip"     # 고르던 줄을 유지
    assert window.status.text() == "이름 일치 2개 · 내용 결과 3개"
    # 늦게 온 옛 이름 결과는 무시한다
    window.show_name_results(request - 1, "옛", [name_hit(r"C:\d\old.txt")])
    assert len(window.hits) == 5
    window.close()


def test_many_name_hits_are_folded_until_asked(qt):
    window = app.SearchWindow()
    window.engine_is_ready("준비됨")
    window.box.setText("report")
    request = window.request
    many = [name_hit(rf"C:\d\report_{i:02d}.txt") for i in range(12)]
    window.show_name_results(request, "report", many)
    assert len(window.items) == 12                                  # 내용 결과가 없으면 다 보인다
    window.show_results(request, "report", [FakeHit(r"C:\d\annual.pdf", "pdf", "page", 2.0)])
    assert len(window.items) == app.NAME_PREVIEW + 1 and groups(window)[0] == "이름 일치 · 12개"
    for _ in range(app.NAME_PREVIEW):                               # ↓로 '더 보기' 줄까지 간다
        QApplication.sendEvent(window.box, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down,
                                                    Qt.KeyboardModifier.NoModifier))
    assert "더 보기" in window.table.currentItem().text(0)
    window.open_selected()
    assert len(window.items) == 13 and window.hits[-1].path == r"C:\d\annual.pdf"
    window.close()


def test_name_search_does_not_wait_for_or_depend_on_the_engine(qt):
    window = app.SearchWindow()
    names_sent, sent = [], []
    window.name_query.connect(lambda r, t: names_sent.append((r, t)))
    window.query.connect(lambda r, t: sent.append((r, t)))
    window.box.setText("청구서")
    window._send()
    assert names_sent and not sent and "준비" in window.status.text()   # 엔진 준비 전: 이름만
    window.show_name_results(window.request, "청구서", [name_hit(r"C:\d\청구서.pdf", "pdf")])
    assert groups(window) == ["이름 일치 · 1개", "내용 · 검색 엔진 준비 중"]
    window.show_error(-1, "검색 엔진을 시작하지 못했습니다.")              # 엔진 시작 실패
    assert groups(window) == ["이름 일치 · 1개", "내용 · 사용할 수 없음"] and len(window.hits) == 1
    assert "이름 일치 1개" in window.status.text() and "시작하지 못했습니다" in window.status.text()
    names_sent.clear()
    window.box.setText("청구")
    window._send()
    assert names_sent and not sent and "이름 일치만" in window.status.text()
    window.close()


def test_executables_folders_and_unreachable_hits_are_not_run(qt, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(app.opener, "open_result", lambda *a: calls.append(("open", a[0])))
    monkeypatch.setattr(app.opener, "reveal", lambda p: calls.append(("reveal", p)))
    monkeypatch.setattr(app.opener, "open_folder", lambda p: calls.append(("folder", p)))
    monkeypatch.setattr(app.opener, "open_code", lambda *a: calls.append(("code", a[0])))
    window = app.SearchWindow()
    window.engine_is_ready("준비됨")
    window.box.setText("x")
    hits = [name_hit(r"C:\d\setup.exe", "other"), name_hit(r"C:\d\run.bat", "code"),
            name_hit(r"C:\d\tool.py", "code"), name_hit(r"C:\d\바로 가기.lnk", "other"),
            name_hit(r"C:\d\사진", "folder", is_dir=True), name_hit(r"E:\usb\a.txt", "doc", status="unavailable"),
            name_hit(r"C:\d\memo.txt", "doc")]
    window.show_name_results(window.request, "x", hits)
    for i in range(len(hits)):
        window.table.setCurrentItem(window.items[i])
        window.open_selected()
    assert calls == [("reveal", r"C:\d\setup.exe"), ("reveal", r"C:\d\run.bat"), ("reveal", r"C:\d\tool.py"),
                     ("reveal", r"C:\d\바로 가기.lnk"), ("folder", r"C:\d\사진"), ("open", r"C:\d\memo.txt")]
    assert window.items[5].text(2) == "접근 불가"
    assert opener.is_executable("a.MSI") and opener.is_executable("b.ps1") and not opener.is_executable("c.pdf")
    window.close()


# 감시: 이름 변경은 AI 갱신 규칙과 따로


def test_watch_sends_name_changes_regardless_of_ai_state(qt, tmp_path):
    root = (tmp_path / "chosen").resolve()
    root.mkdir()
    (root / "a.zip").write_text("x", encoding="utf-8")
    svc, clock = service(tmp_path, [root], busy=lambda: True, index=tmp_path / "index.sqlite")   # 색인 없음, 바쁨
    svc.hold()
    sent, ai = [], []
    svc.names_changed.connect(lambda t, r: sent.append((t, r)))
    svc.dispatch.connect(lambda t, r: ai.append((t, r)))
    sink = FakeWatcher.made[0].sink
    for action, name in [("added", "a.zip"), ("modified", "b.md"), ("removed", "old.exe")]:
        sink(action, str(root), str(root / name), clock.now)
    svc.tick()
    assert not sent                                       # 잠깐 모은다
    clock.now += 0.2
    svc.tick()
    assert sent == [([str(root / "a.zip"), str(root / "old.exe")], [])] and not ai
    sink("rescan", str(root), None, clock.now)
    clock.now += 0.2
    svc.tick()
    assert sent[-1] == ([], [str(root)])
    svc.set_enabled(False)
    sink("added", str(root), str(root / "c.txt"), clock.now)
    clock.now += 0.2
    svc.tick()
    assert len(sent) == 2                                 # 자동 반영을 끄면 이름도 보내지 않는다
    svc.shutdown()


# 앱 전체: 폴더를 고르면 이름 등록, AI 분석은 '색인 시작'으로


class GatedEmbedder(FakeEmbedder):
    """색인이 오래 걸리는 상황: 테스트가 문을 열 때까지 임베딩을 붙잡는다.

    고정 대기(파일마다 0.4초)는 이름 검색보다 색인이 먼저 끝나면 '분석 중' 검사가 우연히 실패했다.
    문을 열지 않으면 30초 뒤 스스로 풀려 종료 정리가 멈추지 않는다."""

    def __init__(self, gate: threading.Event):
        super().__init__()
        self.gate = gate

    def embed_texts(self, texts):
        self.gate.wait(30)
        return super().embed_texts(texts)

    def embed_image(self, path, budget=280):
        self.gate.wait(30)
        return super().embed_image(path, budget)


@pytest.fixture
def named_app(qt, tmp_path, test_data, monkeypatch):
    made = []

    def make(start=lambda self: None, folders=None, factory=FakeEmbedder):
        # 검색 엔진 시작은 가짜: 기본은 '아직 로딩 중'(아무 신호도 보내지 않음)
        monkeypatch.setattr(services.SearchService, "start", start)
        base = tmp_path / "home"
        if folders is not None:
            app.Settings(folders).save(base / "settings.json")
        application = app.App(qt, base, index_factory=factory, register_hotkey=False)
        made.append(application)
        application.start()
        return application, base

    yield make
    for application in made:
        application.quit()


def ask(application, text: str, seconds: float = 10) -> list[str]:
    window = application.window
    window.box.setText(text)
    window._send()
    request = window.request
    wait_for(lambda: window.request == request and any(Path(h.path).name for h in window.name_hits), seconds)
    return [Path(h.path).name for h in window.name_hits]


def test_choosing_a_folder_registers_names_without_starting_ai(named_app, tmp_path, test_data):
    application, base = named_app(folders=[])
    root = small_folder(tmp_path, test_data)
    (root / "backup_2024.zip").write_text("x", encoding="utf-8")
    application._save_folders([str(root)])
    assert ask(application, "backup") == ["backup_2024.zip"]          # AI가 읽지 않는 종류도 이름으로
    assert ask(application, "김치") == ["김치찌개_레시피.txt"]
    assert not application.window.engine_ready                        # 검색 엔진은 아직 로딩 중
    assert not (base / "index.sqlite").exists() and not application.index_active   # AI 분석은 시작하지 않았다
    # 감시: 추가·이름 변경·삭제가 이름 목록에 바로 반영된다 (AI 색인은 아직 없다)
    (root / "새문서.hwp").write_text("x", encoding="utf-8")
    assert ask(application, "새문서") == ["새문서.hwp"]
    os.rename(root / "새문서.hwp", root / "고친문서.hwp")
    assert wait_for(lambda: ask(application, "고친문서", 1) == ["고친문서.hwp"], 10)
    assert ask(application, "새문서", 1) == []
    os.remove(root / "고친문서.hwp")
    assert wait_for(lambda: ask(application, "고친문서", 1) == [], 10)
    # 폴더를 빼면 그 이름 결과는 바로 사라진다
    application._save_folders([])
    assert ask(application, "김치", 2) == []


def test_name_search_works_while_ai_indexing_runs_and_with_a_failed_engine(named_app, tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    for i in range(6):
        shutil.copy2(test_data / "documents" / "tokyo_trip.md", root / f"trip_{i}.md")

    def failing(self):
        self.failed.emit(-1, "검색 엔진을 시작하지 못했습니다. GPU 메모리나 엔진 파일을 확인한 뒤 다시 시도하세요.")

    gate = threading.Event()
    application, base = named_app(start=failing, folders=[str(root)], factory=lambda: GatedEmbedder(gate))
    try:
        assert wait_for(lambda: application.window.engine_problem is not None, 5)
        assert wait_for(lambda: (base / "names.sqlite").exists(), 5)
        application.start_indexing([str(root)])
        assert wait_for(lambda: application.indexing.running, 5)
        latencies = []
        for query in ("trip_3", "invoice", "stop_sign"):
            started = time.perf_counter()
            found = ask(application, query)
            latencies.append(time.perf_counter() - started)
            assert found, query
        assert application.indexing.running                 # AI 분석이 도는 동안 (임베딩이 문에서 기다림)
        assert max(latencies) < 2.0, latencies              # 색인 스레드를 기다리지 않는다
        assert "시작하지 못했습니다" in application.window.status.text()
    finally:
        gate.set()                                          # 종료 정리 전에 색인 스레드를 풀어 준다


def test_names_work_with_an_index_from_another_engine(named_app, tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    base = tmp_path / "home"
    base.mkdir(parents=True, exist_ok=True)
    with IndexStore(base / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root])
        store.set_meta("engine", "some-other-model")
    before = (base / "index.sqlite").read_bytes()
    application, _ = named_app(start=lambda self: self.ready.emit("다른 모델·엔진으로 만든 색인"), folders=[str(root)])
    assert ask(application, "tokyo") == ["tokyo_trip.md"]
    application.start_indexing([str(root)])
    assert wait_for(lambda: not application.index_active, 10)
    assert "다른 모델" in application.folders.state.text()           # AI 색인은 거부, 이름은 그대로
    assert ask(application, "invoice") == ["invoice_INV-4821.docx"]
    assert (base / "index.sqlite").read_bytes()[:100] == before[:100]


def test_restart_uses_the_saved_list_and_marks_a_missing_folder(named_app, tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    application, base = named_app(folders=[str(root)])
    assert ask(application, "tokyo") == ["tokyo_trip.md"]
    application.quit()
    moved = tmp_path / "unplugged"
    os.rename(root, moved)                                    # 꺼져 있는 동안 폴더가 사라짐(드라이브 분리 등)
    application, _ = named_app(folders=[str(root)])
    assert ask(application, "tokyo") == ["tokyo_trip.md"]     # 지우지 않는다
    assert wait_for(lambda: ask(application, "tokyo", 1) and application.window.items
                    and application.window.items[0].text(2) == "접근 불가", 10)
    os.rename(moved, root)


# 분석 중 의미 검색: 커밋된 구간만, 화면 검색을 다시 보내지 않음


def test_search_refresh_sees_only_committed_files(qt, tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    db = tmp_path / "index.sqlite"
    with IndexStore(db) as store:
        Indexer(store, FakeEmbedder()).sync([root])
    search = services.SearchService(db, roots=[str(root)], exclusions=[])
    search.embedder = FakeEmbedder()
    search._open(announce=False)
    ready, refreshed = [], []
    search.ready.connect(ready.append)
    search.refreshed.connect(refreshed.append)
    before = len(search.searcher.row_file)
    (root / "new.md").write_text("A note about the harbour lighthouse.", encoding="utf-8")
    writer = IndexStore(db)
    indexer = Indexer(writer, FakeEmbedder())
    writer.db.execute("BEGIN")
    file_id = writer.upsert_file(str(root / "new.md"), "doc", 1, 1.0, 1, 1)
    indexer._index(file_id, str(root / "new.md"), "doc")     # 아직 커밋하지 않음
    search.refresh()
    assert len(search.searcher.row_file) == before
    writer.commit()
    search.refresh()
    assert len(search.searcher.row_file) == before + 1 and file_id in search.allowed_files
    assert refreshed and not ready and search.refresh_ms
    writer.close()
    search.embedder = None      # 가짜 엔진에는 close가 없다
    search.shutdown()


def test_rerendering_never_touches_deleted_rows(qt, monkeypatch):
    """결과를 다시 그릴 때(입력마다, 이름 등록 중 커밋마다) 격자의 선택 신호가 지운 목록 줄을 건드리지 않는다.
    (2026-10-14 측정 중 발견: 슬롯 예외는 앱에서 오류 창이 된다)"""
    import sys

    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    window = app.SearchWindow()
    window.engine_is_ready("준비됨")
    window.box.setText("a")
    window._send()
    hits = [name_hit(rf"C:\d\a{i}.txt") for i in range(5)]
    for view in (1, 0):
        window.set_expanded(True)
        window.view_mode.setCurrentIndex(view)
        for i in range(4):
            window.show_name_results(window.request, "a", hits[i:])
            window.grid.setCurrentRow(len(window.hits) - 1)
            window.show_results(window.request, "a", [FakeHit(r"C:\d\z.pdf", "pdf", "page", 1.0)])
    assert not errors, errors[0]
    window.close()


def test_adding_a_code_folder_registers_its_safe_names(named_app, tmp_path):
    repo = tmp_path / "shop"
    for name in ("package.json", "src/checkout_flow.py", "node_modules/left/pad.js", ".env.local"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x", encoding="utf-8")
    application, base = named_app(folders=[])
    application._save_code_folders([str(repo)])
    assert ask(application, "checkout") == ["checkout_flow.py"]
    assert ask(application, "package") == ["package.json"]
    assert ask(application, "pad", 1) == [] and ask(application, "env", 1) == []
    assert not (base / "code.sqlite").exists()       # 코드 색인(내용 분석)은 '색인 시작'으로만
