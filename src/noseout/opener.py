"""검색 결과 열기: 연결 프로그램으로 열기, PDF 쪽·음성/영상 시점으로 열기, 탐색기에서 위치 보기.

쪽·시점은 Windows의 공통 방법이 없어 연결 프로그램마다 명령줄이 다르다. 아는 프로그램이면 그 형식으로 열고, 모르면
일반 열기로 열면서 "처음부터 열린다"고 알려 준다(원본 macOS는 Preview·QuickTime을 직접 조작했다).
- PDF: Chromium 계열 브라우저(Chrome, Edge, Aside 등; `--single-argument`로 등록됨) → file URL + `#page=N`,
  Acrobat/Reader → `/A page=N`, SumatraPDF → `-page N`
- 음성·영상: 팟플레이어 → `/seek=HH:MM:SS`, VLC → `--start-time=초`, mpv → `--start=초`
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

ASSOCSTR_COMMAND = 1
ASSOCSTR_EXECUTABLE = 2
ASSOCSTR_FRIENDLYAPPNAME = 4
CHROMIUM = {"chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe", "aside.exe", "whale.exe"}


@dataclass
class Handler:
    exe: str            # 실행 파일 경로 ("" = 알 수 없음)
    name: str           # 사용자에게 보일 이름
    command: str        # 레지스트리의 open 명령 원문


@dataclass
class Plan:
    argv: list[str] | None     # None이면 os.startfile(path)
    note: str | None           # 쪽·시점으로 못 여는 경우의 안내


def handler_for(path: str) -> Handler:
    """이 확장자의 기본 연결 프로그램 (AssocQueryStringW)."""
    ext = Path(path).suffix.lower()
    return Handler(_assoc(ext, ASSOCSTR_EXECUTABLE), _assoc(ext, ASSOCSTR_FRIENDLYAPPNAME) or ext,
                   _assoc(ext, ASSOCSTR_COMMAND))


def _assoc(ext: str, what: int) -> str:
    try:
        from ctypes import wintypes

        query = ctypes.windll.shlwapi.AssocQueryStringW
        query.argtypes = [ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR,
                          ctypes.POINTER(wintypes.DWORD)]
        buffer = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(2048)
        return buffer.value if query(0, what, ext, None, buffer, ctypes.byref(size)) == 0 else ""
    except (AttributeError, OSError):
        return ""


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def plan(path: str, page: int | None = None, seconds: float | None = None, handler: Handler | None = None) -> Plan:
    """어떻게 열지 정한다(실행하지 않음). 테스트는 handler를 직접 넘긴다."""
    if page is None and seconds is None:
        return Plan(None, None)
    handler = handler or handler_for(path)
    exe = os.path.basename(handler.exe).lower()
    where = f"{page}쪽" if page is not None else f"{clock(seconds)}"
    if page is not None:
        if exe in CHROMIUM or "--single-argument" in handler.command:
            return Plan([handler.exe, Path(path).resolve().as_uri() + f"#page={page}"], None)
        if exe in ("acrobat.exe", "acrord32.exe"):
            return Plan([handler.exe, "/A", f"page={page}", path], None)
        if exe == "sumatrapdf.exe":
            return Plan([handler.exe, "-page", str(page), path], None)
    else:
        if exe.startswith("potplayer"):
            return Plan([handler.exe, path, f"/seek={clock(seconds)}"], None)
        if exe == "vlc.exe":
            return Plan([handler.exe, f"--start-time={int(seconds)}", path], None)
        if exe == "mpv.exe":
            return Plan([handler.exe, f"--start={int(seconds)}", path], None)
    return Plan(None, f"{handler.name}은(는) {where}부터 여는 방법을 지원하지 않아 처음부터 열었습니다.")


def open_result(path: str, page: int | None = None, seconds: float | None = None) -> str | None:
    """파일을 연다. 쪽·시점으로 열지 못했으면 안내 문구를 돌려준다. 파일은 읽기만 한다(연결 프로그램이 연다)."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    chosen = plan(path, page, seconds)
    if chosen.argv and chosen.argv[0] and os.path.exists(chosen.argv[0]):
        subprocess.Popen(chosen.argv, close_fds=True)
        return None
    os.startfile(path)  # noqa: S606 (사용자가 고른 결과를 기본 프로그램으로 연다)
    return chosen.note


