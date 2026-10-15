"""설정창(2026-10-14)의 화면 밖 로직: 설정 파일 안전 저장, Windows 로그인 시 실행 항목, 단축키 검사, 창 위치 맞추기.
화면 없이 테스트할 수 있게 위젯을 쓰지 않는다. 원본 DigUp에는 없는 Windows판 기능이다."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from . import hotkey

# 설정 파일


def atomic_write(path: Path, text: str) -> None:
    """같은 폴더의 임시 파일에 다 쓴 뒤 한 번에 바꿔 넣는다. 쓰는 도중 실패·전원 차단에도 이전 settings.json이 반쯤 덮이지
    않는다. 실패하면 임시 파일만 지우고 OSError를 그대로 올린다(호출하는 쪽이 이전 설정을 유지하고 안내한다)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# 단축키

RESERVED = {"alt+tab", "alt+f4", "alt+space", "ctrl+esc", "win+l"}   # Windows가 쓰는 조합: 가로채지 않는다


def normalize_hotkey(text: str) -> str:
    """'ctrl + shift + space' → 'Ctrl+Shift+Space' (비교·저장용). 해석할 수 없으면 원문을 다듬기만 한다."""
    parts = [p.strip() for p in text.split("+") if p.strip()]
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    order = ["Ctrl", "Alt", "Shift", "Win"]
    mods = sorted({names[p.lower()] for p in parts[:-1] if p.lower() in names}, key=order.index)
    if not parts:
        return ""
    key = parts[-1]
    key = key.upper() if len(key) == 1 else key[:1].upper() + key[1:].lower()
    return "+".join(mods + [key])


def hotkey_from_sequence(portable: str) -> str:
    """QKeySequence의 PortableText('Ctrl+Shift+Space', 'Meta+K', 'Ctrl+Return')를 hotkey.parse가 읽는 형식으로."""
    parts = [p for p in portable.split(",")[0].split("+") if p]
    rename = {"meta": "Win", "return": "Enter"}
    return normalize_hotkey("+".join(rename.get(p.lower(), p) for p in parts))


def hotkey_problem(text: str) -> str | None:
    """등록해 보기 전에 거르는 조합. 다른 프로그램과의 충돌은 등록해 봐야 안다(GlobalHotkey.change)."""
    if not text.strip():
        return "단축키를 입력하세요."
    try:
        modifiers, _vk = hotkey.parse(text)
    except ValueError as problem:
        return f"쓸 수 없는 단축키입니다: {problem}"
    if modifiers == hotkey.MOD["shift"]:
        return "Shift만 함께 누르는 조합은 글자 입력과 겹칩니다. Ctrl·Alt·Win 중 하나를 함께 누르세요."
    if normalize_hotkey(text).lower() in RESERVED:
        return f"{normalize_hotkey(text)}는 Windows가 쓰는 조합입니다."
    return None


# Windows 로그인 시 실행 (현재 사용자 계정의 Run 항목만)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
ENTRY = "NoseOut"
MARKER = "--startup"     # 이 앱이 만든 항목의 표시: 끌 때 다른 프로그램이 만든 같은 이름의 항목은 지우지 않는다
EXE_NAME = "NoseOut.exe"


class StartupError(Exception):
    pass


class RegistryRun:
    """HKEY_CURRENT_USER의 Run 키만 읽고 쓴다(관리자 권한·시스템 전체 항목 없음)."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass

    def approved(self, name: str) -> bool | None:
        """작업 관리자·Windows 설정의 '시작 앱'에서 끈 항목인가(읽기만). 기록이 없으면 None."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
                data, _kind = winreg.QueryValueEx(key, name)
        except (FileNotFoundError, OSError):
            return None
        return not (bytes(data)[:1] and bytes(data)[0] & 1)   # 02… 켜짐, 03… 꺼짐


def frozen_exe() -> str | None:
    """PyInstaller로 만든 NoseOut.exe로 실행 중이면 그 경로. 소스(python -m noseout app) 실행이면 None."""
    if getattr(sys, "frozen", False) and Path(sys.executable).name.lower() == EXE_NAME.lower():
        return os.path.normpath(sys.executable)
    return None


