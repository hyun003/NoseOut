"""설정창(2026-10-14): 설정 저장·복원, 적용·취소·기본값, 저장 실패 보존, 단축키 충돌 복구, 로그인 시 실행(대체 구현),
테마·보기·창 위치, 색인 현황·작업 상태·결과 없음 안내. 실제 Windows 시작 항목·전역 단축키·사용자 설정은 쓰지 않는다
(conftest의 no_real_startup_entries, 가짜 RegisterHotKey, pytest 임시 폴더)."""

import ctypes
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from noseout import app, hotkey, prefs, services, stats  # noqa: E402
from noseout.store import IndexStore  # noqa: E402
from test_review_fixes import FakeEmbedder  # noqa: E402

EXE = r"D:\Apps\NoseOut\NoseOut.exe"


@pytest.fixture(scope="module")
def qt():
    return QApplication.instance() or QApplication([])


def wait_for(condition, seconds: float = 10.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class FakeKeys:
    """RegisterHotKey 대체: taken에 든 (수식키, 키)는 '다른 프로그램이 사용 중'으로 거부한다."""

    def __init__(self, taken=()):
        self.taken = {hotkey.parse(t) for t in taken}
        self.active: dict[int, tuple[int, int]] = {}

    def register(self, _hwnd, ident, modifiers, vk):
        combo = (modifiers & ~hotkey.MOD_NOREPEAT, vk)
        if combo in self.taken or combo in self.active.values():
            ctypes.set_last_error(1409)          # ERROR_HOTKEY_ALREADY_REGISTERED
            return False
        self.active[ident] = combo
        return True

    def unregister(self, _hwnd, ident):
        self.active.pop(ident, None)
        return True

    def combos(self) -> set:
        return set(self.active.values())


@pytest.fixture
def make_app(qt, tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(services.SearchService, "start", lambda self: None)

    def make(settings: app.Settings | None = None, keys: FakeKeys | None = None, run=None, exe: str | None = EXE,
             start: bool = False):
        base = tmp_path / "home"
        if settings is not None:
            settings.save(base / "settings.json")
        keys = keys or FakeKeys()
        startup = prefs.Startup(run if run is not None else prefs.MemoryRun(), exe=exe or "")
        application = app.App(qt, base, index_factory=FakeEmbedder, register_hotkey=False,
                              hotkey_backend=(keys.register, keys.unregister), startup=startup, setup=False)
        application.keys = keys
        made.append(application)
        if start:
            application.start()
        return application, base

    yield make
    for application in made:
        application.quit()


def saved(base: Path) -> dict:
    return json.loads((base / "settings.json").read_text(encoding="utf-8"))


# 설정 파일: 기존 설정 호환, 안전 저장


def test_old_settings_load_with_safe_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"folders": ["D:\\docs"], "hotkey": "Ctrl+Alt+K", "exclusions": [], "watch": False,
                                "code_folders": [], "code_editor": ""}), encoding="utf-8")
    settings = app.Settings.load(path)
    assert settings.folders == ["D:\\docs"] and settings.hotkey == "Ctrl+Alt+K" and settings.watch is False
    assert (settings.launch_at_login, settings.start_in_tray, settings.theme, settings.large_view) == \
        (False, False, "system", "grid")
    assert settings.window_small == [] and settings.name_folders == [] and settings.extra_files == []
    # 이상한 값은 기본값으로 (켜짐으로 잘못 읽지 않는다)
    path.write_text(json.dumps({"launch_at_login": "yes", "theme": "neon", "large_view": 3,
                                "window_small": [1, 2, -5, 4], "window_large": [1, 2, 3]}), encoding="utf-8")
    settings = app.Settings.load(path)
    assert (settings.launch_at_login, settings.theme, settings.large_view) == (False, "system", "grid")
    assert settings.window_small == [] and settings.window_large == []


def test_save_failure_keeps_the_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    app.Settings(["D:\\docs"]).save(path)
    before = path.read_bytes()

    def broken(_src, _dst):
        raise OSError(28, "디스크 공간 부족")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        app.Settings(["D:\\other"], theme="dark").save(path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]      # 임시 파일을 남기지 않는다


