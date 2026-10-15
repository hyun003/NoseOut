"""NoseOut 화면 (5단계, PySide6): 트레이, 전역 단축키, 검색 창, 검색 폴더·색인.

실행: .venv\\Scripts\\python -m noseout app
설정·색인 위치: %LOCALAPPDATA%\\NoseOut (환경 변수 NOSEOUT_HOME으로 바꿀 수 있다). settings.json, index.sqlite, app.log.
옛 이름(%LOCALAPPDATA%\\DigUpWin, DIGUPWIN_HOME)에서의 이전은 paths.py.
원본 DigUp의 검색 패널(⇧⌘Space, ↑↓, ↩ 열기, ⌘↩ Finder에서 보기)을 Windows식으로 옮겼다:
Ctrl+Shift+Space, ↑↓, Enter 열기, Ctrl+Enter 탐색기에서 보기, Esc 숨기기.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QImageReader, QKeySequence, QPainter, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QStackedWidget, QStyle,
                               QSystemTrayIcon, QTreeWidget, QTabWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import code as code_files
from . import hotkey, opener, paths, prefs, rules, runtime_setup, scope, stats
from .services import (IndexService, NameSearchService, NameService, SearchService, StatsService, check_file,
                       check_folder, check_name_folder)
from .watcher import WatchService

log = logging.getLogger("noseout")

KIND_LABELS = {"screenshot": "스크린샷", "image": "그림", "pdf": "PDF", "doc": "문서", "audio": "음성", "video": "영상"}
NAME_PREVIEW = 8          # 내용 결과가 있을 때 이름 일치는 이만큼만 보이고 '더 보기'로 펼친다
REFRESH_MS = 3000         # AI 색인 중 의미 검색 벡터를 다시 읽는 간격 (다시 읽기 시간은 docs/progress.md)
STATUS_LABELS = {"unavailable": "접근 불가", "unknown": "상태 미확인"}
THEMES = {"system": "시스템 설정 따르기", "light": "밝게", "dark": "어둡게"}
VIEWS = {"grid": "그림 격자", "list": "파일 목록"}     # 큰 검색창의 기본 보기 (작은 검색창은 늘 목록)
SMALL_SIZE, LARGE_SIZE = (820, 540), (1180, 760)
STATS_MS = 3000           # 분석·등록 중 색인 현황을 다시 세는 간격
POLL_MS = 1000            # USN 변경 기록을 읽는 간격 (이름 목록, ntfs.py)
NAME_METHODS = {"auto": "자동 (변경 기록 사용, 권한 요청 없음)", "ntfs": "빠른 NTFS (처음 등록에 관리자 권한)",
                "walk": "폴더 순회 (기존 방식)"}


def code_font() -> QFont:
    """코드 문맥은 고정폭으로 (QApplication이 생긴 뒤에 만든다)."""
    return QFont("Consolas", 10)


# 설정


def home() -> Path:
    return paths.home()


@dataclass
class Settings:
    folders: list[str] = field(default_factory=list)
    hotkey: str = hotkey.DEFAULT
    exclusions: list[str] = field(default_factory=list)
    watch: bool = True        # 고른 폴더의 변경을 자동으로 색인에 반영 (6단계)
    code_folders: list[str] = field(default_factory=list)   # 코드 검색 폴더 (6단계, 원본 codeRoots)
    code_editor: str = ""     # 코드 결과를 열 편집기: ""(자동), opener.EDITORS의 id, "notepad"
    # 이름만 검색하는 범위(드라이브 루트 가능). 파일 이름·경로만 읽고 내용은 읽지 않는다(2026-10-14).
    name_folders: list[str] = field(default_factory=list)
    # 이름 결과에서 '내용 분석'으로 하나씩 고른 파일 (그 파일만 AI가 읽는다. 부모 폴더는 승인하지 않는다)
    extra_files: list[str] = field(default_factory=list)
    # 설정창 일반·화면 (2026-10-14). 기존 settings.json에 없으면 아래 기본값: 로그인 시 실행은 꺼짐, 시작하면 검색 창 표시,
    # 시스템 테마, 큰 검색창은 그림 격자(이전 동작), 창 위치는 저장값 없음.
    launch_at_login: bool = False
    start_in_tray: bool = False
    theme: str = "system"
    large_view: str = "grid"
    window_small: list[int] = field(default_factory=list)   # x, y, 너비, 높이 (작은 검색창)
    window_large: list[int] = field(default_factory=list)   # 큰 검색창
    # 이름 목록 방식 (2026-10-15, ntfs.py): auto(USN으로 따라잡기, UAC 없음) | ntfs(처음 등록을 MFT로, 사용자가 누를 때만 UAC) | walk
    name_method: str = "auto"

    @staticmethod
    def load(path: Path) -> "Settings":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Settings(folders=[str(f) for f in data.get("folders", [])],
                            hotkey=str(data.get("hotkey", hotkey.DEFAULT)),
                            exclusions=[str(f) for f in data.get("exclusions", [])],
                            watch=data.get("watch", True) is not False,
                            code_folders=[str(f) for f in data.get("code_folders", [])],
                            code_editor=str(data.get("code_editor", "")),
                            name_folders=[str(f) for f in data.get("name_folders", [])],
                            extra_files=[str(f) for f in data.get("extra_files", [])],
                            launch_at_login=data.get("launch_at_login") is True,
                            start_in_tray=data.get("start_in_tray") is True,
                            theme=data.get("theme") if data.get("theme") in THEMES else "system",
                            large_view=data.get("large_view") if data.get("large_view") in VIEWS else "grid",
                            window_small=_rect(data.get("window_small")),
                            window_large=_rect(data.get("window_large")),
                            name_method=data.get("name_method") if data.get("name_method") in NAME_METHODS else "auto")
        except (OSError, ValueError, AttributeError, TypeError):
            return Settings()

    def save(self, path: Path) -> None:
        """임시 파일에 쓴 뒤 바꿔 넣는다(prefs.atomic_write). 실패하면 OSError, 기존 settings.json은 그대로."""
        prefs.atomic_write(path, json.dumps(asdict(self), ensure_ascii=False, indent=2))


def _rect(value) -> list[int]:
    if isinstance(value, list) and len(value) == 4 and all(isinstance(v, int) and not isinstance(v, bool) for v in value)             and value[2] > 0 and value[3] > 0:
        return list(value)
    return []


def apply_theme(qt_app, theme: str) -> None:
    """시스템·밝게·어둡게. Unknown은 Windows 설정을 다시 따른다(Qt 6.8+ QStyleHints.setColorScheme)."""
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(theme, Qt.ColorScheme.Unknown)
    try:
        qt_app.styleHints().setColorScheme(scheme)
    except AttributeError:
        log.warning("color scheme is not supported by this Qt")


def screen_rects() -> list[tuple[int, int, int, int]]:
    """작업 영역(작업 표시줄 제외), 주 모니터가 먼저."""
    primary = QGuiApplication.primaryScreen()
    screens = sorted(QGuiApplication.screens(), key=lambda s: s is not primary)
    return [(r.x(), r.y(), r.width(), r.height()) for r in (s.availableGeometry() for s in screens)]


# 첫 설정: 고를 만한 폴더 (경로만 확인하고 안을 열지 않는다)

KNOWN_FOLDERS = {   # SHGetKnownFolderPath: OneDrive 등으로 옮긴 위치도 따른다
    "문서": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "바탕 화면": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "다운로드": "{374DE290-123F-4565-9164-39C4925E467B}", "사진": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
}
WORK_FOLDER_NAMES = ("Projects", "projects", "source\\repos", "repos", "dev", "work", "workspace", "code", "GitHub")


def known_folder(guid: str) -> str | None:
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ubyte * 16)]

        value = GUID()
        ctypes.memmove(value.data, uuid.UUID(guid).bytes_le, 16)
        out = ctypes.c_wchar_p()
        shell = ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                               ctypes.POINTER(ctypes.c_wchar_p)]
        if shell.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(out)) != 0:
            return None
        path = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        return path
    except (AttributeError, OSError, ValueError):
        return None


def setup_candidates() -> list[tuple[str, str]]:
    """(이름, 경로): 문서·바탕 화면·다운로드·사진, 그리고 사용자 폴더 바로 아래의 작업 폴더(있는지만 본다). 폴더 안의
    파일은 열지 않는다(사용자가 고르기 전에는 내용을 읽지 않는다). 색인 금지 규칙(check_folder)에 걸리는 것은 뺀다."""
    out, seen = [], set()
    candidates = [(label, known_folder(guid)) for label, guid in KNOWN_FOLDERS.items()]
    candidates += [(f"작업 폴더 ({name})", str(Path.home() / name)) for name in WORK_FOLDER_NAMES]
    for label, path in candidates:
        if not path or not os.path.isdir(path) or os.path.normcase(path) in seen or check_folder(path):
            continue
        seen.add(os.path.normcase(path))
        out.append((label, os.path.normpath(path)))
    return out


class SetupDialog(QDialog):
    """첫 실행: 검색할 폴더를 한 번에 고른다. 고르기 전에는 어떤 폴더의 내용도 읽지 않는다."""

    def __init__(self, candidates: list[tuple[str, str]], drive: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NoseOut 처음 설정")
        self.setWindowIcon(app_icon())
        self.resize(640, 420)
        intro = QLabel("내용까지 찾을 폴더를 고르세요(AI가 이 폴더 안의 문서·그림·음성·영상을 읽습니다). 고르기 전에는 "
                       "폴더 안을 열지 않습니다. 최근 파일·작은 문서부터 분석하고 큰 영상·음성은 나중에 읽습니다.")
        intro.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        self.boxes: list[tuple[QCheckBox, str]] = []
        for label, path in candidates:
            box = QCheckBox(f"{label}  —  {path}")
            box.setChecked(True)
            layout.addWidget(box)
            self.boxes.append((box, path))
        self.extra: list[str] = []
        self.add_button = QPushButton("다른 폴더 추가…")
        layout.addWidget(self.add_button)
        self.drive = drive
        self.drive_box = QCheckBox(f"{drive} 드라이브 전체를 이름으로 검색 (파일 이름·경로만, 내용은 읽지 않음. "
                                   "Windows·프로그램·앱 데이터·숨김·개발 폴더·비밀 파일 제외)")
        self.drive_box.setChecked(True)
        layout.addWidget(self.drive_box)
        self.start_box = QCheckBox("확인을 누르면 고른 폴더의 내용 분석을 바로 시작")
        self.start_box.setChecked(True)
        layout.addWidget(self.start_box)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.later_button = QPushButton("나중에")
        self.ok_button = QPushButton("확인")
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.ok_button)
        layout.addLayout(buttons)
        self.ok_button.clicked.connect(self.accept)
        self.later_button.clicked.connect(self.reject)
        self.add_button.clicked.connect(lambda: self.add_folder())

    def add_folder(self, path: str | None = None) -> bool:
        path = path or QFileDialog.getExistingDirectory(self, "검색할 폴더 고르기")
        if not path:
            return False
        problem = check_folder(os.path.normpath(path))
        if problem:
            QMessageBox.warning(self, "이 폴더는 고를 수 없습니다", problem)
            return False
        self.extra.append(os.path.normpath(path))
        box = QCheckBox(f"추가한 폴더  —  {os.path.normpath(path)}")
        box.setChecked(True)
        self.layout().insertWidget(1 + len(self.boxes), box)
        self.boxes.append((box, os.path.normpath(path)))
        return True

    def chosen(self) -> list[str]:
        return [path for box, path in self.boxes if box.isChecked()]


# 결과 표시 (UI 없이 테스트할 수 있게 함수로)


def code_line(hit) -> int | None:
    """코드 결과를 열 줄: 정의 줄이나 검색어가 든 줄(focus), 없으면 묶음의 첫 줄. 노트북(셀)은 줄이 없어 None."""
    if hit.segment != "lines" or hit.loc is None:
        return None
    focus = getattr(hit, "focus", None)
    return int(focus if focus is not None else hit.loc)


def location(hit) -> str:
    """결과가 가리키는 자리: PDF 쪽, 음성·영상 시점, 문서의 몇 번째 부분, 코드의 줄·노트북의 셀."""
    if hit.loc is None:
        return ""
    if hit.segment == "lines":
        return f"{code_line(hit)}번째 줄"
    if hit.segment == "cells":
        return f"{int(hit.loc)}번째 셀"
    if hit.segment in ("page", "page_image"):
        return f"{int(hit.loc)}쪽"
    if hit.segment in ("audio", "frame"):
        seconds = int(hit.loc)
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
            else f"{seconds // 60}:{seconds % 60:02d}"
    if hit.segment == "chunk" and hit.loc > 1:
        return f"{int(hit.loc)}번째 부분"
    return ""


def open_target(hit) -> tuple[int | None, float | None]:
    """(쪽, 초): opener.open_result에 넘길 값."""
    if hit.loc is not None and hit.segment in ("page", "page_image"):
        return int(hit.loc), None
    if hit.loc is not None and hit.segment in ("audio", "frame"):
        return None, float(hit.loc)
    return None, None


def place(hit) -> str:
    """위치 열: AI 결과는 쪽·시점·줄, 이름 결과는 접근 상태(고른 폴더를 열 수 없음, 그 폴더를 읽지 못함)."""
    status = getattr(hit, "status", None)
    return STATUS_LABELS.get(status, status) if status else location(hit)


def excerpt_text(hit) -> str:
    context = getattr(hit, "context", None)
    if context:
        # 코드 문맥: 줄 번호와 함께, 열 줄에는 '>' (고정폭 글꼴에 있는 글자라 열이 어긋나지 않는다)
        focus = code_line(hit)
        return "\n".join(f"{'>' if number == focus else ' '}{number:>5}  {line}" for number, line in context)
    text = (hit.excerpt or "").replace("«", "").replace("»", "")
    return text


def kind_label(hit) -> str:
    if hit.kind == "code":
        return f"코드 · {code_files.language(hit.path)}"
    if hit.kind == "folder":
        return "폴더"
    if hit.kind == "other":
        ext = Path(hit.path).suffix.lstrip(".").upper()
        return f"파일 · {ext}" if ext else "파일"
    return KIND_LABELS.get(hit.kind, hit.kind)


def merge(name_hits: list, content_hits: list, expanded: bool) -> tuple[list, list, set[str], int]:
    """(보일 이름 결과, 내용 결과, 이름도 맞은 내용 결과의 경로 키, 숨긴 이름 결과 수). 내용(AI) 결과의 순서·위치는 그대로
    두고, 같은 파일이 이름으로도 맞았으면 이름 칸에서 빼고 내용 칸의 그 줄에 '이름·내용'으로 표시한다."""
    content_keys = {os.path.normcase(h.path) for h in content_hits}
    names_only = [h for h in name_hits if os.path.normcase(h.path) not in content_keys]
    both = {os.path.normcase(h.path) for h in name_hits} & content_keys
    limit = len(names_only) if expanded or not content_hits else NAME_PREVIEW
    return names_only[:limit], list(content_hits), both, max(0, len(names_only) - limit)


def content_note(info: dict) -> str:
    """내용 결과가 없을 때 지금 색인·작업 상태로 이유를 안내한다(info: App._update_jobs). 숫자는 색인을 센 값만 쓰고, 세는
    중이면 말하지 않는다. 아직 분석하지 않은 파일이 이 검색어에 맞을 것이라고 말하지 않는다."""
    searchable = info.get("searchable")
    if searchable is None:
        return ""
    remaining, unchecked, running = info.get("remaining"), info.get("unchecked", 0), info.get("running", False)
    if searchable == 0:
        if not info.get("scope"):
            return "내용 분석 범위가 없습니다. 설정 > 검색 범위에서 폴더를 고르면 내용으로도 찾습니다."
        if running or remaining or unchecked:
            return "아직 내용 분석 전입니다. 분석을 마친 파일만 내용으로 찾습니다."
        return "내용 분석을 마친 파일이 없습니다."
    if running:
        return f"분석 중이며 완료된 {searchable:,}개만 검색했습니다. 아직 분석하지 않은 파일은 검색 대상이 아닙니다."
    if remaining:
        return f"분석을 마친 {searchable:,}개에서 찾았습니다. 남은 {remaining:,}개는 아직 분석 전이라 검색 대상이 아닙니다."
    if unchecked:
        return f"분석을 마친 {searchable:,}개에서 찾았습니다. 아직 분석을 시작하지 않은 폴더는 검색 대상이 아닙니다."
    return f"분석을 마친 {searchable:,}개에서 찾았습니다."


# EXE·창·트레이가 같은 벡터 원본을 사용한다.


def app_icon() -> QIcon:
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(Path(__file__).parent / "assets" / "noseout.svg")).render(painter)
    painter.end()
    return QIcon(pixmap)


# 검색 창


class SearchWindow(QWidget):
    query = Signal(int, str)          # 검색 서비스로 (AI 내용 검색)
    name_query = Signal(int, str)     # 이름 검색 서비스로 (엔진과 무관하게 늘 보낸다)
    analyze = Signal(str)             # 이름 결과의 '내용 분석' (그 파일만)
    open_folders = Signal()
    open_settings = Signal()
    hidden = Signal()                 # 창 크기·위치를 저장할 때

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NoseOut 검색")
        self.setWindowIcon(app_icon())
        self.resize(820, 540)
        self.request = 0
        self.shown_request = 0
        self.engine_ready = False
        self.engine_problem: str | None = None   # 엔진 시작 실패 안내 (이름 검색은 계속된다)
        self.hits: list = []
        self.name_hits: list = []
        self.content_hits: list = []
        self.content_state = "idle"      # idle | waiting | done | error
        self.content_error = ""
        self.names_expanded = False
        self.items: list[QTreeWidgetItem] = []   # self.hits와 같은 순서의 결과 줄 (그룹 머리 줄 제외)
        self.nav: list[QTreeWidgetItem] = []     # ↑↓로 움직이는 줄: 결과 줄과 '더 보기' 줄
        self.text = ""
        self.expanded = False
        self._thumbnail_rows: list[int] = []
        self.code_editor = ""          # 코드 결과를 열 편집기 (설정)
        self.index_info: dict = {}     # 색인·작업 상태 (결과 없음 안내, App._update_jobs)
        self.large_view = "grid"       # 큰 검색창을 열 때의 기본 보기 (설정 > 화면)
        self.geometries: dict[str, list[int]] = {"small": [], "large": []}   # 창을 닫을 때 저장, 다음 실행 때 복원

        self.box = QLineEdit()
        self.box.setPlaceholderText("찾고 싶은 것을 설명해 보세요  (예: 해변의 강아지 사진, INV-4821, code: 재시도 대기)")
        self.box.setClearButtonEnabled(True)
        self.box.setMinimumHeight(36)
        font = self.box.font()
        font.setPointSize(12)
        self.box.setFont(font)
        self.status = QLabel("검색 엔진을 준비하는 중…")
        self.status.setWordWrap(True)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.hide()
        # 이름 등록과 내용(AI) 분석의 진행·일시정지·완료를 따로 보여 준다
        self.jobs = QLabel()
        self.jobs.setWordWrap(True)
        self.jobs.setEnabled(False)    # 흐린 글자(밝게·어둡게 모두)
        self.jobs.hide()

        self.table = QTreeWidget()
        self.table.setHeaderLabels(["이름", "종류", "위치", "폴더", "일치"])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.table.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(3, 260)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setIconSize(QSize(160, 120))
        self.grid.setGridSize(QSize(210, 170))
        self.grid.setWordWrap(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.views = QStackedWidget()
        self.views.addWidget(self.table)
        self.views.addWidget(self.grid)
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("모든 파일", None)
        self.kind_filter.addItem("그림·스크린샷", {"image", "screenshot"})
        for kind, label in KIND_LABELS.items():
            self.kind_filter.addItem(label, {kind})
        self.view_mode = QComboBox()
        self.view_mode.addItems(["그림 격자", "파일 목록"])
        self.large_controls = QWidget()
        controls = QHBoxLayout(self.large_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("파일 종류"))
        controls.addWidget(self.kind_filter)
        controls.addStretch(1)
        controls.addWidget(QLabel("보기"))
        controls.addWidget(self.view_mode)
        self.large_controls.hide()
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMinimumHeight(40)

        self.open_button = QPushButton("열기  (Enter)")
        self.reveal_button = QPushButton("폴더에서 보기  (Ctrl+Enter)")
        self.folders_button = QPushButton("검색 폴더…")
        self.settings_button = QPushButton("설정…")
        self.menu_button = QPushButton("메뉴")
        self.analyze_button = QPushButton("내용 분석")
        self.analyze_button.setToolTip("분석 범위 밖의 이 파일만 내용 분석에 추가합니다(폴더 전체는 추가하지 않음).")
        self.analyze_button.setEnabled(False)
        self.analyzable = lambda hit: False      # 앱이 정한다: 분석 범위 밖이고 분석할 수 있는 이름 결과인가
        self.large_button = QPushButton("큰 검색창")
        for button in (self.open_button, self.reveal_button):
            button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.reveal_button)
        buttons.addWidget(self.analyze_button)
        buttons.addStretch(1)
        buttons.addWidget(self.large_button)
        # 작은 검색창(820px)에 버튼이 넘치지 않게 '검색 폴더…'는 메뉴로 옮겼다(설정 > 검색 범위와 같은 화면)
        self.folders_button.hide()
        buttons.addWidget(self.settings_button)
        buttons.addWidget(self.menu_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.box)
        layout.addWidget(self.status)
        layout.addWidget(self.jobs)
        layout.addWidget(self.notice)
        layout.addWidget(self.large_controls)
        layout.addWidget(self.views, 1)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(250)
        self.debounce.timeout.connect(self._send)
        self.box.textChanged.connect(lambda _: self.debounce.start())
        self.box.returnPressed.connect(self.open_selected)
        self.table.itemActivated.connect(lambda *_: self.open_selected())
        self.table.currentItemChanged.connect(self._selected)
        self.grid.currentRowChanged.connect(self._grid_selected)
        self.grid.itemActivated.connect(lambda *_: self.open_selected())
        self.large_button.clicked.connect(lambda: self.set_expanded(not self.expanded))
        self.view_mode.currentIndexChanged.connect(self._view_changed)
        self.kind_filter.currentIndexChanged.connect(self._filter_changed)
        self.open_button.clicked.connect(self.open_selected)
        self.analyze_button.clicked.connect(self._analyze_clicked)
        self.reveal_button.clicked.connect(self.reveal_selected)
        self.folders_button.clicked.connect(self.open_folders)
        self.settings_button.clicked.connect(self.open_settings)
        QShortcut(QKeySequence("Ctrl+Return"), self, self.reveal_selected)
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.reveal_selected)
        QShortcut(QKeySequence("Escape"), self, self.hide)
        self.box.installEventFilter(self)
        self.thumbnails = QTimer(self)
        self.thumbnails.setInterval(0)
        self.thumbnails.timeout.connect(self._load_thumbnail)

    # 검색

    def _send(self) -> None:
        text = self.box.text().strip()
        self.request += 1
        self.names_expanded = False
        self.name_hits, self.content_hits, self.content_error = [], [], ""
        if not text:
            self.content_state = "idle"
            self.show_results(self.request, "", [])
            return
        # 이름 검색은 AI 엔진 시작·실패·버전 불일치·색인 진행과 상관없이 바로 보낸다
        self.text = text
        self.name_query.emit(self.request, text)
        if not self.engine_ready:
            self.content_state = "error" if self.engine_problem else "waiting"
            self.content_error = self.engine_problem or ""
            self.status.setText(f"{self.engine_problem} 이름 일치만 찾습니다." if self.engine_problem
                                else "검색 엔진을 준비하는 중… 준비되면 바로 찾습니다. 이름 일치는 먼저 보여 줍니다.")
            return
        self.content_state = "waiting"
        self.status.setText("찾는 중…")
        self.query.emit(self.request, text)

    def engine_is_ready(self, message: str) -> None:
        self.engine_ready = True
        self.engine_problem = None
        self.status.setText(message)
        if self.box.text().strip():
            self._send()

    def show_results(self, request: int, text: str, hits: list) -> None:
        """내용(AI) 결과. 순서·위치(쪽·시점·줄)는 받은 그대로 보인다."""
        if request < self.request:          # 더 최근 검색이 있다
            return
        self.shown_request = request
        self.content_hits = list(hits)
        self.content_state = "done" if text else "idle"
        self.text = text
        self._render(text)

    def show_name_results(self, request: int, text: str, hits: list) -> None:
        if request < self.request or not self.box.text().strip():
            return
        self.name_hits = list(hits)
        self._render(text)

    def refresh_names(self) -> None:
        """이름 목록이 커밋되면(등록 중·감시 반영) 지금 검색어로 이름만 다시 찾는다. 요청 번호는 그대로라 내용 결과는 남는다."""
        text = self.box.text().strip()
        if text and code_files.query_text(text) is None:
            self.name_query.emit(self.request, text)

    def _render(self, text: str) -> None:
        selected = self.current_hit()
        keep = os.path.normcase(selected.path) if selected is not None else None
        names_shown, content, both, hidden = merge(self.name_hits, self.content_hits, self.names_expanded)
        self.hits = names_shown + content
        self.thumbnails.stop()
        self._thumbnail_rows.clear()
        # 줄 목록을 먼저 비운다: 격자를 지우는 동안 격자가 옛 줄 번호로 선택 신호를 보내면 이미 지운 목록 줄을 고르려 했다
        self.items, self.nav = [], []
        self.grid.clear()
        self.table.clear()
        code = code_files.query_text(text)

        def group(title: str) -> QTreeWidgetItem:
            head = QTreeWidgetItem([title])
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)     # 고를 수 없는 머리 줄
            font = head.font(0)
            font.setBold(True)
            head.setFont(0, font)
            self.table.addTopLevelItem(head)
            head.setFirstColumnSpanned(True)
            head.setExpanded(True)
            return head

        def add(parent: QTreeWidgetItem, hit, match: str) -> None:
            path = Path(hit.path)
            item = QTreeWidgetItem([path.name, kind_label(hit), place(hit), str(path.parent), match])
            item.setToolTip(0, str(path))
            item.setToolTip(3, str(path.parent))
            item.setData(0, Qt.ItemDataRole.UserRole, len(self.items))
            parent.addChild(item)
            self.items.append(item)
            self.nav.append(item)
            icon = QStyle.StandardPixmap.SP_DirIcon if getattr(hit, "is_dir", False) else QStyle.StandardPixmap.SP_FileIcon
            tile = QListWidgetItem(self.style().standardIcon(icon), f"{path.name}\n{kind_label(hit)}  {place(hit)}")
            tile.setToolTip(str(path))
            self.grid.addItem(tile)
            if hit.kind in ("image", "screenshot"):
                self._thumbnail_rows.append(self.grid.count() - 1)

        if names_shown:
            head = group(f"이름 일치 · {len(names_shown) + hidden}개")
            for hit in names_shown:
                add(head, hit, "이름")
            if hidden:
                more = QTreeWidgetItem([f"이름 일치 {hidden}개 더 보기 (Enter)"])
                more.setData(0, Qt.ItemDataRole.UserRole, -1)
                head.addChild(more)
                more.setFirstColumnSpanned(True)
                self.nav.append(more)
        waiting = {"waiting": "찾는 중…" if self.engine_ready else "검색 엔진 준비 중",
                   "error": "사용할 수 없음"}.get(self.content_state)
        if content or (names_shown and waiting):
            label = "코드" if code is not None else "내용"
            head = group(f"{label} · {len(content)}개" if content else f"{label} · {waiting}")
            for hit in content:
                add(head, hit, "이름·내용" if os.path.normcase(hit.path) in both else "내용")

        name_count = len(names_shown) + hidden
        if content and code is not None:
            self.status.setText(f"코드 결과 {len(content)}개")
        elif content:
            self.status.setText(f"이름 일치 {name_count}개 · 내용 결과 {len(content)}개" if name_count
                                else f"결과 {len(content)}개")
        elif code == "":
            self.status.setText("'code:' 뒤에 찾을 코드를 이름이나 설명으로 쓰세요. (예: code: retry with backoff)")
        elif code is not None:
            if self.content_state == "done":
                self.status.setText("찾은 코드가 없습니다. 코드 폴더를 추가하고 색인했는지 확인하세요.")
        elif name_count:
            tail = {"waiting": "내용은 찾는 중…" if self.engine_ready else "내용은 검색 엔진 준비 중",
                    "error": self.content_error or "내용 검색을 쓸 수 없습니다"}.get(self.content_state, "내용 결과 없음")
            if self.content_state == "done" and self.index_info.get("running"):
                tail += " (분석 중: 완료된 파일만 검색)"
            self.status.setText(f"이름 일치 {name_count}개 · {tail}")
        elif self.content_state == "done" or not text:
            self.status.setText(self.empty_text() if text else "")
        if self.hits:
            # 이름 결과를 보다가 내용 결과가 도착해도 고른 줄을 유지한다
            target = next((i for i, h in enumerate(self.hits) if os.path.normcase(h.path) == keep), 0)
            self.table.setCurrentItem(self.items[target])
            self.grid.setCurrentRow(target)
        else:
            self._show_detail(None)
        for button in (self.open_button, self.reveal_button):
            button.setEnabled(bool(self.hits))
        if self.views.currentWidget() is self.grid and self._thumbnail_rows:
            self.thumbnails.start()

    def empty_text(self) -> str:
        note = content_note(self.index_info) if self.engine_ready else ""
        if self.index_info.get("names_busy"):
            note += (" " if note else "") + "이름 등록이 진행 중이라 이름 결과도 아직 일부입니다."
        return f"일치하는 파일 없음 · {note}" if note else "일치하는 파일 없음"

    def set_jobs(self, text: str) -> None:
        self.jobs.setText(text)
        self.jobs.setVisible(bool(text))

    def set_expanded(self, expanded: bool) -> None:
        entering = expanded and not self.expanded
        self.expanded = expanded
        self.large_controls.setVisible(expanded)
        self.large_button.setText("작은 검색창" if expanded else "큰 검색창")
        self.apply_geometry()
        if not expanded:
            self.kind_filter.setCurrentIndex(0)
        if entering:
            self.view_mode.setCurrentIndex(0 if self.large_view == "grid" else 1)
        self._view_changed()

    # 창 크기·위치: 작은·큰 검색창을 따로 저장한다. 모니터 구성이 바뀌어 저장한 자리가 화면 밖이면 보이는 곳으로 옮긴다.

    def apply_geometry(self) -> None:
        mode = "large" if self.expanded else "small"
        default = LARGE_SIZE if self.expanded else SMALL_SIZE
        saved = self.geometries.get(mode)
        if saved:
            self.setGeometry(*prefs.fit_rect(saved, screen_rects(), default))
        else:
            self.resize(*default)

    def reset_geometry(self) -> None:
        """설정 > 화면의 기본값: 저장한 크기·위치를 지우고 기본 크기로 화면 가운데에 둔다."""
        self.geometries = {"small": [], "large": []}
        self.setGeometry(*prefs.fit_rect([], screen_rects(), LARGE_SIZE if self.expanded else SMALL_SIZE))

    def _remember_geometry(self, hiding: bool = False) -> None:
        # 숨기는 중(hideEvent)에는 isVisible이 이미 거짓이다: 옮기지 않고 닫아도 지금 자리를 남긴다
        if (hiding or self.isVisible()) and self.windowState() == Qt.WindowState.WindowNoState:
            g = self.geometry()
            self.geometries["large" if self.expanded else "small"] = [g.x(), g.y(), g.width(), g.height()]

    def moveEvent(self, event):  # noqa: N802
        super().moveEvent(event)
        self._remember_geometry()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._remember_geometry()

    def hideEvent(self, event):  # noqa: N802
        if not event.spontaneous():     # 최소화(시스템이 숨김)는 크기·위치를 바꾸지 않는다
            self._remember_geometry(hiding=True)
        super().hideEvent(event)
        self.hidden.emit()

    def _view_changed(self, *_args) -> None:
        row = self.grid.currentRow() if self.views.currentWidget() is self.grid else self._row(self.table.currentItem())
        use_grid = self.expanded and self.view_mode.currentIndex() == 0
        self.views.setCurrentWidget(self.grid if use_grid else self.table)
        if 0 <= row < len(self.hits):
            self.grid.setCurrentRow(row)
            self.table.setCurrentItem(self.items[row])
        if use_grid and self._thumbnail_rows:
            self.thumbnails.start()
        else:
            self.thumbnails.stop()

    def _filter_changed(self, *_args) -> None:
        self.invalidate_results()
        self._send()

    def invalidate_results(self) -> None:
        self.request += 1
        self.name_hits, self.content_state = [], "idle"
        self.show_results(self.request, "", [])

    def _load_thumbnail(self) -> None:
        if not self._thumbnail_rows:
            self.thumbnails.stop()
            return
        row = self._thumbnail_rows.pop(0)
        hit = self.hits[row]
        if not scope.readable_file(hit.path):
            return
        reader = QImageReader(hit.path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(QSize(160, 120), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if not image.isNull():
            self.grid.item(row).setIcon(QIcon(QPixmap.fromImage(image)))

    def show_error(self, request: int, message: str) -> None:
        """내용 검색의 문제(엔진 시작 실패 -1, 버전이 다른 색인 등). 이름 결과는 그대로 둔다."""
        if request == -1 and not self.engine_ready:
            self.engine_problem = message
        if request == -1 or request >= self.request:
            if self.box.text().strip() and self.content_state in ("waiting", "error"):
                self.content_state, self.content_error = "error", message
                self._render(self.text)
            self.status.setText(f"이름 일치 {len(self.name_hits)}개 · {message}" if self.name_hits else message)

    @staticmethod
    def _row(item) -> int:
        value = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, int) else -1

    def _selected(self, current, _previous) -> None:
        row = self._row(current)
        if row >= 0:
            self.grid.setCurrentRow(row)
        self._show_detail(self.current_hit())

    def _grid_selected(self, row: int) -> None:
        if 0 <= row < len(self.items):      # 다시 그리는 중(줄 목록이 아직 비어 있음)에는 건너뛴다
            self.table.setCurrentItem(self.items[row])
        self._show_detail(self.current_hit())

    def _analyze_clicked(self) -> None:
        hit = self.current_hit()
        if hit is not None and self.analyzable(hit):
            self.analyze.emit(hit.path)

    def _show_detail(self, hit) -> None:
        self.analyze_button.setEnabled(hit is not None and bool(self.analyzable(hit)))
        code = hit is not None and getattr(hit, "context", None)
        self.detail.setFont(code_font() if code else self.font())
        self.detail.setText(excerpt_text(hit) if hit else "")

    def current_hit(self):
        if self.views.currentWidget() is self.grid:
            row = self.grid.currentRow()
            return self.hits[row] if 0 <= row < len(self.hits) else None
        index = self._row(self.table.currentItem())
        return self.hits[index] if 0 <= index < len(self.hits) else None

    # 결과 열기

    def open_selected(self) -> None:
        current = self.table.currentItem()
        if self.views.currentWidget() is self.table and current is not None and current.parent() is not None \
                and self._row(current) == -1:
            self.names_expanded = True       # '이름 일치 더 보기' 줄
            self._render(self.text)
            return
        hit = self.current_hit()
        if hit is None:
            return
        page, seconds = open_target(hit)
        try:
            if getattr(hit, "status", None) == "unavailable":
                self.status.setText("이 파일이 있는 폴더를 지금 열 수 없습니다(드라이브 분리·이름 변경 등). "
                                    "다시 열리면 목록이 갱신됩니다.")
                return
            if getattr(hit, "is_dir", False):
                opener.open_folder(hit.path)
                self.status.setText(f"폴더를 열었습니다: {Path(hit.path).name}")
                return
            if opener.is_executable(hit.path) and (hit.kind != "code" or getattr(hit, "source", "") == "name"):
                # 프로그램·스크립트·바로 가기는 Enter로 실행하지 않는다: 탐색기에서 위치만 보여 준다.
                # 코드 검색 결과(code:)는 원래대로 편집기·메모장의 그 줄에서 연다(실행하지 않음).
                opener.reveal(hit.path)
                self.status.setText(f"실행할 수 있는 파일이라 열지 않고 위치를 보여 줍니다: {Path(hit.path).name}")
                return
            if hit.kind == "code":
                # 코드는 편집기의 그 줄에서. 연결 프로그램으로 열면 스크립트가 실행될 수 있어 쓰지 않는다.
                line = code_line(hit)
                note = opener.open_code(hit.path, line, self.code_editor)
                self.status.setText(note or f"열었습니다: {Path(hit.path).name}" + (f" ({line}번째 줄)" if line else ""))
                return
            note = opener.open_result(hit.path, page, seconds)
            self.status.setText(note or f"열었습니다: {Path(hit.path).name}")
        except FileNotFoundError:
            self.status.setText("파일이 더 이상 없습니다(옮겨졌거나 지워짐). 다시 색인하면 목록에서 빠집니다.")
        except OSError as error:
            log.exception("open failed")
            self.status.setText(f"파일을 열지 못했습니다: {error.strerror or '알 수 없는 오류'}")

    def reveal_selected(self) -> None:
        hit = self.current_hit()
        if hit is None:
            return
        try:
            opener.reveal(hit.path)
        except FileNotFoundError:
            self.status.setText("파일이 더 이상 없습니다(옮겨졌거나 지워짐).")
        except OSError:
            log.exception("reveal failed")
            self.status.setText("탐색기를 열지 못했습니다. 다시 시도하세요.")

    # 키보드: 검색 칸에서 ↑↓로 결과를 고른다 (원본 패널과 같음)

    def eventFilter(self, watched, event):  # noqa: N802
        from PySide6.QtCore import QEvent

        if watched is self.box and event.type() == QEvent.Type.KeyPress and self.hits:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                step = 1 if event.key() == Qt.Key.Key_Down else -1
                if self.views.currentWidget() is self.grid:
                    row = max(0, min(len(self.hits) - 1, self.grid.currentRow() + step))
                    self.table.setCurrentItem(self.items[row])
                    self.grid.setCurrentRow(row)
                    return True
                # 목록: 결과 줄과 '더 보기' 줄 사이를 움직인다(그룹 머리 줄은 건너뛴다)
                current = self.table.currentItem()
                at = self.nav.index(current) if current in self.nav else -1
                self.table.setCurrentItem(self.nav[max(0, min(len(self.nav) - 1, at + step))])
                return True
        return super().eventFilter(watched, event)

    def summon(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32" and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            hotkey.bring_to_front(int(self.winId()))
        self.box.setFocus()
        self.box.selectAll()

    def closeEvent(self, event):  # noqa: N802
        # 창을 닫아도 트레이에서 계속 돈다. 끝내려면 트레이 메뉴의 '종료'.
        event.ignore()
        self.hide()


# 검색 폴더와 색인


class FoldersDialog(QWidget):
    """검색 범위 관리. 설정창의 '검색 범위' 탭에 그대로 들어간다(2026-10-14: 따로 뜨던 '검색 폴더' 창과 같은 화면·같은 저장).
    여기서 바꾼 범위는 누르는 즉시 settings.json에 저장된다(설정창의 적용·취소·기본값 복원과 무관)."""

    start_indexing = Signal(list)
    stop_indexing = Signal()
    changed = Signal(list)
    exclusions_changed = Signal(list)
    pause_indexing = Signal()
    resume_indexing = Signal()
    watch_toggled = Signal(bool)
    code_changed = Signal(list)
    editor_changed = Signal(str)
    name_folders_changed = Signal(list)
    extra_changed = Signal(list)
    name_pause = Signal()
    name_resume = Signal()
    name_cancel = Signal()
    name_rescan = Signal()
    name_method_changed = Signal(str)
    name_walk_check = Signal()

    def __init__(self, folders: list[str], parent=None, exclusions: list[str] | None = None, watch: bool = True,
                 code_folders: list[str] | None = None, code_editor: str = "", name_folders: list[str] | None = None,
                 extra_files: list[str] | None = None, name_method: str = "auto"):
        super().__init__(parent)
        self.setWindowTitle("검색 폴더")
        self.setWindowIcon(app_icon())
        self.resize(760, 580)
        self.stop_requested = False
        self.paused = False
        self.auto = False
        self._before_auto = ""
        intro = QLabel("<b>이 탭의 변경은 누르는 즉시 저장됩니다</b>(아래 적용·취소·기본값 복원과 무관). "
                       "<b>이름 검색 범위</b>는 파일 이름·경로만 등록하고, <b>AI 분석</b> 폴더·개별 파일은 내용까지 읽습니다. "
                       "파일은 바꾸지 않습니다. AI 분석 폴더로 드라이브 전체·사용자 폴더 전체·Windows와 프로그램 폴더는 "
                       "고를 수 없습니다.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.addItems(folders)
        self.excluded_list = QListWidget()
        self.excluded_list.addItems(exclusions or [])
        self.code_list = QListWidget()
        self.code_list.addItems(code_folders or [])
        self.add_code_button = QPushButton("코드 폴더 추가…")
        self.remove_code_button = QPushButton("목록에서 빼기")
        self.editor_box = QComboBox()
        self.editor_box.addItem("자동 (설치된 편집기)", "")
        installed = opener.installed_editors()
        for editor in opener.EDITORS:
            if editor.id in installed:
                self.editor_box.addItem(editor.name, editor.id)
        self.editor_box.addItem("메모장 (줄 위치로 열지 못함)", "notepad")
        self.editor_box.setCurrentIndex(max(0, self.editor_box.findData(code_editor)))
        found = ", ".join(e.name for e in opener.EDITORS if e.id in installed)
        self.editor_note = QLabel(f"줄 위치로 여는 편집기: {found}" if found else
                                  f"줄 위치로 여는 편집기({opener.SUPPORTED})를 찾지 못했습니다. 코드는 메모장으로 "
                                  "처음부터 열립니다.")
        self.editor_note.setWordWrap(True)
        self.add_button = QPushButton("폴더 추가…")
        self.remove_button = QPushButton("목록에서 빼기")
        self.exclude_button = QPushButton("제외 폴더 추가…")
        self.unexclude_button = QPushButton("제외 해제")
        self.start_button = QPushButton("색인 시작")
        self.stop_button = QPushButton("중지")
        self.stop_button.setEnabled(False)
        self.pause_button = QPushButton("일시정지")
        self.pause_button.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.state = QLabel("대기 중")
        self.state.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setPlaceholderText("색인 결과와 문제가 있는 파일이 여기 표시됩니다.")

        row = QHBoxLayout()
        for button in (self.add_button, self.remove_button):
            row.addWidget(button)
        row.addStretch(1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.pause_button)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        included = QWidget()
        included_layout = QVBoxLayout(included)
        included_layout.addWidget(self.list)
        included_layout.addLayout(row)
        excluded = QWidget()
        excluded_layout = QVBoxLayout(excluded)
        excluded_layout.addWidget(self.excluded_list)
        excluded_buttons = QHBoxLayout()
        excluded_buttons.addWidget(self.exclude_button)
        excluded_buttons.addWidget(self.unexclude_button)
        excluded_buttons.addStretch(1)
        excluded_layout.addLayout(excluded_buttons)
        coding = QWidget()
        coding_layout = QVBoxLayout(coding)
        code_intro = QLabel("코드 폴더의 소스 파일·저장소 문서만 별도 코드 색인에 넣습니다. 검색어를 'code:'로 시작하면 "
                            "코드만 찾습니다(예: code: retry with backoff). 의존성·빌드 폴더(node_modules, vendor, build, "
                            "dist 등), git이 무시하는 파일, 데이터·비밀·1MB 넘는 파일은 읽지 않습니다.")
        code_intro.setWordWrap(True)
        coding_layout.addWidget(code_intro)
        coding_layout.addWidget(self.code_list)
        code_buttons = QHBoxLayout()
        code_buttons.addWidget(self.add_code_button)
        code_buttons.addWidget(self.remove_code_button)
        code_buttons.addStretch(1)
        code_buttons.addWidget(QLabel("코드 열기"))
        code_buttons.addWidget(self.editor_box)
        coding_layout.addLayout(code_buttons)
        coding_layout.addWidget(self.editor_note)
        self.tabs = QTabWidget()
        self.tabs.addTab(included, "AI 분석 폴더")
        self.tabs.addTab(coding, "코드 폴더")
        self.tabs.addTab(excluded, "제외 폴더(공통)")
        # 이름 검색 범위 (드라이브 전체 가능)
        self.name_list = QListWidget()
        self.name_list.addItems(name_folders or [])
        self.add_name_button = QPushButton("드라이브·폴더 추가…")
        self.remove_name_button = QPushButton("목록에서 빼기")
        self.name_pause_button = QPushButton("이름 등록 일시정지")
        self.name_cancel_button = QPushButton("중지")
        self.name_rescan_button = QPushButton("다시 등록")
        self.name_state = QLabel("이름 목록: 대기 중")
        self.name_state.setWordWrap(True)
        self.name_paused = False
        naming = QWidget()
        naming_layout = QVBoxLayout(naming)
        name_intro = QLabel("여기 고른 범위는 파일 이름·경로만 등록해 이름으로 찾습니다(내용은 읽지 않음, AI 엔진 없이 동작). "
                            "드라이브 전체도 고를 수 있습니다. Windows·프로그램·ProgramData·앱 데이터(AppData) 폴더, 숨김·시스템 "
                            "파일, node_modules·.venv 같은 개발 폴더, 키·인증서 같은 비밀 파일, 링크·정션은 넣지 않습니다. "
                            "내용 분석은 'AI 분석 폴더'와 이름 결과에서 '내용 분석'으로 고른 파일만 합니다. "
                            "외장 드라이브 전체를 고르면 감시 중에는 안전하게 제거되지 않을 수 있으니 자동 반영을 끄고 빼세요.")
        name_intro.setWordWrap(True)
        naming_layout.addWidget(name_intro)
        naming_layout.addWidget(self.name_list)
        name_buttons = QHBoxLayout()
        for button in (self.add_name_button, self.remove_name_button):
            name_buttons.addWidget(button)
        name_buttons.addStretch(1)
        for button in (self.name_pause_button, self.name_cancel_button, self.name_rescan_button):
            name_buttons.addWidget(button)
        naming_layout.addLayout(name_buttons)
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("등록 방식"))
        self.method_box = QComboBox()
        for key, label in NAME_METHODS.items():
            self.method_box.addItem(label, key)
        self.method_box.setCurrentIndex(max(0, self.method_box.findData(name_method)))
        method_row.addWidget(self.method_box, 1)
        self.walk_check_button = QPushButton("폴더 순회로 전체 확인")
        self.walk_check_button.setToolTip("하드링크의 다른 이름 등 MFT 목록에 없는 항목까지 폴더를 걸어 다시 맞춥니다.")
        method_row.addWidget(self.walk_check_button)
        naming_layout.addLayout(method_row)
        method_note = QLabel("NTFS 드라이브는 볼륨 단위 메타데이터(MFT 목록·변경 기록)를 읽지만, 등록·검색은 고른 범위와 제외 "
                             "규칙 안만 합니다. 파일 내용은 읽지 않습니다. 빠른 NTFS는 '다시 등록'을 누를 때만 관리자 권한을 "
                             "요청하고, 자동 실행 때는 요청하지 않습니다. 네트워크·NTFS가 아닌 드라이브는 폴더 순회로 합니다.")
        method_note.setWordWrap(True)
        naming_layout.addWidget(method_note)
        self.method_state = QLabel("현재 방식: 확인 중")
        self.method_state.setWordWrap(True)
        naming_layout.addWidget(self.method_state)
        naming_layout.addWidget(self.name_state)
        self.tabs.addTab(naming, "이름 검색 범위")
        # 이름 결과에서 하나씩 '내용 분석'에 추가한 파일
        self.extra_list = QListWidget()
        self.extra_list.addItems(extra_files or [])
        self.remove_extra_button = QPushButton("목록에서 빼기")
        chosen = QWidget()
        chosen_layout = QVBoxLayout(chosen)
        chosen_note = QLabel("검색 창의 이름 결과에서 '내용 분석'을 누른 파일입니다. 그 파일만 읽고, 들어 있는 폴더는 분석 범위에 "
                             "넣지 않습니다. 빼면 내용 결과에서 바로 숨깁니다(색인은 보존).")
        chosen_note.setWordWrap(True)
        chosen_layout.addWidget(chosen_note)
        chosen_layout.addWidget(self.extra_list)
        extra_buttons = QHBoxLayout()
        extra_buttons.addWidget(self.remove_extra_button)
        extra_buttons.addStretch(1)
        chosen_layout.addLayout(extra_buttons)
        self.tabs.addTab(chosen, "AI 분석 개별 파일")
        scope_note = QLabel("폴더 제거·제외는 기존 색인을 보존하며 즉시 검색에서 숨깁니다. 다시 추가하거나 제외를 해제하면 "
                            "보존된 결과가 다시 보입니다. 자동 반영이 켜져 있으면 이미 색인한 폴더의 변경을 바로 확인하고, "
                            "처음 고른 폴더는 '색인 시작'으로 읽습니다.")
        scope_note.setWordWrap(True)
        pause_note = QLabel("일시정지: 현재 파일/단계 완료 뒤 같은 작업에서 대기 · 이어하기: 남은 작업 계속 · "
                            "중지: 작업과 색인 엔진 종료, 다음 색인 때 목록을 다시 확인")
        pause_note.setWordWrap(True)
        self.watch_box = QCheckBox("폴더 변경 자동 반영 (파일 추가·수정·삭제·이름 변경 감시)")
        self.watch_box.setChecked(watch)
        self.watch_state = QLabel("")
        self.watch_state.setWordWrap(True)
        watch_row = QHBoxLayout()
        watch_row.addWidget(self.watch_box)
        watch_row.addWidget(self.watch_state, 1)
        # Windows 폴더 감시 핸들이 열려 있으면 상위 폴더의 이동·이름 변경이 거부된다(실측). 감시를 끄면 핸들이 닫힌다.
        self.watch_note = QLabel("감시 중에는 검색 폴더의 상위 폴더를 옮기거나 이름을 바꿀 수 없습니다. "
                                 "그런 작업 전에는 자동 반영을 잠시 끄세요.")
        self.watch_note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(scope_note)
        layout.addWidget(pause_note)
        layout.addLayout(watch_row)
        layout.addWidget(self.watch_note)
        layout.addLayout(actions)
        layout.addWidget(self.progress)
        layout.addWidget(self.state)
        layout.addWidget(self.log, 1)

        self.add_button.clicked.connect(self.add_folder)
        self.remove_button.clicked.connect(self.remove_folder)
        self.start_button.clicked.connect(lambda: self.start_indexing.emit(self.folders()))
        self.stop_button.clicked.connect(self._stop_clicked)
        self.pause_button.clicked.connect(self._pause_clicked)
        self.exclude_button.clicked.connect(self.add_exclusion)
        self.unexclude_button.clicked.connect(self.remove_exclusion)
        self.watch_box.toggled.connect(self.watch_toggled)
        self.add_code_button.clicked.connect(lambda: self.add_code_folder())
        self.remove_code_button.clicked.connect(self.remove_code_folder)
        self.editor_box.currentIndexChanged.connect(lambda _: self.editor_changed.emit(self.editor_box.currentData()))
        self.add_name_button.clicked.connect(lambda: self.add_name_folder())
        self.remove_name_button.clicked.connect(self.remove_name_folder)
        self.remove_extra_button.clicked.connect(self.remove_extra)
        self.name_pause_button.clicked.connect(self._name_pause_clicked)
        self.name_cancel_button.clicked.connect(self.name_cancel)
        self.name_rescan_button.clicked.connect(self.name_rescan)
        self.method_box.currentIndexChanged.connect(lambda _: self.name_method_changed.emit(self.method_box.currentData()))
        self.walk_check_button.clicked.connect(self.name_walk_check)

    def name_folders(self) -> list[str]:
        return [self.name_list.item(i).text() for i in range(self.name_list.count())]

    def add_name_folder(self, path: str | None = None) -> bool:
        """이름 검색 범위: 드라이브 루트·사용자 폴더 전체 허용(이름·경로만), 시스템 폴더·링크·정션은 거부."""
        path = path or QFileDialog.getExistingDirectory(self, "이름으로 찾을 드라이브·폴더 고르기")
        if not path:
            return False
        path = os.path.normpath(path)
        if len(path) == 2 and path[1] == ":":
            path += "\\"
        problem = check_name_folder(path)
        if problem:
            self.warn_folder(problem)
            return False
        if os.path.normcase(path) in {os.path.normcase(f) for f in self.name_folders()}:
            return False
        self.name_list.addItem(path)
        self.name_folders_changed.emit(self.name_folders())
        return True

    def remove_name_folder(self) -> None:
        for item in self.name_list.selectedItems():
            self.name_list.takeItem(self.name_list.row(item))
        self.name_folders_changed.emit(self.name_folders())

    def extra_files(self) -> list[str]:
        return [self.extra_list.item(i).text() for i in range(self.extra_list.count())]

    def set_extra_files(self, files: list[str]) -> None:
        self.extra_list.clear()
        self.extra_list.addItems(files)

    def remove_extra(self) -> None:
        for item in self.extra_list.selectedItems():
            self.extra_list.takeItem(self.extra_list.row(item))
        self.extra_changed.emit(self.extra_files())

    def _name_pause_clicked(self) -> None:
        if self.name_paused:
            self.name_resume.emit()
        else:
            self.name_pause.emit()

    def names_paused(self, paused: bool) -> None:
        self.name_paused = paused
        self.name_pause_button.setText("이름 등록 이어하기" if paused else "이름 등록 일시정지")
        if paused:
            self.name_state.setText("이름 목록: 일시정지 — 이미 등록한 이름은 검색됩니다. '이름 등록 이어하기'로 계속")

    def show_method_state(self, state: dict) -> None:
        """NameService.method_state: 방식과 볼륨별 상태(관리자 권한 사용 여부, 대체 방식의 이유)."""
        method = state.get("method", "auto")
        lines = [f"현재 방식: {NAME_METHODS.get(method, method)}"]
        if method == "walk":
            lines.append("모든 범위를 폴더 순회와 폴더 감시로 맞춥니다.")
        for root, info in sorted(state.get("roots", {}).items()):
            if info["method"] == "usn":
                by = {"mft": "MFT 목록(관리자 도우미)", "walk": "폴더 순회"}.get(info["registered_by"], "이전 등록")
                text = f"{root}: 변경 기록(USN)으로 갱신 · 등록: {by}"
            else:
                text = f"{root}: 폴더 순회" + (f" ({info['detail']})" if info["detail"] else "")
                info = dict(info, detail="")
            if info.get("detail"):
                text += f" · {info['detail']}"
            if info.get("unresolved"):
                # 범위 안의 열 수 없는 폴더에서 온 변경만 (범위 밖 기록은 세어 두기만 하고 여기 보이지 않는다)
                text += f" · 열 수 없는 폴더의 변경 {info['unresolved']}건은 확인하지 못함(그 폴더는 '상태 미확인')"
            lines.append(text)
        lines.append("권한: 앱은 일반 사용자 권한으로 실행 중" + (" · MFT 등록 때만 관리자 도우미 사용" if method == "ntfs" else ""))
        self.method_state.setText("\n".join(lines))

    def names_progress(self, added: int, folder: str) -> None:
        if not self.name_paused:
            self.name_state.setText(f"이름 목록: 등록 중 — 새 이름 {added:,}개 · {folder}")

    def names_finished(self, summary: dict) -> None:
        head = "중지함(다음 '다시 등록'·앱 시작 때 이어서 맞춤)" if summary.get("stopped") else "완료"
        self.name_state.setText(f"이름 목록: {head} — 파일 {summary.get('files', 0):,}개, 폴더 {summary.get('folders', 0):,}개 · "
                                f"{summary.get('seconds', 0):.1f}초"
                                + (f" · 읽지 못한 경로 {summary['trouble']}개(지우지 않음)" if summary.get("trouble") else ""))

    def folders(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def code_folders(self) -> list[str]:
        return [self.code_list.item(i).text() for i in range(self.code_list.count())]

    def add_code_folder(self, path: str | None = None) -> bool:
        """코드 폴더: 검색 폴더와 같은 검사(드라이브 전체·사용자 폴더 전체·시스템 폴더·링크·정션 거부)."""
        path = path or QFileDialog.getExistingDirectory(self, "코드 폴더 고르기")
        if not path:
            return False
        path = os.path.normpath(path)
        problem = check_folder(path)
        if problem:
            self.warn_folder(problem)
            return False
        if os.path.normcase(path) in {os.path.normcase(f) for f in self.code_folders()}:
            return False
        self.code_list.addItem(path)
        self.code_changed.emit(self.code_folders())
        return True

    def remove_code_folder(self) -> None:
        for item in self.code_list.selectedItems():
            self.code_list.takeItem(self.code_list.row(item))
        self.code_changed.emit(self.code_folders())

    def exclusions(self) -> list[str]:
        return [self.excluded_list.item(i).text() for i in range(self.excluded_list.count())]

    def add_exclusion(self, path: str | None = None) -> bool:
        path = path or QFileDialog.getExistingDirectory(self, "제외할 폴더 고르기")
        if not path:
            return False
        path = os.path.normpath(path)
        problem = check_folder(path)
        inside = scope.excluded(path, self.folders() + self.code_folders() + self.name_folders())
        if problem and scope.excluded(path, self.name_folders()):
            problem = check_name_folder(path)   # 드라이브 이름 범위 안의 사용자 폴더 등은 이름 검색에서 뺄 수 있다
        if problem or not inside:
            self.warn_folder(problem or "AI 분석 폴더·코드 폴더·이름 검색 범위로 고른 폴더의 하위 폴더만 제외할 수 있습니다.")
            return False
        if any(os.path.normcase(path) == os.path.normcase(p) for p in self.exclusions()):
            return False
        self.excluded_list.addItem(path)
        self.exclusions_changed.emit(self.exclusions())
        return True

    def remove_exclusion(self) -> None:
        for item in self.excluded_list.selectedItems():
            self.excluded_list.takeItem(self.excluded_list.row(item))
        self.exclusions_changed.emit(self.exclusions())

    def add_folder(self, path: str | None = None) -> bool:
        path = path or QFileDialog.getExistingDirectory(self, "검색할 폴더 고르기")
        if not path:
            return False
        path = os.path.normpath(path)
        problem = check_folder(path)
        if problem:
            self.warn_folder(problem)
            return False
        if os.path.normcase(path) in {os.path.normcase(f) for f in self.folders()}:
            return False
        self.list.addItem(path)
        self.changed.emit(self.folders())
        return True

    def warn_folder(self, problem: str) -> None:
        QMessageBox.warning(self, "이 폴더는 고를 수 없습니다", problem)

    def remove_folder(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed.emit(self.folders())

    def _stop_clicked(self) -> None:
        self.stop_requested = True
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.state.setText("지금 파일이 끝나면 멈춥니다…")
        self.stop_indexing.emit()

    def _pause_clicked(self) -> None:
        self.pause_button.setEnabled(False)
        if self.paused:
            self.resume_indexing.emit()
        else:
            self.state.setText("현재 파일/단계가 끝나면 일시정지합니다…")
            self.pause_indexing.emit()

    def indexing_paused(self, paused: bool) -> None:
        self.paused = paused
        self.pause_button.setText("이어하기" if paused else "일시정지")
        if not self.stop_requested:
            self.pause_button.setEnabled(True)
            self.state.setText("일시정지 중 — 이어하기를 누르면 남은 작업을 계속합니다." if paused else "이어서 색인합니다…")

    def indexing_started(self, auto: bool = False) -> None:
        """auto: 감시가 시작한 자동 갱신. 같은 일시정지·중지 버튼을 쓰고, 그동안 폴더 범위 편집은 막는다."""
        self.stop_requested = False
        self.paused = False
        self.auto = auto
        self._before_auto = self.state.text()
        if not auto:
            self.log.clear()
        self.progress.setRange(0, 0)       # 전체 수를 알기 전까지는 움직이는 막대
        self.state.setText("자동 반영: 바뀐 파일을 확인하는 중…" if auto else "색인을 시작합니다…")
        for widget in (self.start_button, self.add_button, self.remove_button, self.exclude_button, self.unexclude_button,
                       self.add_code_button, self.remove_code_button):
            widget.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.pause_button.setEnabled(True)

    def indexing_progress(self, done: int, total: int, path: str, result: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)
        if self.pause_button.isEnabled() and not self.stop_requested:
            self.state.setText(f"{done}/{total}  {Path(path).name}")
        if result.startswith(("failed", "retry")):
            self.log.appendPlainText(f"문제: {Path(path).name} — {result}")

    def indexing_status(self, message: str) -> None:
        if self.pause_button.isEnabled() and not self.stop_requested:
            self.state.setText(message)

    def indexing_finished(self, summary: dict) -> None:
        self._idle()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        if summary.get("auto"):
            self._auto_finished(summary)
            return
        head = "색인을 중지했습니다. 남은 파일은 다음 색인 때 이어집니다." if summary.get("stopped") else "색인을 마쳤습니다."
        self.state.setText(f"{head}  새 파일 {summary['added']}, 바뀐 파일 {summary['changed']}, 지운 파일 "
                           f"{summary['removed']}, 그대로 {summary['unchanged']} · 읽음 {summary['done']}, 건너뜀 "
                           f"{summary['skipped']}, 실패 {summary['failed']}"
                           + (f" (코드 파일 {summary['code_done']})" if "code_done" in summary else ""))
        for line in summary.get("errors", [])[:50]:
            self.log.appendPlainText(f"실패: {line}")

    def _auto_finished(self, summary: dict) -> None:
        if summary.get("stopped"):
            self.state.setText("자동 반영을 중지했습니다. '색인 시작'을 누르면 놓친 변경까지 확인합니다.")
        elif summary["added"] or summary["changed"] or summary["removed"] or summary["failed"]:
            self.state.setText(f"자동 반영 완료: 새 파일 {summary['added']}, 바뀐 파일 {summary['changed']}, 지운 파일 "
                               f"{summary['removed']}" + (f" · 실패 {summary['failed']}" if summary["failed"] else ""))
        else:
            self.state.setText(self._before_auto)   # 바뀐 것이 없는 확인: 이전 안내를 그대로 둔다
        for root in summary.get("unavailable", []):
            self.log.appendPlainText(f"열 수 없는 폴더(색인은 그대로 둠): {root}")
        for line in summary.get("errors", [])[:50]:
            self.log.appendPlainText(f"실패: {line}")

    def show_watch_state(self, text: str) -> None:
        self.watch_state.setText(text)

    def indexing_failed(self, message: str) -> None:
        self._idle()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.state.setText(message)
        self.log.appendPlainText(f"오류: {message}")

    def _idle(self) -> None:
        for widget in (self.start_button, self.add_button, self.remove_button, self.exclude_button, self.unexclude_button,
                       self.add_code_button, self.remove_code_button):
            widget.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.pause_button.setText("일시정지")
        self.pause_button.setEnabled(False)
        self.paused = False
        self.auto = False


# 설정창 (2026-10-14): 일반·화면은 적용·확인·취소, 검색 범위는 위 FoldersDialog를 그대로(즉시 저장), 색인 현황은 읽기만


GENERAL_DEFAULTS = {"hotkey": hotkey.DEFAULT, "launch_at_login": False, "start_in_tray": False}
DISPLAY_DEFAULTS = {"theme": "system", "large_view": "grid", "reset_window": False}
FIELD_LABELS = {"hotkey": "검색 창 단축키", "launch_at_login": "Windows 로그인 시 실행", "start_in_tray": "앱 시작 시",
                "theme": "테마", "large_view": "큰 검색창 기본 보기", "reset_window": "검색창 크기·위치"}


def field_text(key: str, value) -> str:
    if key == "launch_at_login":
        return "켬" if value else "끔"
    if key == "start_in_tray":
        return "트레이에서 시작" if value else "검색 창 표시"
    if key == "theme":
        return THEMES.get(value, value)
    if key == "large_view":
        return VIEWS.get(value, value)
    if key == "reset_window":
        return "기본 크기·화면 가운데로 초기화" if value else "저장한 크기·위치 유지"
    return str(value)


def describe_changes(before: dict, after: dict) -> list[str]:
    """기본값 복원 전에 보여 줄 변경 목록(일반·화면 항목만)."""
    return [f"{FIELD_LABELS[k]}: {field_text(k, before.get(k))} → {field_text(k, after[k])}"
            for k in FIELD_LABELS if k in after and before.get(k) != after[k]]


class SettingsDialog(QDialog):
    """apply(values) → 오류 문구 또는 None. 실패하면 App이 이전 설정·단축키·시작 항목을 되돌리고, 여기의 입력은 남겨 고칠 수 있게
    한다. 취소는 적용하지 않은 입력만 버린다(이미 적용한 설정은 되돌리지 않는다)."""

    TABS = ("general", "scope", "display", "status")

    def __init__(self, folders: QWidget, current, apply, startup: prefs.Startup, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NoseOut 설정")
        self.setWindowIcon(app_icon())
        self.resize(820, 680)
        self.current = current          # () → 지금 적용된 Settings
        self.apply = apply
        self.startup = startup
        self.warn = lambda title, text: QMessageBox.warning(self, title, text)
        self.confirm = lambda title, text: QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes
        self.refresh_requested = None   # App이 정한다: 색인 현황 다시 세기
        self.open_folder = opener.open_folder
        self.base: Path | None = None
        self._loading = False

        # 일반
        general = QWidget()
        form = QFormLayout(general)
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_default = QPushButton("기본값")
        row = QHBoxLayout()
        row.addWidget(self.hotkey_edit, 1)
        row.addWidget(self.hotkey_default)
        form.addRow("검색 창 단축키", row)
        self.hotkey_note = QLabel()
        self.hotkey_note.setWordWrap(True)
        form.addRow("", self.hotkey_note)
        self.launch_box = QCheckBox("Windows에 로그인하면 NoseOut 실행 (현재 사용자 계정만)")
        form.addRow("시작 프로그램", self.launch_box)
        self.launch_note = QLabel()
        self.launch_note.setWordWrap(True)
        form.addRow("", self.launch_note)
        self.start_mode = QComboBox()
        self.start_mode.addItem("검색 창 표시", False)
        self.start_mode.addItem("트레이에서 시작(창 숨김)", True)
        form.addRow("앱 시작 시", self.start_mode)

        # 화면
        display = QWidget()
        display_form = QFormLayout(display)
        self.theme_box = QComboBox()
        for key, label in THEMES.items():
            self.theme_box.addItem(label, key)
        display_form.addRow("테마", self.theme_box)
        self.view_box = QComboBox()
        for key, label in VIEWS.items():
            self.view_box.addItem(label, key)
        display_form.addRow("큰 검색창 기본 보기", self.view_box)
        self.reset_window_box = QCheckBox("적용할 때 검색창 크기·위치를 기본으로 초기화")
        display_form.addRow("검색창 크기·위치", self.reset_window_box)
        window_note = QLabel("작은·큰 검색창의 크기와 위치는 창을 닫을 때 저장되고 다음 실행 때 복원됩니다. 모니터 구성이 바뀌어 "
                             "저장한 자리가 화면 밖이면 보이는 곳으로 옮깁니다. 작은 검색창은 늘 파일 목록입니다.")
        window_note.setWordWrap(True)
        display_form.addRow("", window_note)

        # 색인 현황 (읽기만, 다른 스레드에서 센다)
        status = QWidget()
        status_layout = QVBoxLayout(status)
        self.stats_state = QLabel("확인 중…")
        self.stats_labels: dict[str, QLabel] = {}
        stats_form = QFormLayout()
        for key, label in (("names", "이름 등록"), ("ai", "내용(AI) 분석"), ("code", "코드 색인"), ("updated", "마지막 갱신"),
                           ("storage", "색인 저장 공간")):
            value = QLabel("확인 중…")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.stats_labels[key] = value
            stats_form.addRow(label, value)
        status_layout.addWidget(self.stats_state)
        status_layout.addLayout(stats_form)
        status_layout.addWidget(QLabel("분석 실패 파일 (고르면 이유가 보입니다)"))
        self.failures = QListWidget()
        self.failure_reason = QLabel()
        self.failure_reason.setWordWrap(True)
        self.failure_reason.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_layout.addWidget(self.failures, 1)
        status_layout.addWidget(self.failure_reason)
        status_buttons = QHBoxLayout()
        self.refresh_button = QPushButton("새로 고침")
        self.open_settings_button = QPushButton("설정 폴더 열기")
        self.open_logs_button = QPushButton("로그 폴더 열기")
        for button in (self.refresh_button, self.open_settings_button, self.open_logs_button):
            status_buttons.addWidget(button)
        status_buttons.addStretch(1)
        status_layout.addLayout(status_buttons)

        self.tabs = QTabWidget()
        self.tabs.addTab(general, "일반")
        self.tabs.addTab(folders, "검색 범위")
        self.tabs.addTab(display, "화면")
        self.tabs.addTab(status, "색인 현황")

        self.defaults_button = QPushButton("기본값 복원…")
        self.defaults_button.setToolTip("일반·화면 설정만 기본값으로 돌립니다. 검색 범위·제외·개별 분석 파일·색인은 그대로입니다.")
        self.ok_button = QPushButton("확인")
        self.cancel_button = QPushButton("취소")
        self.apply_button = QPushButton("적용")
        self.scope_note = QLabel("적용·취소·기본값 복원은 '일반'과 '화면'에만 해당합니다.")
        self.scope_note.setEnabled(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.defaults_button)
        bottom.addWidget(self.scope_note, 1)
        for button in (self.ok_button, self.cancel_button, self.apply_button):
            bottom.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(bottom)

        self.hotkey_default.clicked.connect(
            lambda: self.hotkey_edit.setKeySequence(QKeySequence(self._qt_sequence(hotkey.DEFAULT))))
        for signal in (self.hotkey_edit.keySequenceChanged, self.launch_box.toggled, self.start_mode.currentIndexChanged,
                       self.theme_box.currentIndexChanged, self.view_box.currentIndexChanged,
                       self.reset_window_box.toggled):
            signal.connect(self._edited)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self.apply_now)
        self.defaults_button.clicked.connect(self.restore_defaults)
        self.refresh_button.clicked.connect(lambda: self.refresh_requested and self.refresh_requested())
        self.open_settings_button.clicked.connect(lambda: self.base and self.open_folder(str(self.base)))
        self.open_logs_button.clicked.connect(lambda: self.base and self.open_folder(str(self.base)))
        self.failures.currentRowChanged.connect(self._failure_selected)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._failure_rows: list[tuple[str, str]] = []
        self.load(self.current())

    # 일반·화면: 입력 ↔ 값

    @staticmethod
    def _qt_sequence(text: str) -> str:
        return "+".join("Meta" if p.strip().lower() == "win" else p.strip() for p in text.split("+"))

    def load(self, settings: Settings) -> None:
        """적용된 설정으로 입력을 되돌린다(열 때, 취소, 적용 성공 뒤)."""
        self._loading = True
        self.hotkey_edit.setKeySequence(QKeySequence(self._qt_sequence(settings.hotkey)))
        self.launch_box.setChecked(settings.launch_at_login)
        self.start_mode.setCurrentIndex(1 if settings.start_in_tray else 0)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(settings.theme)))
        self.view_box.setCurrentIndex(max(0, self.view_box.findData(settings.large_view)))
        self.reset_window_box.setChecked(False)
        self._loading = False
        self._show_startup_state(settings)
        self._edited()

    def values(self) -> dict:
        sequence = self.hotkey_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        return {"hotkey": prefs.hotkey_from_sequence(sequence), "launch_at_login": self.launch_box.isChecked(),
                "start_in_tray": bool(self.start_mode.currentData()), "theme": self.theme_box.currentData(),
                "large_view": self.view_box.currentData(), "reset_window": self.reset_window_box.isChecked()}

    def applied_values(self) -> dict:
        settings = self.current()
        return {"hotkey": prefs.normalize_hotkey(settings.hotkey), "launch_at_login": settings.launch_at_login,
                "start_in_tray": settings.start_in_tray, "theme": settings.theme, "large_view": settings.large_view,
                "reset_window": False}

    def pending(self) -> bool:
        return self.values() != self.applied_values()

    def _edited(self, *_args) -> None:
        if self._loading:
            return
        problem = prefs.hotkey_problem(self.values()["hotkey"])
        self.hotkey_note.setText(problem or "Ctrl·Alt·Win 중 하나 이상과 글자·숫자·F1~F12·Space 등을 함께 누르세요. "
                                 "다른 프로그램이 쓰는 조합이면 적용할 때 알려 주고 기존 단축키를 유지합니다.")
        self.apply_button.setEnabled(self.pending())

    def _show_startup_state(self, settings: Settings) -> None:
        state = self.startup.state()
        if not self.startup.supported:
            note = "소스(python)로 실행 중이라 지원하지 않습니다. NoseOut.exe로 실행했을 때만 켤 수 있습니다."
            if settings.launch_at_login:
                note += " (끄기는 할 수 있습니다)"
            self.launch_box.setEnabled(settings.launch_at_login)
        else:
            note = "켜면 현재 EXE 위치로 사용자 계정의 시작 항목에 등록하고, 끄면 이 앱이 만든 항목만 지웁니다."
            self.launch_box.setEnabled(True)
        if state == "other":
            note += " 시작 항목에 이 앱이 만들지 않은 같은 이름의 항목이 있어 바꾸지 않습니다."
        elif settings.launch_at_login and state == "off":
            note += " 지금 시작 항목이 없습니다(직접 지움 등). 껐다가 다시 켜면 등록합니다."
        elif settings.launch_at_login and state == "moved":
            note += " 등록된 경로가 지금 EXE와 다릅니다."
        if settings.launch_at_login and self.startup.disabled_by_windows():
            note += " Windows 설정의 '시작 앱'에서 꺼져 있어 로그인 때 실행되지 않습니다."
        self.launch_note.setText(note)

    def apply_now(self) -> bool:
        if not self.pending():
            return True
        error = self.apply(self.values())
        if error:
            self.warn("설정을 적용하지 못했습니다", error)
            self.hotkey_note.setText(error)
            return False
        self.load(self.current())
        return True

    def accept(self) -> None:
        if self.apply_now():
            super().accept()

    def reject(self) -> None:
        self.load(self.current())     # 적용하지 않은 입력만 버린다
        super().reject()

    def restore_defaults(self) -> bool:
        """일반·화면 설정만. 바뀔 내용을 보여 주고 동의하면 입력에 채운다(적용·확인을 눌러야 반영)."""
        before = self.values()
        settings = self.current()
        after = dict(GENERAL_DEFAULTS, **DISPLAY_DEFAULTS)
        after["reset_window"] = bool(settings.window_small or settings.window_large)
        changes = describe_changes(before, after)
        if not changes:
            self.warn("기본값 복원", "일반·화면 설정이 이미 기본값입니다.")
            return False
        text = ("다음 항목을 기본값으로 바꿉니다:\n\n" + "\n".join(f"• {line}" for line in changes)
                + "\n\n검색 범위·제외 규칙·개별 분석 파일·색인은 바꾸지 않습니다. '적용' 또는 '확인'을 눌러야 반영됩니다.")
        if not self.confirm("기본값 복원", text):
            return False
        self.hotkey_edit.setKeySequence(QKeySequence(self._qt_sequence(after["hotkey"])))
        self.launch_box.setChecked(after["launch_at_login"])
        self.start_mode.setCurrentIndex(1 if after["start_in_tray"] else 0)
        self.theme_box.setCurrentIndex(self.theme_box.findData(after["theme"]))
        self.view_box.setCurrentIndex(self.view_box.findData(after["large_view"]))
        self.reset_window_box.setChecked(after["reset_window"])
        return True

    # 색인 현황

    def _tab_changed(self, index: int) -> None:
        if self.TABS[index] == "status" and self.refresh_requested:
            self.refresh_requested()

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(self.TABS.index(name))

    def stats_pending(self) -> None:
        self.stats_state.setText("확인 중… (색인을 읽기만 합니다)")

    def show_stats(self, data: dict, jobs: dict) -> None:
        """data: stats.collect 결과(아직 없으면 빈 dict), jobs: App._job_texts (이름 등록·분석 작업 상태)."""
        if not data:
            self.stats_state.setText("확인 중…")
            return
        names, ai, code = data.get("names", {}), data.get("ai", {}), data.get("code", {})
        self.stats_state.setText(f"{time.strftime('%H:%M:%S')} 기준 · 세는 데 {data.get('seconds', 0):.2f}초")
        if names.get("error"):
            text = f"읽지 못함: {names['error']}"
        elif names.get("busy"):
            text = "확인 중 (만드는 중)"
        elif not names.get("exists"):
            text = "아직 없음"
        else:
            text = f"파일 {names['files']:,}개 · 폴더 {names['folders']:,}개" + (
                f" · 읽지 못한 경로 {names['trouble']:,}개" if names.get("trouble") else "")
        self.stats_labels["names"].setText(f"{text}\n상태: {jobs.get('names', '대기')}")
        if ai.get("error"):
            text = f"읽지 못함: {ai['error']}"
        elif ai.get("busy"):
            text = "확인 중 (만드는 중)"
        elif not ai.get("exists"):
            text = "아직 없음"
        else:
            text = (f"완료 {ai['done']:,} · 일부만 읽음 {ai['partial']:,} · 대기 {ai['pending']:,} · "
                    f"실패 {ai['failed']:,} · 건너뜀 {ai['skipped']:,}")
        self.stats_labels["ai"].setText(f"{text}\n상태: {jobs.get('ai', '대기')} · 검색 가능 {jobs.get('searchable', '확인 중')}"
                                        f" · 남은 분석 {jobs.get('remaining', '확인 중')}")
        if code.get("error"):
            self.stats_labels["code"].setText(f"읽지 못함: {code['error']}")
        else:
            self.stats_labels["code"].setText(f"파일 {code.get('done', 0):,}개" if code.get("exists") else "아직 없음")
        self.stats_labels["updated"].setText(
            f"이름 목록 {stats.time_text(names.get('checked_at'))} · 내용 분석 {stats.time_text(ai.get('last'))} · "
            f"코드 {stats.time_text(code.get('last'))}")
        storage = data.get("storage", {})
        self.stats_labels["storage"].setText(
            f"합계 {stats.size_text(storage.get('total', 0))} (내용 {stats.size_text(storage.get('index', 0))}, 코드 "
            f"{stats.size_text(storage.get('code', 0))}, 이름 {stats.size_text(storage.get('names', 0))}, 로그 "
            f"{stats.size_text(storage.get('logs', 0))})")
        self._failure_rows = list(ai.get("failures", []))
        self.failures.clear()
        for path, _reason in self._failure_rows:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            self.failures.addItem(item)
        self.failure_reason.setText("" if self._failure_rows else "실패한 파일이 없습니다.")

    def _failure_selected(self, row: int) -> None:
        if 0 <= row < len(self._failure_rows):
            path, reason = self._failure_rows[row]
            self.failure_reason.setText(f"{path}\n이유: {reason or '기록 없음'}")


# 앱


class App(QObject):
    """트레이, 단축키, 두 서비스 스레드를 묶는다. 종료할 때 진행 중인 색인과 엔진을 정리한다."""

    run_index = Signal(list)
    search_reload = Signal()
    search_request = Signal(int, str, object)
    search_scope = Signal(list, list)
    run_changes = Signal(list, list, list)
    release_engine = Signal()
    search_code_scope = Signal(list)
    search_refresh = Signal()                    # AI 색인 중 끝낸 파일을 의미 검색에 반영
    name_scan = Signal(list, list, list, list)   # 이름 목록: 검색 폴더·제외·코드 폴더·이름 범위 전체 맞추기
    search_extra = Signal(list)                  # '내용 분석'으로 고른 파일 (검색 범위)
    run_files = Signal(list)                     # 그 파일만 분석
    name_apply = Signal(list, list)              # 이름 목록: 감시가 알려 준 변경만
    name_request = Signal(int, str, object)
    name_scope = Signal(list, list)
    stats_request = Signal(object)               # 색인 현황 세기 (StatsService 스레드)
    name_scan_with = Signal(list, list, list, list, object)   # 이름 목록 맞추기 + 방식·UAC 허용·다시 등록
    name_poll = Signal()                         # USN 변경 기록 읽기 (ntfs.py)

    def __init__(self, qt_app: QApplication, base: Path, search_config=None, index_factory=None,
                 register_hotkey: bool = True, setup: bool = True, hotkey_backend=None, startup=None,
                 name_backend: dict | None = None):
        """hotkey_backend: (RegisterHotKey, UnregisterHotKey) 대체(테스트). startup: prefs.Startup(테스트는 MemoryRun)."""
        super().__init__()
        self.qt = qt_app
        self.base = base
        self.settings_path = base / "settings.json"
        self.settings = Settings.load(self.settings_path)
        self.db = base / "index.sqlite"
        self.code_db = base / "code.sqlite"   # 코드 색인: 일반 색인과 같은 설정 폴더의 별도 파일
        self.startup = startup if startup is not None else prefs.Startup()
        apply_theme(self.qt, self.settings.theme)

        self.window = SearchWindow()
        self.window.large_view = self.settings.large_view
        self.window.geometries = {"small": list(self.settings.window_small), "large": list(self.settings.window_large)}
        self.window.apply_geometry()
        self.window.hidden.connect(self._save_window)
        first_run = not self.settings_path.exists()
        # 검색 범위 화면은 설정창의 '검색 범위' 탭에 들어간다(따로 뜨던 '검색 폴더' 창과 같은 화면, 같은 저장).
        self.folders = FoldersDialog(self.settings.folders, exclusions=self.settings.exclusions,
                                     watch=self.settings.watch, code_folders=self.settings.code_folders,
                                     code_editor=self.settings.code_editor, name_folders=self.settings.name_folders,
                                     extra_files=self.settings.extra_files, name_method=self.settings.name_method)
        self.preferences = SettingsDialog(self.folders, lambda: self.settings, self.apply_preferences, self.startup,
                                          self.window)
        self.preferences.base = base
        self.preferences.refresh_requested = self.refresh_stats
        self.folders.name_folders_changed.connect(self._save_name_folders)
        self.folders.extra_changed.connect(self._save_extra)
        self.setup = SetupDialog(setup_candidates(), os.environ.get("SystemDrive", "C:") + "\\", self.window) \
            if first_run and setup else None
        if self.setup is not None:
            self.setup.accepted.connect(self._setup_accepted)
            self.setup.rejected.connect(lambda: self._persist())   # 다시 묻지 않는다
        self.window.analyzable = self._analyzable
        self.window.analyze.connect(self.analyze_file)
        self.window.code_editor = self.settings.code_editor
        self.folders.code_changed.connect(self._save_code_folders)
        self.folders.editor_changed.connect(self._save_editor)
        self.window.open_folders.connect(self.show_folders)
        self.window.open_settings.connect(self.show_settings)
        # 작업 상태: 이름 등록과 내용(AI) 분석을 따로 (검색 창 작업 줄, 설정 > 색인 현황, 결과 없음 안내)
        self.name_job = {"state": "idle", "added": 0}
        self.ai_job = {"state": "idle", "done": 0, "total": 0, "planned": False}
        self.last_stats: dict = {}
        self.folders.changed.connect(self._save_folders)
        self.folders.exclusions_changed.connect(self._save_exclusions)
        self.index_active = False
        self.name_method_state: dict = {}

        # 검색 서비스 스레드
        self.search_thread = QThread()
        self.search = SearchService(self.db, search_config, self.settings.folders, self.settings.exclusions,
                                    code_db=self.code_db, code_roots=self.settings.code_folders)
        self.search.moveToThread(self.search_thread)
        self.search_thread.started.connect(self.search.start)
        self.window.query.connect(self._query)
        self.search.results.connect(self.window.show_results)
        self.search.failed.connect(self.window.show_error)
        self.search.ready.connect(self.window.engine_is_ready)
        self.search_reload.connect(self.search.reload)
        self.search_request.connect(self.search.search)     # 다른 스레드라 큐로 전달된다
        self.search_scope.connect(self.search.set_scope)
        self.search_code_scope.connect(self.search.set_code_scope)
        self.search_refresh.connect(self.search.refresh)
        self.search_extra.connect(self.search.set_extra)
        self.search.extra_files = {os.path.normcase(os.path.abspath(f)) for f in self.settings.extra_files}
        self.search.refreshed.connect(self._search_refreshed)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self.search_refresh)

        # 빠른 이름 검색: 이름 목록(names.sqlite)을 쓰는 스레드와 읽는 스레드. 엔진·AI 색인 스레드와 따로 돈다.
        self.names_db = base / "names.sqlite"
        self.name_thread = QThread()
        self.names = NameService(self.names_db, **(name_backend or {}))
        self.names.moveToThread(self.name_thread)
        self.name_scan.connect(self.names.scan)
        self.name_scan_with.connect(self.names.scan_with)
        self.name_poll.connect(self.names.poll)
        self.names.method_state.connect(self._name_method_state)
        self.names.stage.connect(self._name_stage)
        self.folders.name_method_changed.connect(self._save_name_method)
        self.folders.name_walk_check.connect(lambda: self._scan_names(force=True, method="auto"))
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self._poll_names)
        self.name_apply.connect(self.names.apply)
        self.names.changed.connect(self._names_changed)
        self.names.finished.connect(self._names_finished)
        self.names.failed.connect(self._names_failed)
        self.names.progress.connect(self.folders.names_progress)
        self.names.finished.connect(self.folders.names_finished)
        self.names.paused_changed.connect(self.folders.names_paused)
        self.names.progress.connect(self._name_progress)
        self.names.paused_changed.connect(self._name_paused)
        # 일시정지·중지는 이름 스레드가 걷는 중에도 바로 닿아야 한다(Event·Condition만 바꾸는 함수)
        self.folders.name_pause.connect(self.names.request_pause, Qt.ConnectionType.DirectConnection)
        self.folders.name_resume.connect(self.names.resume, Qt.ConnectionType.DirectConnection)
        self.folders.name_cancel.connect(self.names.request_cancel, Qt.ConnectionType.DirectConnection)
        self.folders.name_rescan.connect(lambda: self._scan_names(force=True, prompt=self.settings.name_method == "ntfs"))
        self.name_search_thread = QThread()
        self.name_search = NameSearchService(self.names_db, self._named(), self.settings.exclusions)
        self.name_search.moveToThread(self.name_search_thread)
        self.window.name_query.connect(self._name_query)
        self.name_request.connect(self.name_search.search)
        self.name_search.results.connect(self.window.show_name_results)
        self.name_scope.connect(self.name_search.set_scope)
        self._names_timer = QTimer(self)   # 등록 중 커밋마다 다시 찾지 않고 0.3초에 한 번
        self._names_timer.setSingleShot(True)
        self._names_timer.setInterval(300)
        self._names_timer.timeout.connect(self.window.refresh_names)

        # 색인 서비스 스레드
        self.index_thread = QThread()
        self.indexing = IndexService(self.db, index_factory, code_db=self.code_db)
        self.indexing.moveToThread(self.index_thread)
        self.run_index.connect(self.indexing.run)
        self.folders.start_indexing.connect(self.start_indexing)
        # 색인 run이 스레드를 차지하므로 큐에 넣으면 끝난 뒤에야 중지된다. Event만 바꾸는 함수라 즉시 호출해도 안전하다.
        self.folders.stop_indexing.connect(self.indexing.request_stop, Qt.ConnectionType.DirectConnection)
        self.folders.pause_indexing.connect(self.indexing.request_pause, Qt.ConnectionType.DirectConnection)
        self.folders.resume_indexing.connect(self.indexing.resume, Qt.ConnectionType.DirectConnection)
        self.indexing.paused_changed.connect(self._index_paused)
        self.indexing.planned.connect(self._index_planned)
        self.indexing.progress.connect(self.folders.indexing_progress)
        self.indexing.progress.connect(self._index_progress)
        self.indexing.status.connect(self.folders.indexing_status)
        self.indexing.finished.connect(self._index_finished)
        self.indexing.failed.connect(self._index_failed)
        self.run_changes.connect(self.indexing.run_changes)
        self.run_files.connect(self.indexing.run_files)
        self.indexing.extra_files = list(self.settings.extra_files)
        self.release_engine.connect(self.indexing.release_engine)

        # 폴더 감시 (6단계): 이벤트를 화면 스레드에서 모아, 색인 스레드가 쉬고 있을 때 같은 큐로 넘긴다.
        # 설정 폴더(색인·로그·임시 파일)의 변경은 듣지 않는다: 자기 쓰기로 감시가 다시 돌지 않게.
        # 드라이브 전체를 이름 범위로 감시하면 앱 데이터·캐시 쓰기 알림이 쏟아진다: 시스템 폴더는 이벤트 단계에서 버린다
        # (이름 목록에도 넣지 않는 폴더라 결과는 같다).
        ignore = [base] + [f for f in rules._system_folders() if os.path.isdir(f)]
        self.watch = WatchService(self._watched(), self.settings.exclusions, ignore=ignore,
                                  enabled=self.settings.watch, busy=lambda: self.index_active,
                                  index=[self.db, self.code_db], name_roots=self.settings.name_folders,
                                  extra_files=self.settings.extra_files)
        self.watch.dispatch.connect(self._auto_index)
        self.watch.names_changed.connect(self._names_watch)
        self.watch.state_changed.connect(self._watch_state)
        self.watch.engine_idle.connect(self.release_engine)
        self.folders.watch_toggled.connect(self._toggle_watch)
        self.folders.stop_indexing.connect(self.watch.hold)

        # 색인 현황: 다른 스레드에서 읽기만 한다. 세는 중에 다시 요청되면 끝난 뒤 한 번 더 센다.
        self.stats_thread = QThread()
        self.stats_service = StatsService()
        self.stats_service.moveToThread(self.stats_thread)
        self.stats_request.connect(self.stats_service.collect)
        self.stats_service.ready.connect(self._stats_ready)
        self._stats_busy = False
        self._stats_again = False
        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(STATS_MS)
        self._stats_timer.timeout.connect(self.refresh_stats)

        # 트레이
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("NoseOut 검색")
        menu = QMenu()
        self.action_open = QAction("검색 창 열기", menu)
        self.action_folders = QAction("검색 폴더…", menu)
        self.action_settings = QAction("설정…", menu)
        self.action_large = QAction("큰 검색창 열기", menu)
        self.action_quit = QAction("종료", menu)
        self.action_open.triggered.connect(self.window.summon)
        self.action_folders.triggered.connect(self.show_folders)
        self.action_settings.triggered.connect(self.show_settings)
        self.action_large.triggered.connect(self.show_large)
        self.action_quit.triggered.connect(self.quit)
        menu.addAction(self.action_open)
        menu.addAction(self.action_large)
        menu.addAction(self.action_folders)
        menu.addAction(self.action_settings)
        menu.addSeparator()
        menu.addAction(self.action_quit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        # Windows 11의 숨김 트레이에 접근하기 어려울 때도 같은 메뉴로 종료할 수 있다.
        self.window.menu_button.setMenu(menu)
        self.tray.activated.connect(self._tray_activated)

        # 전역 단축키 (창의 핸들로 등록)
        self.hotkey = hotkey.GlobalHotkey(int(self.window.winId()), self.settings.hotkey, *(hotkey_backend or ()))
        self.hotkey.pressed.connect(self.window.summon)
        self.qt.installNativeEventFilter(self.hotkey)
        # 실제로 등록하는가: 끈 테스트는 바꾼 단축키도 시스템에 등록하지 않는다(대체 구현을 주면 그것으로 등록)
        self.hotkey_live = register_hotkey or hotkey_backend is not None
        if self.hotkey_live and not self.hotkey.register():
            self._hotkey_notice()
        self.quitting = False
        self.qt.aboutToQuit.connect(self.quit)

    def _hotkey_notice(self) -> None:
        self.window.notice.setText(f"{self.hotkey.error} 트레이 아이콘을 눌러 검색 창을 열 수 있습니다. "
                                   f"단축키는 설정 > 일반에서 바꿀 수 있습니다.")
        self.window.notice.show()

    def start(self) -> None:
        self.stats_thread.start()
        if self.settings.launch_at_login and self.startup.supported and self.startup.state() == "moved":
            # EXE 폴더를 옮긴 뒤 처음 실행: 이 앱이 만든 시작 항목만 지금 위치로 고친다
            try:
                self.startup.enable()
                log.info("startup entry moved to the current exe")
            except prefs.StartupError:
                log.exception("startup entry update failed")
        self.name_thread.start()
        self.name_search_thread.start()
        self._poll_timer.start()
        # 이름 목록: 저장된 목록으로 바로 찾을 수 있고, 꺼져 있던 동안의 변경은 뒤에서 맞춘다(처음이면 등록)
        self._scan_names()
        self.search_thread.start()
        self.index_thread.start()
        self.watch.start()
        self.watch.reconcile()   # 앱이 꺼져 있던 동안 놓친 변경 확인 (이미 색인한 폴더만)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            hint = f"{self.settings.hotkey} 또는 트레이 아이콘으로 검색 창을 엽니다." if self.hotkey.registered \
                else "트레이 아이콘으로 검색 창을 엽니다."
            self.tray.showMessage("NoseOut이 실행 중입니다", hint, app_icon(), 4000)
        # 트레이에서 시작: 트레이를 쓸 수 없거나 첫 설정이면 창을 연다(창도 트레이도 없는 상태를 만들지 않는다)
        if not (self.settings.start_in_tray and QSystemTrayIcon.isSystemTrayAvailable()) or self.setup is not None:
            self.window.summon()
            QTimer.singleShot(0, self.window.summon)  # 이벤트 루프가 시작된 뒤에도 전경 표시를 요청한다
        self.refresh_stats()
        if self.setup is not None:
            QTimer.singleShot(0, self.setup.show)  # 첫 실행: 폴더를 한 번에 고른다(모달 아님: 검색 창도 쓸 수 있다)

    def _setup_accepted(self) -> None:
        folders = self.setup.chosen()
        self.settings.folders = folders
        self.folders.list.clear()
        self.folders.list.addItems(folders)
        if self.setup.drive_box.isChecked():
            self.settings.name_folders = [self.setup.drive]
            self.folders.name_list.clear()
            self.folders.name_list.addItems(self.settings.name_folders)
        self._persist()
        self._scope_changed()
        if folders and self.setup.start_box.isChecked():
            self.start_indexing(folders)

    def _query(self, request: int, text: str) -> None:
        self.search.latest = request            # 서비스가 큐에 쌓인 오래된 요청을 건너뛰게
        self.search_request.emit(request, text, self.window.kind_filter.currentData())

    def _named(self) -> list[str]:
        """이름 목록의 범위: 검색 폴더, 코드 폴더(코드 프로젝트의 안전한 이름), 이름 검색 범위(드라이브 전체 등)."""
        return self._watched() + list(self.settings.name_folders)

    def _scan_names(self, force: bool = False, prompt: bool = False, method: str | None = None) -> None:
        """prompt: 이번 등록에 관리자 권한(UAC)을 요청해도 되는가. 사용자가 '빠른 NTFS'를 고르거나 '다시 등록'을 눌렀을 때만 참이다
        (앱 시작·자동 실행·범위 변경 때는 요청하지 않는다). method: 이번만 다른 방식(폴더 순회로 전체 확인)."""
        self.name_job = {"state": "checking", "added": 0}
        self._update_jobs()
        options = {"method": method or self.settings.name_method, "prompt": prompt, "force": force}
        self.name_scan_with.emit(list(self.settings.folders), list(self.settings.exclusions),
                                 list(self.settings.code_folders), list(self.settings.name_folders), options)

    def _save_name_folders(self, folders: list) -> None:
        self.settings.name_folders = list(folders)
        self._persist()
        self._scope_changed()

    def _ai_scope(self, path: str) -> bool:
        return scope.allows(path, self.settings.folders + self.settings.code_folders, self.settings.exclusions) or \
            os.path.normcase(os.path.abspath(path)) in {os.path.normcase(f) for f in self.settings.extra_files}

    def _analyzable(self, hit) -> bool:
        """'내용 분석' 버튼: 이름 결과이고, 분석 범위 밖이며, 분석할 수 있는 종류의 파일."""
        return getattr(hit, "source", "") == "name" and not getattr(hit, "is_dir", False) \
            and getattr(hit, "status", None) is None and rules.classify(Path(hit.path).name)[0] == "index" \
            and not self._ai_scope(hit.path)

    def analyze_file(self, path: str) -> None:
        """이름 결과의 그 파일만 내용 분석에 추가한다. 부모 폴더는 승인하지 않는다(검색 폴더 목록은 그대로)."""
        problem = check_file(path)
        if problem:
            self.window.status.setText(f"내용 분석에 추가하지 않았습니다: {problem}")
            return
        path = os.path.normpath(os.path.abspath(path))
        if os.path.normcase(path) not in {os.path.normcase(f) for f in self.settings.extra_files}:
            self.settings.extra_files.append(path)
            self._persist()
            self.folders.set_extra_files(self.settings.extra_files)
        self._extra_changed()
        self.run_files.emit([path])
        if self.ai_job["state"] != "running":
            self.ai_job = {"state": "running", "done": 0, "total": 0, "planned": False}
            self._update_jobs()
        self.window.status.setText(f"내용 분석에 추가했습니다: {Path(path).name} — 분석이 끝나면 내용 결과에 나옵니다.")
        self.window._show_detail(self.window.current_hit())

    def _save_extra(self, files: list) -> None:
        self.settings.extra_files = list(files)
        self._persist()
        self._extra_changed()
        self.window.invalidate_results()
        self.window._send()

    def _extra_changed(self) -> None:
        self.indexing.extra_files = list(self.settings.extra_files)
        self.search_extra.emit(list(self.settings.extra_files))
        self.watch.set_extra(self.settings.extra_files)

    def _name_query(self, request: int, text: str) -> None:
        self.name_search.latest = request
        self.name_request.emit(request, text, self.window.kind_filter.currentData())

    def _names_changed(self) -> None:
        if not self._names_timer.isActive():
            self._names_timer.start()

    def _names_watch(self, targets: list, rescans: list) -> None:
        if not self.quitting:
            self.name_apply.emit(list(targets), list(rescans))

    def _names_finished(self, summary: dict) -> None:
        self.name_job = {"state": "stopped" if summary.get("stopped") else "done", "added": summary.get("added", 0)}
        self._update_jobs()
        self.refresh_stats()
        self._names_changed()
        if not self.window.box.text().strip() and summary.get("added"):
            # 덧붙이기만 한다: 엔진 시작 실패·다른 엔진의 색인 같은 안내를 덮어쓰지 않게(최종 점검에서 발견)
            note = f"이름 {summary['files'] + summary['folders']:,}개 등록됨(이름으로 바로 찾기)"
            text = self.window.status.text().split(" · 이름 ")[0]
            if text.startswith("이름 ") and text.endswith("바로 찾기)"):
                text = ""
            self.window.status.setText(f"{text} · {note}" if text else note)
        for root in summary.get("unavailable", []):
            self.folders.log.appendPlainText(f"열 수 없는 폴더(이름 목록은 그대로 두고 '접근 불가'로 표시): {root}")

    def _names_failed(self, message: str) -> None:
        self.folders.log.appendPlainText(f"이름 목록: {message}")

    def _index_progress(self, done: int = 0, total: int = 0, *_args) -> None:
        if self.ai_job["state"] in ("running", "paused"):
            self.ai_job.update(done=done, total=total, planned=True)
            self._update_jobs()
            if not self._stats_timer.isActive():
                self._stats_timer.start()
        # 분석 중에도 끝낸 파일부터 의미 검색에 나오게 몇 초마다 다시 읽는다(커밋된 구간만 보인다)
        if not self.quitting and not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _search_refreshed(self, message: str) -> None:
        # 다시 읽는 동안 검색 스레드는 다음 검색을 처리하지 못한다(10만 벡터 약 0.44초, results/names-bench.json).
        # 간격을 다시 읽기 시간의 10배 이상으로 두어 막히는 시간이 색인 중 10%를 넘지 않게 한다.
        if self.search.refresh_ms:
            self._refresh_timer.setInterval(max(REFRESH_MS, int(self.search.refresh_ms[-1] * 10)))
        if not self.window.box.text().strip():
            self.window.status.setText(f"색인 중 · {message}")

    def show_large(self) -> None:
        self.window.set_expanded(True)
        self.window.summon()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.window.summon()

    def show_folders(self) -> None:
        self.show_settings("scope")

    def show_settings(self, tab: str | None = None) -> None:
        if not self.preferences.isVisible():
            self.preferences.load(self.settings)   # 열 때마다 적용된 값에서 시작한다
        if tab:
            self.preferences.show_tab(tab)
        self.preferences.show()
        self.preferences.raise_()
        self.preferences.activateWindow()
        self.refresh_stats()

    # 설정 저장·적용

    def _persist(self, settings: Settings | None = None) -> bool:
        """설정 파일을 안전하게 바꿔 넣는다. 실패하면 기존 파일은 그대로이고, 화면에 알린다(범위 관리는 즉시 저장 방식)."""
        try:
            (settings or self.settings).save(self.settings_path)
            return True
        except OSError as error:
            log.exception("settings save failed")
            message = (f"설정 파일을 저장하지 못했습니다({error.strerror or error}). 저장된 설정 파일은 그대로이며, "
                       "이번 변경은 다시 시작하면 사라질 수 있습니다.")
            self.window.notice.setText(message)
            self.window.notice.show()
            self.folders.log.appendPlainText(message)
            return False

    def apply_preferences(self, values: dict) -> str | None:
        """설정창 일반·화면의 '적용'. 단축키·시작 항목(외부 상태)을 먼저 바꾸고 설정 파일을 저장한다. 어느 단계든 실패하면
        이미 바꾼 외부 상태를 되돌리고 이전 설정을 그대로 둔다(저장된 설정과 실제 상태가 어긋나지 않게). 오류 문구를 돌려준다."""
        values = dict(values)
        reset_window = values.pop("reset_window", False)
        self._remember_window()
        new = replace(self.settings, **values)
        new.hotkey = prefs.normalize_hotkey(new.hotkey)
        if reset_window:
            new.window_small, new.window_large = [], []
        undo: list = []

        def rollback() -> None:
            for step in reversed(undo):
                try:
                    step()
                except Exception:  # noqa: BLE001 - 되돌리기 실패는 로그로 남기고 다음 단계를 계속 되돌린다
                    log.exception("settings rollback step failed")

        hotkey_changed = prefs.normalize_hotkey(self.settings.hotkey) != new.hotkey
        if not hotkey_changed and self.hotkey_live and not self.hotkey.registered and "hotkey" in values:
            # 시작 때 충돌로 등록하지 못한 같은 단축키: 다른 설정 적용을 막지 않고 다시 시도만 한다
            if self.hotkey.change(new.hotkey):
                undo.append(self.hotkey.unregister)
        if hotkey_changed:
            problem = prefs.hotkey_problem(new.hotkey)
            if problem:
                return f"{problem} 기존 단축키({self.settings.hotkey})를 그대로 씁니다."
            if self.hotkey_live:
                old_text, was = self.hotkey.text, self.hotkey.registered
                if not self.hotkey.change(new.hotkey):
                    return f"{self.hotkey.error} 기존 단축키({old_text})를 그대로 씁니다. 다른 조합을 고르세요."

                def restore_hotkey() -> None:
                    if was:
                        self.hotkey.change(old_text)
                    else:
                        self.hotkey.unregister()
                        self.hotkey.text = old_text
                undo.append(restore_hotkey)
            else:
                old_text = self.hotkey.text
                self.hotkey.text = new.hotkey
                undo.append(lambda: setattr(self.hotkey, "text", old_text))
        if new.launch_at_login != self.settings.launch_at_login:
            before = self.startup.backend.get(prefs.ENTRY)
            try:
                self.startup.enable() if new.launch_at_login else self.startup.disable()
            except prefs.StartupError as error:
                rollback()
                return f"{error} 다른 설정도 적용하지 않았습니다."

            def restore_startup() -> None:
                if before is None:
                    self.startup.backend.delete(prefs.ENTRY)
                else:
                    self.startup.backend.set(prefs.ENTRY, before)
            undo.append(restore_startup)
        try:
            new.save(self.settings_path)
        except OSError as error:
            log.exception("settings apply failed")
            rollback()
            return (f"설정 파일을 저장하지 못했습니다({error.strerror or error}). 이전 설정과 단축키·시작 항목을 그대로 "
                    "둡니다.")
        self.settings = new
        apply_theme(self.qt, new.theme)
        self.window.large_view = new.large_view
        if reset_window:
            self.window.reset_geometry()
        if self.hotkey.registered:
            self.window.notice.hide()
        log.info("settings applied: %s", ", ".join(sorted(values)))
        return None

    def _remember_window(self) -> None:
        self.settings.window_small = list(self.window.geometries.get("small") or [])
        self.settings.window_large = list(self.window.geometries.get("large") or [])

    def _save_window(self) -> None:
        """검색 창을 숨길 때(Esc·닫기·종료) 크기·위치를 저장한다. 바뀌지 않았으면 쓰지 않는다."""
        before = (list(self.settings.window_small), list(self.settings.window_large))
        if self.window.isVisible():
            self.window._remember_geometry()
        self._remember_window()
        if (self.settings.window_small, self.settings.window_large) != before:
            try:
                self.settings.save(self.settings_path)
            except OSError:
                log.exception("window position save failed")   # 창 위치만: 다음 저장 때 다시 쓴다

    # 작업 상태와 색인 현황

    def refresh_stats(self) -> None:
        if self.quitting:
            return
        if self._stats_busy:
            self._stats_again = True
            return
        self._stats_busy = True
        if self.preferences.isVisible():
            self.preferences.stats_pending()
        self.stats_request.emit({"base": self.base, "folders": list(self.settings.folders),
                                 "code_folders": list(self.settings.code_folders),
                                 "extra_files": list(self.settings.extra_files),
                                 "exclusions": list(self.settings.exclusions)})

    def _stats_ready(self, data: dict) -> None:
        self._stats_busy = False
        if self.quitting:
            return
        self.last_stats = data if "error" not in data else {}
        self._update_jobs()
        if self._stats_again:
            self._stats_again = False
            self.refresh_stats()

    def _job_texts(self) -> dict:
        names, ai = self.name_job, self.ai_job
        counted = self.last_stats.get("names", {})
        name_text = {"running": f"등록 중 (새 이름 {names['added']:,}개)", "paused": "일시정지 (이미 등록한 이름은 검색됨)",
                     "checking": "변경 확인 중 (저장된 목록으로 검색 가능)",
                     "stopped": "중지됨 (다음 실행·'다시 등록' 때 이어서 맞춤)"}.get(names["state"])
        if name_text is None:
            name_text = f"완료 ({counted.get('files', 0) + counted.get('folders', 0):,}개)" if counted.get("exists")                 else ("완료" if names["state"] == "done" else "아직 없음")
        progress = f" {ai['done']:,}/{ai['total']:,}" if ai["planned"] and ai["total"] else ""
        ai_text = {"running": f"분석 중{progress}" if ai["planned"] else "분석 중 (목록 확인 중)",
                   "paused": f"일시정지{progress}", "done": "완료", "stopped": "중지됨 (남은 파일은 다음에 이어서)",
                   "failed": "실패 (설정 > 색인 현황·로그 확인)"}.get(ai["state"], "대기")
        counts = self.last_stats.get("ai", {})
        if not counts or counts.get("error") or counts.get("busy"):
            searchable = remaining = "확인 중"
        else:
            searchable = f"{counts['done'] + counts['partial']:,}개"
            if ai["state"] == "running" and not ai["planned"]:
                remaining = "확인 중"
            else:
                remaining = f"{counts['pending'] + counts['partial']:,}개"
                if counts.get("unchecked_roots"):
                    remaining += f" + 분석 시작 전 폴더 {counts['unchecked_roots']}개"
        return {"names": name_text, "ai": ai_text, "searchable": searchable, "remaining": remaining}

    def _update_jobs(self) -> None:
        if self.quitting:
            return
        texts = self._job_texts()
        counts = self.last_stats.get("ai", {})
        known = bool(counts) and not counts.get("error") and not counts.get("busy")
        running = self.ai_job["state"] in ("running", "paused")
        self.window.index_info = {
            "searchable": counts["done"] + counts["partial"] if known else None,
            "remaining": (None if running and not self.ai_job["planned"] else counts["pending"] + counts["partial"])
            if known else None,
            "unchecked": counts.get("unchecked_roots", 0) if known else 0,
            "running": running, "scope": bool(self.settings.folders or self.settings.extra_files),
            "names_busy": self.name_job["state"] in ("running", "checking")}
        show = bool(self.settings.folders or self.settings.extra_files or self.settings.name_folders
                    or self.settings.code_folders or self.last_stats.get("names", {}).get("exists")
                    or counts.get("exists"))
        self.window.set_jobs(f"이름 등록: {texts['names']} · 내용 분석: {texts['ai']} · 검색 가능 {texts['searchable']} · "
                             f"남은 분석 {texts['remaining']}" if show else "")
        if self.preferences.isVisible():
            self.preferences.show_stats(self.last_stats, texts)

    def _save_name_method(self, method: str) -> None:
        if method == self.settings.name_method:
            return
        self.settings.name_method = method
        self._persist()
        # 사용자가 고른 순간: 빠른 NTFS면 등록이 필요한 볼륨에만 관리자 권한을 요청한다(이미 맞춘 볼륨은 변경 기록만 따라잡음)
        self._scan_names(prompt=method == "ntfs")

    def _name_method_state(self, state: dict) -> None:
        self.name_method_state = state
        self.folders.show_method_state(state)
        self.watch.set_usn_roots(state.get("usn_roots", []))

    def _name_stage(self, stage: str) -> None:
        if self.name_job["state"] in ("checking", "running"):
            self.name_job["state"] = "checking" if stage == "checking" else "running"
            self._update_jobs()

    def _poll_names(self) -> None:
        if not self.quitting and self.settings.watch and self.settings.name_method != "walk" and not self.names.busy:
            self.name_poll.emit()

    def _name_progress(self, added: int, _folder: str) -> None:
        if self.name_job["state"] != "paused":
            self.name_job = {"state": "running", "added": added}
            self._update_jobs()

    def _name_paused(self, paused: bool) -> None:
        self.name_job["state"] = "paused" if paused else "running"
        self._update_jobs()

    def _index_planned(self, _count: int) -> None:
        self.ai_job["planned"] = True
        self.refresh_stats()     # 계획을 커밋한 뒤라 남은 분석 수를 셀 수 있다

    def _save_folders(self, folders: list) -> None:
        self.settings.folders = folders
        self._persist()
        self._scope_changed()

    def _save_exclusions(self, exclusions: list) -> None:
        self.settings.exclusions = list(exclusions)
        self._persist()
        self._scope_changed()

    def _scope_changed(self) -> None:
        self.window.invalidate_results()  # 범위 밖 파일을 열거나 미리 보지 않게 즉시 비우고 옛 응답도 거부한다
        self.search_scope.emit(self.settings.folders, self.settings.exclusions)
        self.search_code_scope.emit(self.settings.code_folders)
        # 이름 목록: 범위 밖은 즉시 숨기고(검색 범위), 새로 고른 폴더는 바로 등록한다(AI 분석은 '색인 시작'으로)
        self.name_scope.emit(self._named(), list(self.settings.exclusions))
        self._scan_names()
        self.watch.set_scope(self._watched(), self.settings.exclusions, self.settings.name_folders)
        self.window._send()

    def _watched(self) -> list[str]:
        """감시할 폴더: 검색 폴더와 코드 폴더 (같은 폴더는 한 번)."""
        seen, out = set(), []
        for folder in self.settings.folders + self.settings.code_folders:
            if os.path.normcase(folder) not in seen:
                seen.add(os.path.normcase(folder))
                out.append(folder)
        return out

    def _save_code_folders(self, folders: list) -> None:
        self.settings.code_folders = list(folders)
        self._persist()
        self._scope_changed()

    def _save_editor(self, editor: str) -> None:
        self.settings.code_editor = editor or ""
        self.window.code_editor = self.settings.code_editor
        self._persist()

    def _toggle_watch(self, enabled: bool) -> None:
        self.settings.watch = enabled
        self._persist()
        self.watch.set_enabled(enabled)

    def _watch_state(self, text: str) -> None:
        self.folders.show_watch_state(text)
        self.tray.setToolTip(f"NoseOut 검색 — {text}")

    def _auto_index(self, targets: list, rescans: list) -> None:
        if self.index_active or self.quitting:
            # 그 사이 수동 색인이 시작됨: 다시 모아 두었다가 끝난 뒤 반영한다
            self.watch.job_failed()
            for path in targets:
                self.watch.batcher.add(path)
            for folder in rescans:
                self.watch.batcher.rescan(folder)
            return
        self.index_active = True
        self.auto_job = True
        self.ai_job = {"state": "running", "done": 0, "total": 0, "planned": False}
        self._update_jobs()
        self.folders.indexing_started(auto=True)
        self.indexing.prepare()
        self.indexing.exclusions = list(self.settings.exclusions)
        self.indexing.code_roots = list(self.settings.code_folders)
        self.run_changes.emit(list(self.settings.folders), list(targets), list(rescans))

    def start_indexing(self, folders: list) -> None:
        if not folders and not self.settings.code_folders and not self.settings.extra_files:
            self.folders.indexing_failed("먼저 검색할 폴더나 코드 폴더를 추가하세요.")
            return
        if self.index_active:
            return
        self.folders.indexing_started()
        self.index_active = True
        self.auto_job = False
        self.ai_job = {"state": "running", "done": 0, "total": 0, "planned": False}
        self._update_jobs()
        self.watch.manual_started()
        self._scan_names()   # 이름 목록도 다시 맞춘다
        self.indexing.prepare()
        self.indexing.exclusions = list(self.settings.exclusions)
        self.indexing.code_roots = list(self.settings.code_folders)
        self.indexing.extra_files = list(self.settings.extra_files)
        self.run_index.emit(list(folders))

    def _index_finished(self, summary: dict) -> None:
        self.index_active = False
        self.ai_job = dict(self.ai_job, state="stopped" if summary.get("stopped") else "done")
        self._update_jobs()
        self.refresh_stats()
        self.folders.indexing_finished(summary)
        self.watch.job_finished(summary)
        if summary.get("auto") and not (summary["added"] or summary["changed"] or summary["removed"]
                                        or summary["results"]):
            return   # 바뀐 것이 없는 확인: 검색 벡터를 다시 읽지 않는다
        self.search_reload.emit()
        if not self.quitting and not self.preferences.isVisible() and not summary.get("auto"):
            self.tray.showMessage("색인을 마쳤습니다", f"읽음 {summary['done']}, 실패 {summary['failed']}",
                                  app_icon(), 4000)

    def _index_paused(self, paused: bool) -> None:
        self.folders.indexing_paused(paused)
        if self.ai_job["state"] in ("running", "paused"):
            self.ai_job["state"] = "paused" if paused else "running"
            self._update_jobs()
            self.refresh_stats()
        if paused:
            self.search_reload.emit()  # 커밋 경계에서 기다리므로 이미 끝낸 구간은 안전하게 검색에 반영한다

    def _index_failed(self, message: str) -> None:
        self.index_active = False
        self.ai_job = dict(self.ai_job, state="failed")
        self._update_jobs()
        self.refresh_stats()
        self.folders.indexing_failed(message)
        self.watch.job_failed()
        if getattr(self, "auto_job", False):
            # 자동 갱신이 실패하면(엔진이 다른 색인, 엔진 시작 실패 등) 변경마다 같은 실패를 되풀이하지 않게 멈춘다.
            # '색인 시작'을 누르면 다시 시도하고 감시도 다시 켠다.
            self.watch.hold()
        if not self.quitting:
            self.tray.showMessage("색인하지 못했습니다", message, app_icon(), 6000)

    def quit(self) -> None:
        """진행 중인 색인을 멈추고 엔진들을 끈 뒤 끝낸다."""
        if self.quitting:
            return
        self._save_window()          # 창이 이미 숨겨져 있어도 마지막 크기·위치를 남긴다
        self.quitting = True
        self.hotkey.unregister()
        self.qt.removeNativeEventFilter(self.hotkey)
        self.tray.hide()
        self.window.hide()
        self.preferences.hide()
        self._stats_timer.stop()
        self.watch.shutdown()
        self._refresh_timer.stop()
        self._names_timer.stop()
        self._poll_timer.stop()
        self.names.request_stop()      # 걷는 중이면 다음 폴더 앞에서 멈춘다 (커밋한 것은 남는다)
        self.indexing.request_stop()
        self.search.request_stop()
        if self.indexing.running:
            # 지금 파일이 금방 끝나지 않으면 엔진을 끊어 되돌리고 끝낸다(파일마다 커밋하므로 이미 읽은 것은 남는다)
            deadline = 3000
            waited = 0
            while self.indexing.running and waited < deadline:
                self.qt.processEvents()
                QThread.msleep(50)
                waited += 50
            if self.indexing.running:
                self.indexing.abort_engine()
        self.index_thread.quit()
        self.index_thread.wait(30000)
        from PySide6.QtCore import QMetaObject

        if self.search_thread.isRunning():
            QMetaObject.invokeMethod(self.search, "shutdown", Qt.ConnectionType.BlockingQueuedConnection)
        self.search_thread.quit()
        self.search_thread.wait(10000)
        for thread, service in ((self.name_thread, self.names), (self.name_search_thread, self.name_search)):
            if thread.isRunning():
                QMetaObject.invokeMethod(service, "shutdown", Qt.ConnectionType.BlockingQueuedConnection)
            thread.quit()
            thread.wait(10000)
        self.stats_thread.quit()      # 세는 중이면 그 한 번이 끝난 뒤 멈춘다(읽기만 함)
        self.stats_thread.wait(10000)
        self.qt.quit()


def setup_logging(base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    # 개인용으로 오래 켜 두어도 로그가 끝없이 커지지 않게: app.log 1 MB, 이전 것 2개(app.log.1, app.log.2)만 둔다
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(base / "app.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def hook(kind, value, tb):
        log.error("unhandled error", exc_info=(kind, value, tb))
        QMessageBox.critical(None, "NoseOut", "예상하지 못한 오류가 났습니다. 자세한 내용은 설정 폴더의 app.log에 남겼습니다.")

    sys.excepthook = hook


def install_korean(qt_app: QApplication) -> bool:
    """Qt 표준 버튼·대화상자(예/아니요/확인, 파일 고르기)를 한국어로. PySide6에 들어 있는 qtbase_ko.qm을 쓴다(EXE에도 포함)."""
    from PySide6.QtCore import QLibraryInfo, QTranslator

    import PySide6

    translator = QTranslator(qt_app)
    for folder in (QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
                   str(Path(PySide6.__file__).parent / "translations")):
        if translator.load("qtbase_ko", folder):
            return qt_app.installTranslator(translator)
    log.warning("Korean Qt translation not found")
    return False


def main() -> int:
    base = home()
    setup_logging(base)
    for note in runtime_setup.NOTES:       # 로그 설정 전(공개용 EXE 시작 직후)에 준비한 런타임 기록
        log.info(note)
    for note in paths.NOTES:               # 설정 폴더 이전·옛 환경 변수 경고
        log.warning(note)
    qt_app = QApplication(sys.argv[:1])
    qt_app.setApplicationName("NoseOut")
    install_korean(qt_app)
    qt_app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(base / "app.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "NoseOut", "NoseOut이 이미 실행 중입니다. 트레이 아이콘이나 단축키로 검색 창을 여세요.")
        return 0
    app = None
    try:
        app = App(qt_app, base)
        app.start()
        return qt_app.exec()
    finally:
        if app is not None:
            app.quit()
        lock.unlock()