class Startup:
    """로그인 시 실행 항목. 켤 때 현재 EXE 위치로 등록하고, 끌 때는 이 앱이 만든 항목만 지운다."""

    def __init__(self, backend=None, exe: str | None = None):
        self.backend = backend if backend is not None else RegistryRun()
        self.exe = exe if exe is not None else frozen_exe()

    @property
    def supported(self) -> bool:
        return bool(self.exe)

    def command(self) -> str:
        return f'"{self.exe}" {MARKER}'

    @staticmethod
    def target(value: str) -> str | None:
        """이 앱이 만든 값이면 EXE 경로, 아니면 None."""
        value = value.strip()
        if not value.startswith('"') or value.count('"') < 2:
            return None
        exe, rest = value[1:].split('"', 1)
        if Path(exe).name.lower() != EXE_NAME.lower() or rest.strip() != MARKER:
            return None
        return exe

    def state(self) -> str:
        """off(없음) | on(현재 EXE) | moved(이 앱이 만든 항목이지만 다른 위치의 EXE) | other(같은 이름의 다른 항목)."""
        value = self.backend.get(ENTRY)
        if value is None:
            return "off"
        exe = self.target(value)
        if exe is None:
            return "other"
        if self.exe and os.path.normcase(os.path.normpath(exe)) == os.path.normcase(self.exe):
            return "on"
        return "moved"

    def disabled_by_windows(self) -> bool:
        approved = getattr(self.backend, "approved", None)
        return approved is not None and approved(ENTRY) is False

    def enable(self) -> None:
        if not self.supported:
            raise StartupError("소스(python)로 실행 중이라 로그인 시 실행을 켤 수 없습니다. NoseOut.exe로 실행했을 때만 지원합니다.")
        if self.state() == "other":
            raise StartupError(f"시작 항목에 이 앱이 만들지 않은 '{ENTRY}' 항목이 있어 바꾸지 않았습니다.")
        try:
            self.backend.set(ENTRY, self.command())
        except OSError as error:
            raise StartupError(f"시작 항목을 등록하지 못했습니다: {error.strerror or error}") from error
        if self.state() != "on":
            raise StartupError("시작 항목을 등록했지만 다시 읽은 값이 다릅니다.")

    def disable(self) -> None:
        value = self.backend.get(ENTRY)
        if value is None:
            return
        if self.target(value) is None:
            raise StartupError(f"시작 항목의 '{ENTRY}'은 이 앱이 만든 항목이 아니라 지우지 않았습니다.")
        try:
            self.backend.delete(ENTRY)
        except OSError as error:
            raise StartupError(f"시작 항목을 지우지 못했습니다: {error.strerror or error}") from error


class MemoryRun:
    """레지스트리 대신 쓰는 대체 구현(테스트·시험 실행용). 실제 Windows 시작 항목을 바꾸지 않는다."""

    def __init__(self, values: dict | None = None, approved: dict | None = None, fail: str | None = None):
        self.values = dict(values or {})
        self.approved_values = dict(approved or {})
        self.fail = fail     # "set" | "delete": 그 동작에서 OSError를 낸다

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        if self.fail == "set":
            raise OSError(5, "액세스가 거부되었습니다")
        self.values[name] = value

    def delete(self, name: str) -> None:
        if self.fail == "delete":
            raise OSError(5, "액세스가 거부되었습니다")
        self.values.pop(name, None)

    def approved(self, name: str) -> bool | None:
        return self.approved_values.get(name)


# 창 위치

TITLE_BAR = 32   # 제목 표시줄이 화면 위로 나가 창을 잡지 못하는 일이 없게


def fit_rect(rect: list[int] | tuple, screens: list[tuple[int, int, int, int]],
             default_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """저장된 창 (x, y, 너비, 높이)를 지금 화면 구성에 맞춘다. 가장 많이 겹치는 화면 안으로 옮기고, 어느 화면과도 겹치지
    않으면(모니터를 뺐거나 해상도가 바뀜) 첫 화면 가운데에 둔다. 화면보다 크면 줄인다. screens는 작업 영역(작업 표시줄 제외)."""
    if not screens:
        return tuple(rect) if rect and len(rect) == 4 else (0, 0, *default_size)
    if rect and len(rect) == 4:
        x, y, w, h = (int(v) for v in rect)
    else:
        x, y, w, h = None, None, *default_size

    def overlap(screen):
        sx, sy, sw, sh = screen
        return max(0, min(x + w, sx + sw) - max(x, sx)) * max(0, min(y + h, sy + sh) - max(y, sy))

    best = max(screens, key=overlap) if x is not None else screens[0]
    if x is None or overlap(best) < 48 * 48:
        best = screens[0]
        sx, sy, sw, sh = best
        w, h = min(w, sw), min(h, sh - TITLE_BAR)
        return sx + (sw - w) // 2, sy + TITLE_BAR + (sh - TITLE_BAR - h) // 2, w, h
    sx, sy, sw, sh = best
    w, h = min(w, sw), min(h, sh - TITLE_BAR)
    x = min(max(x, sx), sx + sw - w)
    y = min(max(y, sy + TITLE_BAR), sy + sh - h)
    return x, y, w, h