# 단축키


@pytest.mark.parametrize("text, ok", [("Ctrl+Shift+Space", True), ("Ctrl+Alt+K", True), ("Win+F5", True),
                                      ("Shift+A", False), ("Alt+F4", False), ("Alt+Tab", False), ("K", False),
                                      ("Ctrl+Shift+PgUp", False), ("", False)])
def test_hotkey_problems(text, ok):
    assert (prefs.hotkey_problem(text) is None) == ok


def test_key_sequence_text():
    assert prefs.hotkey_from_sequence("Ctrl+Shift+Space") == "Ctrl+Shift+Space"
    assert prefs.hotkey_from_sequence("Meta+K") == "Win+K"
    assert prefs.hotkey_from_sequence("Shift+Ctrl+Return") == "Ctrl+Shift+Enter"
    assert prefs.normalize_hotkey("ctrl + shift + space") == "Ctrl+Shift+Space"


def test_hotkey_change_keeps_the_old_one_on_conflict(qt):
    keys = FakeKeys(taken=["Ctrl+Alt+K"])
    key = hotkey.GlobalHotkey(1, "Ctrl+Shift+Space", keys.register, keys.unregister)
    assert key.register() and keys.combos() == {hotkey.parse("Ctrl+Shift+Space")}
    assert not key.change("Ctrl+Alt+K")                                  # 다른 프로그램이 사용 중
    assert "다른 프로그램" in key.error
    assert key.registered and key.text == "Ctrl+Shift+Space" and keys.combos() == {hotkey.parse("Ctrl+Shift+Space")}
    assert key.change("Ctrl+Alt+J")                                      # 새 것을 먼저 등록하고 옛 것을 푼다
    assert keys.combos() == {hotkey.parse("Ctrl+Alt+J")} and key.text == "Ctrl+Alt+J"
    assert key.change("Ctrl+Shift+Space") and keys.combos() == {hotkey.parse("Ctrl+Shift+Space")}


# 로그인 시 실행 (메모리 대체 구현)


def test_startup_entry_only_for_the_exe_and_only_ours_is_removed():
    run = prefs.MemoryRun()
    source = prefs.Startup(run, exe="")
    assert not source.supported
    with pytest.raises(prefs.StartupError, match="소스"):
        source.enable()
    startup = prefs.Startup(run, exe=EXE)
    assert startup.state() == "off"
    startup.enable()
    assert run.values == {"NoseOut": f'"{EXE}" --startup'} and startup.state() == "on"
    assert prefs.Startup(run, exe=r"E:\New\NoseOut.exe").state() == "moved"   # EXE 폴더를 옮김
    startup.disable()
    assert run.values == {}
    # 같은 이름이지만 이 앱이 만들지 않은 항목은 덮어쓰지도 지우지도 않는다
    run.values["NoseOut"] = r'"C:\Other\nose.exe" /quiet'
    assert startup.state() == "other"
    with pytest.raises(prefs.StartupError):
        startup.enable()
    with pytest.raises(prefs.StartupError):
        startup.disable()
    assert run.values == {"NoseOut": r'"C:\Other\nose.exe" /quiet'}
    assert prefs.Startup(prefs.MemoryRun(approved={"NoseOut": False}), exe=EXE).disabled_by_windows()


def test_real_registry_is_not_used_in_tests():
    assert prefs.RegistryRun is prefs.MemoryRun                          # conftest가 바꿔 둔다
    assert isinstance(prefs.Startup().backend, prefs.MemoryRun)


# 창 위치