# 연결 프로그램으로 "열면" 실행되는 확장자 (프로그램, 설치 파일, 스크립트, 바로 가기·인터넷 바로 가기, 제어판·관리 콘솔,
# 레지스트리·설치 정보 파일). 이름 검색은 AI 색인과 달리 모든 파일을 결과로 내므로 Enter 한 번에 이런 파일이 실행되지 않게
# 위치만 보여 준다. 시스템의 PATHEXT도 더한다.
EXECUTABLE_EXTENSIONS = {
    "exe", "com", "scr", "pif", "cpl", "msc", "msi", "msp", "mst", "appx", "appxbundle", "msix", "msixbundle",
    "appinstaller", "application", "appref-ms", "xbap", "gadget", "bat", "cmd", "ps1", "psm1", "psd1", "ps1xml",
    "vbs", "vbe", "js", "jse", "wsf", "wsh", "wsc", "hta", "sct", "py", "pyw", "pyz", "pyzw", "jar", "lnk", "url",
    "website", "inf", "reg", "scf", "settingcontent-ms", "library-ms", "searchconnector-ms", "diagcab", "xll", "ws",
}


def is_executable(path: str) -> bool:
    ext = Path(path).suffix.lower().lstrip(".")
    if not ext:
        return False
    pathext = {e.strip(".").lower() for e in os.environ.get("PATHEXT", "").split(";") if e.strip(".")}
    return ext in EXECUTABLE_EXTENSIONS or ext in pathext


def open_folder(path: str) -> None:
    """폴더 결과: 탐색기로 연다(폴더 이름이 .exe로 끝나도 실행하지 않게 explorer.exe에 넘긴다)."""
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    subprocess.Popen(["explorer.exe", os.path.normpath(path)])


