"""전역 단축키: Win32 RegisterHotKey(추가 패키지 없음). 원본은 ⇧⌘Space(macOS); Windows의 Win+Shift+Space는 입력기
전환에 쓰이므로 기본값을 Ctrl+Shift+Space로 했다. 다른 프로그램이 이미 쓰고 있으면 등록이 실패하고, 그 사실을 알린다."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

WM_HOTKEY = 0x0312
MOD = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
        "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}
DEFAULT = "Ctrl+Shift+Space"
HOTKEY_ID = 0xD16


def bring_to_front(hwnd: int) -> None:
    """전경 제한 때문에 activateWindow만으로 뒤에 남는 창을 위로 올린다. 최상위 고정은 곧바로 해제한다."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    flags = 0x0001 | 0x0002  # SWP_NOSIZE | SWP_NOMOVE
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)
    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
    user32.SetForegroundWindow(hwnd)


def parse(text: str) -> tuple[int, int]:
    """'Ctrl+Shift+Space' → (modifiers, virtual key). 모르는 키나 수식키 없는 조합은 ValueError."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"단축키에는 Ctrl·Alt·Shift·Win 중 하나 이상과 키 하나가 필요합니다: {text}")
    modifiers = 0
    for part in parts[:-1]:
        if part not in MOD:
            raise ValueError(f"알 수 없는 수식키: {part}")
        modifiers |= MOD[part]
    key = parts[-1]
    if key in KEYS:
        vk = KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(f"알 수 없는 키: {key}")
    return modifiers, vk


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """등록에 성공하면 단축키가 눌릴 때 `pressed`를 낸다. 실패하면 `error`에 이유가 남는다."""

    pressed = Signal()

    def __init__(self, hwnd: int, text: str = DEFAULT, register=None, unregister=None):
        QObject.__init__(self)
        QAbstractNativeEventFilter.__init__(self)
        self.hwnd = hwnd
        self.text = text
        self.registered = False
        self.error: str | None = None
        self.id = HOTKEY_ID          # change()가 새 조합을 먼저 등록할 때 다른 id를 번갈아 쓴다
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._register = register or user32.RegisterHotKey
        self._unregister = unregister or user32.UnregisterHotKey

    def register(self) -> bool:
        try:
            modifiers, vk = parse(self.text)
        except ValueError as problem:
            self.error = str(problem)
            return False
        if self._register(wintypes.HWND(self.hwnd), self.id, modifiers | MOD_NOREPEAT, vk):
            self.registered = True
            self.error = None
            return True
        self.error = self._failure(self.text)
        return False

    @staticmethod
    def _failure(text: str) -> str:
        code = ctypes.get_last_error()   # 1409 = ERROR_HOTKEY_ALREADY_REGISTERED
        return (f"단축키 {text}를 등록하지 못했습니다"
                + (" (다른 프로그램이 이미 사용 중)" if code in (0, 1409) else f" (오류 {code})") + ".")

    def change(self, text: str) -> bool:
        """설정창에서 단축키 바꾸기: 새 조합을 다른 id로 먼저 등록하고, 성공했을 때만 옛 조합을 푼다. 실패하면(충돌·잘못된
        조합) 옛 단축키가 그대로 동작하고 `error`에 이유가 남는다."""
        try:
            modifiers, vk = parse(text)
        except ValueError as problem:
            self.error = str(problem)
            return False
        if self.registered and parse(self.text) == (modifiers, vk):
            self.text = text
            return True
        new_id = HOTKEY_ID + 1 if self.id == HOTKEY_ID else HOTKEY_ID
        if not self._register(wintypes.HWND(self.hwnd), new_id, modifiers | MOD_NOREPEAT, vk):
            self.error = self._failure(text)
            return False
        if self.registered:
            self._unregister(wintypes.HWND(self.hwnd), self.id)
        self.id, self.text, self.registered, self.error = new_id, text, True, None
        return True

    def unregister(self) -> None:
        if self.registered:
            self._unregister(wintypes.HWND(self.hwnd), self.id)
            self.registered = False

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt 이름)
        if event_type == b"windows_generic_MSG" and self.registered:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self.id:
                self.pressed.emit()
                return True, 0
        return False, 0