def test_window_rect_stays_on_a_visible_screen():
    screens = [(0, 0, 1920, 1040), (1920, 0, 2560, 1400)]
    assert prefs.fit_rect([2100, 100, 820, 540], screens, (820, 540)) == (2100, 100, 820, 540)   # 그대로
    # 두 번째 모니터를 뺐다: 첫 화면 가운데로
    x, y, w, h = prefs.fit_rect([2100, 100, 820, 540], screens[:1], (820, 540))
    assert (w, h) == (820, 540) and 0 <= x and x + w <= 1920 and prefs.TITLE_BAR <= y and y + h <= 1040
    # 반쯤 걸친 창은 그 화면 안으로, 화면보다 큰 창은 줄인다
    assert prefs.fit_rect([1700, 900, 820, 540], screens[:1], (820, 540)) == (1100, 500, 820, 540)
    assert prefs.fit_rect([0, -500, 4000, 3000], screens[:1], (820, 540))[2:] == (1920, 1040 - prefs.TITLE_BAR)
    assert prefs.fit_rect([], screens, (820, 540))[2:] == (820, 540)


# 앱: 적용·취소·기본값·실패 복구


def test_apply_saves_and_changes_hotkey_startup_theme_view(make_app, monkeypatch):
    themes = []
    monkeypatch.setattr(app, "apply_theme", lambda _qt, theme: themes.append(theme))
    run = prefs.MemoryRun()
    application, base = make_app(app.Settings(["D:\\docs"], exclusions=["D:\\docs\\tmp"]), run=run)
    application.hotkey.register()
    dialog = application.preferences
    dialog.load(application.settings)
    assert not dialog.apply_button.isEnabled()                          # 바꾼 것이 없으면 적용할 것도 없다
    dialog.hotkey_edit.setKeySequence(QKeySequence("Ctrl+Alt+K"))
    dialog.launch_box.setChecked(True)
    dialog.theme_box.setCurrentIndex(dialog.theme_box.findData("dark"))
    dialog.view_box.setCurrentIndex(dialog.view_box.findData("list"))
    dialog.start_mode.setCurrentIndex(1)
    assert dialog.apply_button.isEnabled()
    assert dialog.apply_now()
    data = saved(base)
    assert (data["hotkey"], data["launch_at_login"], data["theme"], data["large_view"], data["start_in_tray"]) == \
        ("Ctrl+Alt+K", True, "dark", "list", True)
    assert data["folders"] == ["D:\\docs"] and data["exclusions"] == ["D:\\docs\\tmp"]   # 범위는 그대로
    assert application.keys.combos() == {hotkey.parse("Ctrl+Alt+K")}
    assert run.values == {"NoseOut": f'"{EXE}" --startup'}
    assert themes[-1] == "dark" and application.window.large_view == "list"
    application.window.set_expanded(True)
    assert application.window.views.currentWidget() is application.window.table   # 큰 검색창 기본 보기: 목록
    application.window.set_expanded(False)
    assert application.window.views.currentWidget() is application.window.table   # 작은 검색창은 늘 목록
    # 다시 시작하면 그대로 복원된다
    again = app.Settings.load(base / "settings.json")
    assert (again.hotkey, again.launch_at_login, again.theme, again.large_view) == ("Ctrl+Alt+K", True, "dark", "list")


def test_cancel_discards_only_unapplied_changes(make_app):
    application, base = make_app(app.Settings())
    dialog = application.preferences
    dialog.theme_box.setCurrentIndex(dialog.theme_box.findData("light"))
    assert dialog.apply_now()                                           # 적용한 것
    dialog.theme_box.setCurrentIndex(dialog.theme_box.findData("dark"))
    dialog.view_box.setCurrentIndex(dialog.view_box.findData("list"))   # 적용하지 않은 것
    dialog.reject()
    assert application.settings.theme == "light" and saved(base)["theme"] == "light"   # 적용한 것은 남는다
    assert saved(base)["large_view"] == "grid"
    assert dialog.values()["theme"] == "light" and dialog.values()["large_view"] == "grid"   # 입력도 적용 값으로