def reveal(path: str) -> None:
    """탐색기에서 파일 위치를 열고 그 파일을 선택한다."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    subprocess.Popen(["explorer.exe", f"/select,{os.path.normpath(path)}"])


# 코드 결과 열기 (6단계, 원본 Sources/DigUpApp/CodeEditors.swift를 Windows 편집기로 옮김)
#
# 원본은 macOS 번들 ID로 편집기를 찾고 편집기마다 줄을 넘기는 방법(URL, 명령)을 쓴다. Windows에서는 URL 프로토콜 등록
# (HKCR\<scheme>)과 App Paths·기본 설치 위치로 찾는다. 코드 파일을 연결 프로그램(os.startfile)으로 열면 .py·.js·.bat 같은
# 파일이 실행될 수 있으므로, 편집기가 없으면 메모장으로 연다(메모장은 줄 위치를 받지 않는다: 처음부터 열고 알린다).


@dataclass(frozen=True)
class Editor:
    id: str
    name: str
    scheme: str | None = None                 # "<scheme>://file/<경로>:<줄>:1"
    exe_names: tuple[str, ...] = ()           # App Paths·기본 위치에서 찾을 실행 파일
    places: tuple[str, ...] = ()              # %ProgramFiles% 등 기준 기본 설치 위치
    style: str = ""                           # "colon": <경로>:<줄>, "dash-n": -n<줄> <경로>


EDITORS = (
    Editor("vscode", "Visual Studio Code", scheme="vscode"),
    Editor("vscode-insiders", "VS Code Insiders", scheme="vscode-insiders"),
    Editor("cursor", "Cursor", scheme="cursor"),
    Editor("windsurf", "Windsurf", scheme="windsurf"),
    Editor("vscodium", "VSCodium", scheme="vscodium"),
    Editor("sublime", "Sublime Text", exe_names=("subl.exe", "sublime_text.exe"),
           places=(r"%ProgramFiles%\Sublime Text\subl.exe", r"%ProgramFiles%\Sublime Text 3\subl.exe"), style="colon"),
    Editor("notepad++", "Notepad++", exe_names=("notepad++.exe",),
           places=(r"%ProgramFiles%\Notepad++\notepad++.exe", r"%ProgramFiles(x86)%\Notepad++\notepad++.exe"),
           style="dash-n"),
)
SUPPORTED = ", ".join(e.name for e in EDITORS)


@dataclass
class CodePlan:
    url: str | None            # 열 URL (편집기 프로토콜)
    argv: list[str] | None     # 또는 실행할 명령
    note: str | None           # 줄 위치로 열지 못한 경우의 안내


def _scheme_registered(scheme: str) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
            winreg.QueryValueEx(key, "URL Protocol")
        return True
    except OSError:
        return False


def _app_path(exe: str) -> str:
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                    value = winreg.QueryValueEx(key, "")[0]
                    if value and os.path.exists(value.strip('"')):
                        return value.strip('"')
            except OSError:
                continue
    except ImportError:
        pass
    return ""


def editor_location(editor: Editor) -> str | None:
    """설치되어 있으면 URL 프로토콜 이름이나 실행 파일 경로, 없으면 None."""
    if editor.scheme:
        return editor.scheme if _scheme_registered(editor.scheme) else None
    for exe in editor.exe_names:
        found = _app_path(exe)
        if found:
            return found
    for place in editor.places:
        path = os.path.expandvars(place)
        if "%" not in path and os.path.exists(path):
            return path
    return None


def installed_editors() -> dict[str, str]:
    """설치된 편집기: id → 위치 (EDITORS 순서)."""
    found = {}
    for editor in EDITORS:
        where = editor_location(editor)
        if where:
            found[editor.id] = where
    return found


def notepad() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")


def plan_code(path: str, line: int | None, choice: str = "", installed: dict[str, str] | None = None) -> CodePlan:
    """코드 결과를 어떻게 열지 정한다(실행하지 않음). choice: ""(자동: 설치된 첫 편집기), 편집기 id, "notepad".
    line이 None이면(노트북의 셀 등) 처음부터 연다."""
    installed = installed_editors() if installed is None else installed
    chosen = None
    if choice != "notepad":
        ids = [choice] if choice and choice in installed else [e.id for e in EDITORS if e.id in installed]
        chosen = next((e for e in EDITORS if ids and e.id == ids[0]), None)
    if chosen is None:
        why = ("메모장으로 열었습니다(설정)." if choice == "notepad" else
               f"줄 위치로 여는 편집기({SUPPORTED})를 찾지 못해 메모장으로 열었습니다.")
        return CodePlan(None, [notepad(), path], why + (f" {line}번째 줄을 찾아가세요." if line else ""))
    where = installed[chosen.id]
    note = None if line else f"{chosen.name}에서 처음부터 열었습니다(줄 위치 없음)."
    if chosen.scheme:
        from urllib.parse import quote

        target = quote(Path(path).resolve().as_posix(), safe="/:")
        return CodePlan(f"{chosen.scheme}://file/{target}" + (f":{line}:1" if line else ""), None, note)
    if chosen.style == "dash-n":
        return CodePlan(None, [where] + ([f"-n{line}"] if line else []) + [path], note)
    return CodePlan(None, [where, f"{path}:{line}" if line else path], note)


def open_code(path: str, line: int | None, choice: str = "") -> str | None:
    """코드 파일을 편집기의 그 줄에서 연다. 파일을 실행하지 않는다(연결 프로그램으로 열지 않음)."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    chosen = plan_code(path, line, choice)
    if chosen.url:
        os.startfile(chosen.url)  # noqa: S606 (등록된 편집기의 URL 프로토콜)
    else:
        subprocess.Popen(chosen.argv, close_fds=True)
    return chosen.note
