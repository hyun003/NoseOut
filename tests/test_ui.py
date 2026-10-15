"""5단계 UI: 화면 없는 Qt(offscreen)로 화면 연결을 시험한다. 앱 전체(실제 엔진) 시험은 pytest 임시 폴더의 색인만 쓴다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ctypes  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from ctypes import wintypes  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import requests  # noqa: E402
from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from conftest import requires_engine  # noqa: E402
from noseout import app as app_module, hotkey, opener, services  # noqa: E402
from noseout.indexer import IncompatibleIndex  # noqa: E402
from test_review_fixes import FakeEmbedder  # noqa: E402


@pytest.fixture(scope="module")
def qt():
    return QApplication.instance() or QApplication([])


def wait_for(condition, seconds: float = 30.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@dataclass
class FakeHit:
    path: str
    kind: str
    segment: str
    loc: float | None
    excerpt: str | None = None
    score: float = 1.0


# 쪽·시점 열기


AIDE = opener.Handler(r"C:\Program Files\Aside\Application\Aside.exe", "Aside",
                      r'"C:\Program Files\Aside\Application\Aside.exe" --single-argument %1')
POT = opener.Handler(r"C:\Program Files\DAUM\PotPlayer\PotPlayer64.exe", "팟플레이어", '"...PotPlayer64.exe" "%1"')


def test_plain_open_without_page_or_time(tmp_path):
    assert opener.plan(str(tmp_path / "a.pdf"), handler=AIDE) == opener.Plan(None, None)


@pytest.mark.parametrize("handler, expect", [
    (AIDE, lambda h, p: [h.exe, p.resolve().as_uri() + "#page=7"]),
    (opener.Handler(r"C:\x\msedge.exe", "Edge", ""), lambda h, p: [h.exe, p.resolve().as_uri() + "#page=7"]),
    (opener.Handler(r"C:\x\Acrobat.exe", "Acrobat", ""), lambda h, p: [h.exe, "/A", "page=7", str(p)]),
    (opener.Handler(r"C:\x\SumatraPDF.exe", "SumatraPDF", ""), lambda h, p: [h.exe, "-page", "7", str(p)]),
])
def test_pdf_page_commands(tmp_path, handler, expect):
    path = tmp_path / "책 1.pdf"
    chosen = opener.plan(str(path), page=7, handler=handler)
    assert chosen.argv == expect(handler, path) and chosen.note is None


@pytest.mark.parametrize("handler, argv", [
    (POT, lambda h, p: [h.exe, p, "/seek=00:01:25"]),
    (opener.Handler(r"C:\x\vlc.exe", "VLC", ""), lambda h, p: [h.exe, "--start-time=85", p]),
    (opener.Handler(r"C:\x\mpv.exe", "mpv", ""), lambda h, p: [h.exe, "--start=85", p]),
])
def test_media_time_commands(handler, argv):
    chosen = opener.plan(r"C:\m\talk.m4a", seconds=85.4, handler=handler)
    assert chosen.argv == argv(handler, r"C:\m\talk.m4a") and chosen.note is None


def test_unknown_handlers_fall_back_with_a_note():
    pdf = opener.plan(r"C:\d\a.pdf", page=3, handler=opener.Handler(r"C:\x\SomeViewer.exe", "어떤 뷰어", ""))
    assert pdf.argv is None and "3쪽" in pdf.note and "처음부터" in pdf.note
    media = opener.plan(r"C:\m\a.mp4", seconds=3725, handler=opener.Handler("", "영화 및 TV", ""))
    assert media.argv is None and "01:02:05" in media.note


def test_open_missing_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        opener.open_result(str(tmp_path / "gone.pdf"), page=2)
    with pytest.raises(FileNotFoundError):
        opener.reveal(str(tmp_path / "gone.pdf"))


def test_location_and_open_target():
    assert app_module.location(FakeHit("a.pdf", "pdf", "page", 12.0)) == "12쪽"
    assert app_module.location(FakeHit("a.pdf", "pdf", "page_image", 1.0)) == "1쪽"
    assert app_module.location(FakeHit("a.m4a", "audio", "audio", 85.0)) == "1:25"
    assert app_module.location(FakeHit("a.mp4", "video", "frame", 3725.0)) == "1:02:05"
    assert app_module.location(FakeHit("a.txt", "doc", "chunk", 3.0)) == "3번째 부분"
    assert app_module.location(FakeHit("a.txt", "doc", "chunk", 1.0)) == ""
    assert app_module.location(FakeHit("a.png", "image", "image", None)) == ""
    assert app_module.open_target(FakeHit("a.pdf", "pdf", "page", 12.0)) == (12, None)
    assert app_module.open_target(FakeHit("a.mp4", "video", "frame", 6.5)) == (None, 6.5)
    assert app_module.open_target(FakeHit("a.txt", "doc", "chunk", 3.0)) == (None, None)


# 전역 단축키


def test_hotkey_parse():
    assert hotkey.parse("Ctrl+Shift+Space") == (0x2 | 0x4, 0x20)
    assert hotkey.parse("alt+F2") == (0x1, 0x71)
    assert hotkey.parse("Win+Alt+K") == (0x8 | 0x1, ord("K"))
    for bad in ("Space", "Ctrl+Hyper+K", "Ctrl+PageMiddle"):
        with pytest.raises(ValueError):
            hotkey.parse(bad)


def test_hotkey_registration_failure_is_explained(qt):
    key = hotkey.GlobalHotkey(0, "Ctrl+Shift+Space", register=lambda *a: 0, unregister=lambda *a: 1)
    assert key.register() is False and not key.registered
    assert "Ctrl+Shift+Space" in key.error and "등록하지 못했습니다" in key.error


def test_hotkey_message_emits_pressed(qt):
    key = hotkey.GlobalHotkey(0, "Ctrl+Shift+Space", register=lambda *a: 1, unregister=lambda *a: 1)
    assert key.register()
    pressed = []
    key.pressed.connect(lambda: pressed.append(1))
    msg = wintypes.MSG()
    msg.message = hotkey.WM_HOTKEY
    msg.wParam = hotkey.HOTKEY_ID
    handled, _ = key.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
    assert handled and pressed == [1]
    msg.wParam = 999                                         # 다른 단축키는 건드리지 않는다
    assert key.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))[0] is False


def test_real_hotkey_conflict_is_detected(qt):
    """실제 Win32: 같은 조합을 두 번 등록하면 두 번째는 실패한다(다른 프로그램이 쓰는 경우와 같은 경로).
    offscreen Qt의 창 핸들은 진짜 HWND가 아니므로 스레드 단위 등록(hwnd 0)으로 시험한다. 창 핸들 등록은 앱 실행으로 확인."""
    first = hotkey.GlobalHotkey(0, "Ctrl+Alt+Shift+F12")
    second = hotkey.GlobalHotkey(0, "Ctrl+Alt+Shift+F12")
    try:
        assert first.register()
        assert second.register() is False and "다른 프로그램이 이미 사용 중" in second.error
    finally:
        first.unregister()


# 폴더 검사와 안내


def test_folder_checks_use_the_crawler_rules(tmp_path):
    assert services.check_folder("C:\\") == "드라이브 전체는 고를 수 없습니다. 검색할 폴더를 골라 주세요."
    assert "사용자 폴더 전체" in services.check_folder(str(Path.home()))
    assert "앱 데이터" in services.check_folder(str(Path.home() / "AppData"))
    assert "앱 데이터" in services.check_folder(os.environ["SystemRoot"])
    assert services.check_folder(str(tmp_path / "없음")) == "폴더가 없거나 열 수 없습니다."
    assert services.check_folder(str(tmp_path)) is None
    link = tmp_path / "link"
    if subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)], capture_output=True).returncode == 0:
        assert "링크" in services.check_folder(str(link))


def test_friendly_messages_hide_internals():
    messages = [
        services.friendly(IncompatibleIndex("index was made with other vectors (x)")),
        services.friendly(requests.ConnectionError("HTTPConnectionPool(host='127.0.0.1')")),
        services.friendly(FileNotFoundError(2, "No such file", r"C:\v\llama-server.exe")),
        services.friendly(RuntimeError("llama-server exited with 1")),
        services.friendly(KeyError("boom")),
    ]
    assert "기존 색인은 그대로" in messages[0]
    assert "연결이 끊겼습니다" in messages[1]
    assert "엔진 파일" in messages[2]
    assert "시작하지 못했습니다" in messages[3]
    assert "app.log" in messages[4]
    assert not any(word in m for m in messages for word in ("Traceback", "HTTPConnectionPool", "KeyError", "boom"))


# 검색 창


def test_search_window_shows_results_and_ignores_stale_ones(qt):
    window = app_module.SearchWindow()
    sent = []
    window.query.connect(lambda r, t: sent.append((r, t)))
    window.engine_is_ready("검색할 수 있는 파일 3개")
    window.box.setText("해변 사진")
    assert wait_for(lambda: sent, 2)
    request = sent[-1][0]
    hits = [FakeHit(r"C:\d\사진\beach.heic", "image", "image", None, "바다 «노을»"),
            FakeHit(r"C:\d\계약.pdf", "pdf", "page", 2.0, "반려동물 조항"),
            FakeHit(r"C:\d\talk.m4a", "audio", "audio", 25.0)]
    window.show_results(request - 1, "옛 검색", [hits[1]])         # 늦게 온 옛 결과는 무시
    assert window.table.topLevelItemCount() == 0
    window.show_results(request, "해변 사진", hits)
    # 결과 줄은 그룹("내용 · 3개") 아래에 있다. 이름 일치가 없으면 내용 그룹 하나뿐이다
    assert [window.table.topLevelItem(i).text(0) for i in range(window.table.topLevelItemCount())] == ["내용 · 3개"]
    rows = [[window.items[i].text(c) for c in range(4)] for i in range(3)]
    assert rows == [["beach.heic", "그림", "", r"C:\d\사진"], ["계약.pdf", "PDF", "2쪽", r"C:\d"],
                    ["talk.m4a", "음성", "0:25", r"C:\d"]]
    assert window.detail.text() == "바다 노을" and window.status.text() == "결과 3개"
    QApplication.sendEvent(window.box, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier))
    assert window.current_hit() is hits[1] and window.detail.text() == "반려동물 조항"
    window.show_results(request, "해변 사진", [])
    assert window.status.text() == "일치하는 파일 없음" and not window.open_button.isEnabled()


def test_search_waits_for_the_engine(qt):
    window = app_module.SearchWindow()
    sent = []
    window.query.connect(lambda r, t: sent.append((r, t)))
    window.box.setText("결제 오류")
    window._send()
    assert sent == [] and "준비" in window.status.text()
    window.engine_is_ready("준비됨")
    assert sent and sent[-1][1] == "결제 오류"                   # 준비되자마자 보낸다


def test_clearing_query_rejects_inflight_results(qt):
    window = app_module.SearchWindow()
    window.engine_is_ready("준비됨")
    window.box.setText("사진")
    window._send()
    request = window.request
    window.box.clear()
    window._send()
    window.show_results(request, "사진", [FakeHit("old.jpg", "image", "image", None)])
    assert not window.hits and window.table.topLevelItemCount() == 0
    window.show_name_results(request, "사진", [FakeHit("old.jpg", "image", "image", None)])
    assert not window.hits and window.table.topLevelItemCount() == 0


def test_search_bad_index_closes_its_engine(qt, tmp_path, monkeypatch):
    closed, failed = [], []

    class DummyEngine:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(services.engine, "Embedder", DummyEngine)
    db = tmp_path / "index.sqlite"
    db.write_bytes(b"broken sqlite")
    original = db.read_bytes()
    service = services.SearchService(db)
    service.failed.connect(lambda *a: failed.append(a))
    service.start()
    assert failed and closed and service.embedder is None
    assert db.read_bytes() == original


def test_window_close_hides_instead_of_quitting(qt):
    window = app_module.SearchWindow()
    window.summon()
    assert window.isVisible()
    window.close()
    assert not window.isVisible()


# 폴더 대화상자


def test_folders_dialog_rejects_forbidden_and_duplicates(qt, tmp_path, monkeypatch):
    warnings = []
    dialog = app_module.FoldersDialog([])
    # PySide6 네이티브 정적 메서드를 교체·복구하면 프로세스 종료 때 충돌한다. Python 경계만 교체한다.
    monkeypatch.setattr(dialog, "warn_folder", warnings.append)
    changed = []
    dialog.changed.connect(changed.append)
    assert dialog.add_folder("C:\\") is False and "드라이브 전체" in warnings[-1]
    assert dialog.add_folder(str(Path.home())) is False and "사용자 폴더 전체" in warnings[-1]
    assert dialog.add_folder(str(tmp_path)) is True and changed[-1] == [str(tmp_path)]
    assert dialog.add_folder(str(tmp_path).upper()) is False                      # 같은 폴더(대소문자만 다름)
    assert dialog.folders() == [str(tmp_path)]


def test_settings_roundtrip_and_bad_file(tmp_path):
    path = tmp_path / "settings.json"
    app_module.Settings(folders=[r"D:\사진"], hotkey="Alt+Space").save(path)
    assert app_module.Settings.load(path) == app_module.Settings([r"D:\사진"], "Alt+Space")
    path.write_text("{not json", encoding="utf-8")
    assert app_module.Settings.load(path) == app_module.Settings()                # 깨진 설정은 기본값으로


# 색인 서비스 (가짜 임베더)


def small_folder(tmp_path, test_data) -> Path:
    root = tmp_path / "chosen"
    root.mkdir()
    for name in ("tokyo_trip.md", "김치찌개_레시피.txt", "invoice_INV-4821.docx"):
        shutil.copy2(test_data / "documents" / name, root)
    shutil.copy2(test_data / "pictures" / "stop_sign.jpg", root)
    return root


def test_index_service_reports_progress_and_summary(qt, tmp_path, test_data):
    service = services.IndexService(tmp_path / "index.sqlite", embedder_factory=FakeEmbedder)
    progress, finished, failed = [], [], []
    service.progress.connect(lambda *a: progress.append(a))
    service.finished.connect(finished.append)
    service.failed.connect(failed.append)
    service.run([str(small_folder(tmp_path, test_data))])
    assert not failed and len(progress) == 4 and progress[-1][:2] == (4, 4)
    assert finished[0]["added"] == 4 and finished[0]["done"] == 4 and finished[0]["failed"] == 0
    assert not service.running


def test_index_service_stop_and_refusal(qt, tmp_path, test_data):
    folder = small_folder(tmp_path, test_data)
    service = services.IndexService(tmp_path / "index.sqlite", embedder_factory=FakeEmbedder)
    finished, failed = [], []
    service.finished.connect(finished.append)
    service.failed.connect(failed.append)
    service.progress.connect(lambda *a: service.request_stop())         # 첫 파일 뒤에 중지
    service.run([str(folder)])
    assert finished[-1]["stopped"] and finished[-1]["done"] == 1
    service.run([str(Path.home())])                                     # 금지 폴더는 색인하지 않는다
    assert failed and "사용자 폴더 전체" in failed[-1]


def test_index_stop_before_queued_run_is_preserved(qt, tmp_path, test_data):
    started, finished = [], []

    def factory():
        started.append(True)
        return FakeEmbedder()

    service = services.IndexService(tmp_path / "index.sqlite", embedder_factory=factory)
    service.finished.connect(finished.append)
    service.prepare()
    service.request_stop()
    service.run([str(small_folder(tmp_path, test_data))])
    assert not started and finished[-1]["stopped"] and finished[-1]["done"] == 0
    service.prepare()
    service.run([str(tmp_path / "chosen")])
    assert started and finished[-1]["done"] == 4 and not finished[-1]["stopped"]


def test_index_service_engine_failure_is_friendly(qt, tmp_path, test_data):
    service = services.IndexService(tmp_path / "index.sqlite",
                                    embedder_factory=lambda: FakeEmbedder(fail_texts=requests.ConnectionError("x")))
    failed = []
    service.failed.connect(failed.append)
    service.run([str(small_folder(tmp_path, test_data))])
    assert failed and "연결이 끊겼습니다" in failed[0]


def test_app_stop_button_reaches_busy_worker(qt, tmp_path, test_data, monkeypatch):
    """중지 신호가 실행 중인 QThread의 큐 뒤에서 기다리지 않아야 한다. 버튼부터 실제 연결 경로로 시험한다."""
    import threading

    entered, release = threading.Event(), threading.Event()

    class WaitingEmbedder(FakeEmbedder):
        def embed_texts(self, texts):
            entered.set()
            assert release.wait(5)
            return super().embed_texts(texts)

    monkeypatch.setattr(services.SearchService, "start", lambda self: self.ready.emit("준비됨"))
    application = app_module.App(qt, tmp_path / "home", index_factory=WaitingEmbedder, register_hotkey=False)
    summaries = []
    application.indexing.finished.connect(summaries.append)
    application.start()
    try:
        application.start_indexing([str(small_folder(tmp_path, test_data))])
        assert wait_for(entered.is_set, 3)
        application.folders.stop_button.click()
        assert application.indexing._stop.is_set()  # 작업이 끝나기 전에 화면 스레드에서 전달된다
        release.set()
        assert wait_for(lambda: summaries, 5)
        assert summaries[-1]["stopped"] and summaries[-1]["done"] < 4
        assert "색인을 중지했습니다" in application.folders.state.text()
    finally:
        release.set()
        application.quit()


# 앱 전체 (실제 엔진: 색인 → 검색 → 종료)


@requires_engine
def test_app_indexes_searches_and_quits_cleanly(qt, tmp_path, test_data):
    owned = []

    def index_embedder():
        embedder = services.engine.Embedder()
        owned.append(embedder.process)
        return embedder

    folder = small_folder(tmp_path, test_data)
    base = tmp_path / "home"
    application = app_module.App(qt, base, index_factory=index_embedder, register_hotkey=False)
    application.start()
    try:
        assert wait_for(lambda: application.window.engine_ready, 60), application.window.status.text()
        owned.append(application.search.embedder.process)
        assert "아직 색인이 없습니다" in application.window.status.text()
        assert application.folders.add_folder(str(folder))
        application.start_indexing(application.folders.folders())
        assert wait_for(lambda: "색인을 마쳤습니다" in application.folders.state.text(), 120), \
            application.folders.state.text()
        assert "새 파일 4" in application.folders.state.text()
        assert wait_for(lambda: "검색할 수 있는 파일 4개" in application.window.status.text(), 30)
        application.window.box.setText("a red stop sign")
        assert wait_for(lambda: application.window.hits, 30), application.window.status.text()
        assert Path(application.window.hits[0].path).name == "stop_sign.jpg"
        import json

        assert json.loads(application.settings_path.read_text("utf-8"))["folders"] == [str(folder)]
    finally:
        application.quit()
    assert not application.search_thread.isRunning() and not application.index_thread.isRunning()
    # 동시에 다른 앱·UI 시험이 서버를 띄워도 이 앱이 소유한 프로세스의 종료만 판정한다.
    assert len(owned) == 2 and wait_for(lambda: all(p.poll() is not None for p in owned), 10)


def test_app_import_order_keeps_ocr_safe(test_data):
    """새 프로세스에서 앱 모듈을 먼저 불러온 뒤 OCR(winrt)을 쓴다: 앱이 실제로 겪는 순서. winrt를 먼저 불러오면
    PySide6가 죽으므로(conftest 주석), app.py가 PySide6를 먼저 불러오는 순서가 깨지면 이 테스트가 실패한다."""
    import sys

    shot = test_data / "pictures" / "Screenshots" / "Screenshot 2026-10-01 093000.png"
    code = ("import noseout.app\nfrom noseout import ocr\n"
            f"print('OK' if 'Payment declined' in ocr.text_of_image(r'{shot}') else 'NO TEXT')")
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONPATH": str(root / "src"), "QT_QPA_PLATFORM": "offscreen"})
    assert done.returncode == 0 and done.stdout.strip() == "OK", done.stderr[-500:]