def test_hotkey_conflict_keeps_the_old_hotkey_and_settings(make_app):
    keys = FakeKeys(taken=["Ctrl+Alt+K"])
    application, base = make_app(app.Settings(), keys=keys)
    application.hotkey.register()
    before = (base / "settings.json").read_bytes()
    dialog = application.preferences
    messages = []
    dialog.warn = lambda title, text: messages.append(text)
    dialog.hotkey_edit.setKeySequence(QKeySequence("Ctrl+Alt+K"))
    dialog.theme_box.setCurrentIndex(dialog.theme_box.findData("dark"))
    assert not dialog.apply_now()
    assert "다른 프로그램" in messages[-1] and "Ctrl+Shift+Space" in messages[-1]
    assert keys.combos() == {hotkey.parse("Ctrl+Shift+Space")} and application.hotkey.registered
    assert (base / "settings.json").read_bytes() == before and application.settings.theme == "system"
    assert dialog.values()["hotkey"] == "Ctrl+Alt+K"                     # 입력은 남겨 고칠 수 있게
    # 잘못된 조합은 등록해 보기 전에 거른다
    dialog.hotkey_edit.setKeySequence(QKeySequence("Shift+A"))
    assert not dialog.apply_now() and "Shift" in messages[-1]
    assert keys.combos() == {hotkey.parse("Ctrl+Shift+Space")}


def test_save_failure_rolls_back_hotkey_and_startup(make_app, monkeypatch):
    run = prefs.MemoryRun()
    application, base = make_app(app.Settings(), run=run)
    application.hotkey.register()
    before = (base / "settings.json").read_bytes()
    real = os.replace

    def broken(src, dst):
        if str(dst).endswith("settings.json"):
            raise OSError(13, "액세스가 거부되었습니다")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", broken)
    error = application.apply_preferences({"hotkey": "Ctrl+Alt+J", "launch_at_login": True, "start_in_tray": False,
                                           "theme": "dark", "large_view": "grid"})
    assert error and "저장하지 못했습니다" in error
    assert (base / "settings.json").read_bytes() == before
    assert application.settings.hotkey == "Ctrl+Shift+Space" and application.settings.launch_at_login is False
    assert application.keys.combos() == {hotkey.parse("Ctrl+Shift+Space")} and application.hotkey.text == "Ctrl+Shift+Space"
    assert run.values == {}                                             # 등록했던 시작 항목도 되돌림


def test_startup_failure_rolls_back_hotkey(make_app):
    run = prefs.MemoryRun(fail="set")
    application, base = make_app(app.Settings(), run=run)
    application.hotkey.register()
    error = application.apply_preferences({"hotkey": "Ctrl+Alt+J", "launch_at_login": True, "start_in_tray": False,
                                           "theme": "system", "large_view": "grid"})
    assert error and "시작 항목" in error
    assert application.keys.combos() == {hotkey.parse("Ctrl+Shift+Space")}
    assert saved(base)["hotkey"] == "Ctrl+Shift+Space" and saved(base)["launch_at_login"] is False


def test_turning_off_removes_only_our_entry_and_source_run_cannot_turn_on(make_app):
    run = prefs.MemoryRun()
    application, base = make_app(app.Settings(launch_at_login=True), run=run)
    run.values["NoseOut"] = f'"{EXE}" --startup'
    run.values["Other"] = r'"C:\Other\other.exe"'
    assert application.apply_preferences({"launch_at_login": False}) is None
    assert run.values == {"Other": r'"C:\Other\other.exe"'} and saved(base)["launch_at_login"] is False
    source, _ = make_app(None, run=prefs.MemoryRun(), exe="")
    assert not source.preferences.launch_box.isEnabled() and "소스" in source.preferences.launch_note.text()
    assert "소스" in source.apply_preferences({"launch_at_login": True})


def test_restore_defaults_shows_changes_and_leaves_scope(make_app, tmp_path):
    settings = app.Settings(["D:\\docs"], hotkey="Ctrl+Alt+K", exclusions=["D:\\docs\\tmp"], name_folders=["D:\\"],
                            extra_files=["D:\\x\\a.txt"], theme="dark", large_view="list", start_in_tray=True,
                            window_small=[10, 40, 700, 500])
    application, base = make_app(settings)
    dialog = application.preferences
    dialog.load(application.settings)
    shown = []
    dialog.confirm = lambda title, text: shown.append(text) or True
    assert dialog.restore_defaults()
    text = shown[-1]
    for line in ("검색 창 단축키: Ctrl+Alt+K → Ctrl+Shift+Space", "테마: 어둡게 → 시스템 설정 따르기",
                 "큰 검색창 기본 보기: 파일 목록 → 그림 격자", "앱 시작 시: 트레이에서 시작 → 검색 창 표시",
                 "검색창 크기·위치: 저장한 크기·위치 유지 → 기본 크기·화면 가운데로 초기화"):
        assert line in text
    assert "검색 범위·제외 규칙·개별 분석 파일·색인은 바꾸지 않습니다" in text
    assert saved(base)["theme"] == "dark"                               # 적용 전에는 아무것도 바뀌지 않는다
    assert dialog.apply_now()
    data = saved(base)
    assert (data["hotkey"], data["theme"], data["large_view"], data["start_in_tray"], data["window_small"]) == \
        ("Ctrl+Shift+Space", "system", "grid", False, [])
    assert (data["folders"], data["exclusions"], data["name_folders"], data["extra_files"]) == \
        (["D:\\docs"], ["D:\\docs\\tmp"], ["D:\\"], ["D:\\x\\a.txt"])
    dialog.confirm = lambda title, text: pytest.fail("이미 기본값이면 묻지 않는다")
    dialog.warn = lambda title, text: shown.append(text)
    assert not dialog.restore_defaults() and "이미 기본값" in shown[-1]


def test_scope_tab_is_the_same_folder_screen_and_saves_immediately(make_app, tmp_path):
    application, base = make_app(app.Settings())
    dialog = application.preferences
    assert dialog.tabs.widget(dialog.TABS.index("scope")) is application.folders
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == ["일반", "검색 범위", "화면", "색인 현황"]
    tabs = application.folders.tabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == \
        ["AI 분석 폴더", "코드 폴더", "제외 폴더(공통)", "이름 검색 범위", "AI 분석 개별 파일"]
    folder = tmp_path / "docs"
    folder.mkdir()
    dialog.theme_box.setCurrentIndex(dialog.theme_box.findData("dark"))   # 적용하지 않은 화면 설정
    assert application.folders.add_folder(str(folder))
    assert saved(base)["folders"] == [str(folder)]                      # 범위는 누르는 즉시 저장
    dialog.reject()
    assert saved(base)["folders"] == [str(folder)] and saved(base)["theme"] == "system"
    application.show_folders()
    assert dialog.isVisible() and dialog.tabs.currentIndex() == dialog.TABS.index("scope")
    assert any(a.text() == "설정…" for a in application.tray_menu.actions())
    assert application.window.settings_button.text() == "설정…"


def test_window_size_and_position_restore(make_app, qt):
    application, base = make_app(app.Settings())
    window = application.window
    window.show()
    assert window.minimumSizeHint().width() <= app.SMALL_SIZE[0]       # 작은 검색창 기본 크기에 버튼이 들어간다
    window.hide()                                                      # 옮기지 않고 닫아도 지금 자리를 남긴다
    g = window.geometry()
    assert saved(base)["window_small"] == [g.x(), g.y(), g.width(), g.height()]
    window.show()
    window.setGeometry(10, 60, 790, 480)
    assert wait_for(lambda: window.geometries["small"] == [10, 60, 790, 480])
    window.set_expanded(True)
    window.setGeometry(5, 50, 795, 500)
    assert wait_for(lambda: window.geometries["large"] == [5, 50, 795, 500])
    window.set_expanded(False)
    assert wait_for(lambda: window.geometry().width() == 790)           # 작은 검색창의 크기로 돌아온다
    window.hide()
    data = saved(base)
    assert data["window_small"] == [10, 60, 790, 480] and data["window_large"] == [5, 50, 795, 500]
    application.quit()
    # 다시 시작: 모니터 구성이 바뀌어 저장 위치가 화면 밖이어도 보이는 곳에 둔다
    settings = app.Settings.load(base / "settings.json")
    settings.window_small = [9000, 9000, 790, 480]
    settings.save(base / "settings.json")
    again, _ = make_app()
    geometry = again.window.geometry()
    screen = app.screen_rects()[0]
    assert geometry.width() == 790 and screen[0] <= geometry.x() and geometry.right() < screen[0] + screen[2]
    assert screen[1] <= geometry.y() and geometry.bottom() < screen[1] + screen[3]


def test_start_in_tray_keeps_the_window_hidden(make_app, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    application, _ = make_app(app.Settings(start_in_tray=True), start=True)
    QCoreApplication.processEvents()
    assert not application.window.isVisible()
    application.quit()
    shown, _ = make_app(app.Settings(start_in_tray=False), start=True)
    assert wait_for(lambda: shown.window.isVisible(), 3)


# 색인 현황·작업 상태·결과 없음 안내


def make_index(base: Path, rows: list[tuple[str, str, str]]) -> None:
    with IndexStore(base / "index.sqlite") as store:
        for path, state, detail in rows:
            store.upsert_file(path, "doc", 10, 1.0, 0, 0)
            store.db.execute("UPDATE files SET state = ?, detail = ?, indexed_at = ? WHERE path = ?",
                             (state, detail or None, 1_700_000_000.0, path))
        store.commit()


def test_stats_without_indexes_create_nothing(tmp_path):
    base = tmp_path / "home"
    data = stats.collect(base, ["D:\\docs"], [], [], [])
    assert data["names"] == {"exists": False} and not data["ai"]["exists"] and not data["code"]["exists"]
    assert data["ai"]["unchecked_roots"] == 1 and data["storage"]["total"] == 0
    assert not base.exists()                                            # 읽기만: 빈 색인을 만들지 않는다


def test_stats_count_only_the_current_scope(tmp_path):
    base = tmp_path / "home"
    docs, old = "D:\\docs", "D:\\removed"
    make_index(base, [(f"{docs}\\a.txt", "done", ""), (f"{docs}\\b.txt", "partial", ""),
                      (f"{docs}\\c.txt", "pending", ""), (f"{docs}\\d.txt", "failed", "암호가 걸린 PDF"),
                      (f"{docs}\\tmp\\e.txt", "done", ""), (f"{old}\\f.txt", "done", ""),
                      ("D:\\x\\chosen.txt", "done", "")])
    data = stats.collect(base, [docs, "D:\\new"], [], ["D:\\x\\chosen.txt"], [f"{docs}\\tmp"])
    ai = data["ai"]
    assert (ai["done"], ai["partial"], ai["pending"], ai["failed"]) == (2, 1, 1, 1)   # 뺀 폴더·제외는 세지 않음
    assert ai["failures"] == [(f"{docs}\\d.txt", "암호가 걸린 PDF")]
    assert ai["unchecked_roots"] == 1 and ai["last"] == 1_700_000_000.0             # D:\new는 아직 확인 전
    assert data["storage"]["index"] > 0


@pytest.mark.parametrize("info, expected", [
    ({}, ""),
    ({"searchable": 0, "scope": False}, "내용 분석 범위가 없습니다"),
    ({"searchable": 0, "scope": True, "running": True, "remaining": None}, "아직 내용 분석 전입니다"),
    ({"searchable": 5, "scope": True, "running": True, "remaining": None}, "분석 중이며 완료된 5개만 검색했습니다"),
    ({"searchable": 5, "scope": True, "remaining": 3}, "남은 3개는 아직 분석 전이라 검색 대상이 아닙니다"),
    ({"searchable": 5, "scope": True, "remaining": 0, "unchecked": 1}, "분석을 시작하지 않은 폴더"),
    ({"searchable": 5, "scope": True, "remaining": 0}, "분석을 마친 5개에서 찾았습니다."),
])
def test_content_note(info, expected):
    note = app.content_note(info)
    assert expected in note if expected else note == ""
    assert "일치할" not in note and "있을 수" not in note                 # 미분석 파일이 맞을 것처럼 말하지 않는다


def test_job_states_and_empty_results(make_app, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    base = tmp_path / "home"
    make_index(base, [(str(folder / "a.txt"), "done", ""), (str(folder / "b.txt"), "pending", ""),
                      (str(folder / "c.txt"), "failed", "깨진 파일")])
    application, _ = make_app(app.Settings([str(folder)], name_folders=[str(tmp_path)]))
    window = application.window
    application.refresh_stats()
    application.stats_thread.start()
    assert wait_for(lambda: application.last_stats.get("ai", {}).get("exists"))
    assert "검색 가능 1개" in window.jobs.text() and "남은 분석 1개" in window.jobs.text()
    assert "내용 분석: 대기" in window.jobs.text()
    # 이름 등록과 AI 분석이 동시에 진행: 따로 보인다. 분석 목록을 정하기 전에는 남은 수를 짐작하지 않는다.
    application._name_progress(1234, str(tmp_path))
    application.ai_job = {"state": "running", "done": 0, "total": 0, "planned": False}
    application._update_jobs()
    text = window.jobs.text()
    assert "이름 등록: 등록 중 (새 이름 1,234개)" in text and "내용 분석: 분석 중 (목록 확인 중)" in text
    assert "남은 분석 확인 중" in text
    application._index_progress(1, 3, str(folder / "a.txt"), "done")
    assert "분석 중 1/3" in window.jobs.text() and "남은 분석 1개" in window.jobs.text()
    # 결과 없음: 분석 중이면 완료된 파일만 찾았다고 안내
    window.engine_ready, window.content_state = True, "done"
    window.box.setText("없는말")
    window.show_results(window.request, "없는말", [])
    assert window.status.text().startswith("일치하는 파일 없음 · 분석 중이며 완료된 1개만 검색했습니다")
    assert "이름 등록이 진행 중" in window.status.text()
    application._name_paused(True)
    application._index_paused(True)
    assert "이름 등록: 일시정지" in window.jobs.text() and "내용 분석: 일시정지 1/3" in window.jobs.text()
    application._names_finished({"added": 0, "files": 3, "folders": 1})
    application._index_finished({"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "done": 1, "skipped": 0,
                                 "failed": 1, "results": {}, "errors": []})
    assert wait_for(lambda: "내용 분석: 완료" in window.jobs.text() and "이름 등록: 완료" in window.jobs.text())
    window.show_results(window.request, "없는말", [])
    assert "남은 1개는 아직 분석 전이라 검색 대상이 아닙니다" in window.status.text()
    application._index_failed("검색 엔진을 시작하지 못했습니다.")
    assert "내용 분석: 실패" in window.jobs.text()
    # 설정 > 색인 현황: 실패 이유와 저장 공간
    application.show_settings("status")
    assert wait_for(lambda: "실패 1" in application.preferences.stats_labels["ai"].text())
    application.preferences.failures.setCurrentRow(0)
    assert "깨진 파일" in application.preferences.failure_reason.text()
    assert "합계" in application.preferences.stats_labels["storage"].text()
    opened = []
    application.preferences.open_folder = opened.append
    application.preferences.open_logs_button.click()
    assert opened == [str(base)]


def test_no_index_state(make_app):
    application, base = make_app(app.Settings())
    application.stats_thread.start()
    application.refresh_stats()
    assert wait_for(lambda: "ai" in application.last_stats)
    assert application.window.jobs.isHidden()                          # 범위도 색인도 없으면 작업 줄을 숨긴다
    application.show_settings("status")
    assert wait_for(lambda: application.preferences.stats_labels["ai"].text().startswith("아직 없음"))
    assert application.preferences.stats_labels["names"].text().startswith("아직 없음")
    assert application.preferences.stats_labels["code"].text() == "아직 없음"
    window = application.window
    window.engine_ready = True
    window.box.setText("아무거나")
    window.show_results(window.request, "아무거나", [])
    assert window.status.text() == "일치하는 파일 없음 · 내용 분석 범위가 없습니다. 설정 > 검색 범위에서 폴더를 고르면 " \
                                   "내용으로도 찾습니다."
    assert not (base / "index.sqlite").exists() and not (base / "names.sqlite").exists()


def test_standard_buttons_are_korean(qt):
    from PySide6.QtWidgets import QMessageBox

    assert app.install_korean(qt)
    box = QMessageBox(QMessageBox.Icon.Question, "기본값 복원", "바꿀까요?",
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    labels = {b.text().replace("&", "") for b in box.buttons()}
    assert not labels & {"Yes", "No"}, labels
