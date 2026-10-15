"""공개용 EXE의 Microsoft VC++ 런타임 확인과 NumPy 런타임 별칭 자동 준비 (2026-10-15, 원본 DigUp에는 없음).

공개용 패키지는 Microsoft 런타임 DLL을 동봉하지 않는다(docs/binary-distribution.md). NumPy wheel은 delvewheel로
이름을 바꾼 `_internal/numpy.libs/msvcp140-<해시>.dll`을 찾는데(numpy/__init__.py가 그 폴더를 add_dll_directory로
추가할 뿐 미리 불러오지 않는다), 이 파일도 빠져 있다. 그래서 사용자가 설치한 System32의 정식 런타임을 그 이름으로
복사해 둔다. 규칙은 수동 대안인 scripts/prepare_runtime.ps1과 같다:

- 별칭은 system_name이 msvcp140.dll이고 relative_path가 _internal/numpy.libs/msvcp140-*.dll인 것만 받는다.
- 대상이 앱 폴더 밖이거나 numpy.libs 폴더가 없으면 거부한다.
- System32의 런타임 DLL 다섯 개가 모두 있고 파일 버전이 14.44 이상이어야 한다.
- 별칭이 이미 있고 내용이 다르면 덮어쓰지 않는다(그대로 쓰고 기록만 남긴다).

앱 폴더에 쓸 수 없으면(Program Files 등) 설정 폴더의 runtime\\에 복사하고 그 폴더를 os.add_dll_directory로 추가한다.
별칭 이름은 NumPy 확장 모듈이 직접 가져오는 DLL 이름이므로 추가한 폴더에서도 찾아진다.
ps1과 달리 Authenticode 서명은 확인하지 않는다: 원본은 관리자만 쓸 수 있는 System32에서만 읽고 버전을 확인한다.

NumPy보다 먼저 불려야 하므로 표준 라이브러리만 쓴다. 시스템 DLL은 읽기만 한다.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable

MANIFEST = "runtime-setup.json"
REDIST_URL = "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist"
# prepare_runtime.ps1과 같은 목록·최소 버전. Qt·Python 확장도 이름을 바꾸지 않은 이 DLL들을 System32에서 쓴다.
REQUIRED = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll")
MIN_VERSION = (14, 44)
ALIAS_SYSTEM_NAME = "msvcp140.dll"
ALIAS_PATH = re.compile(r"_internal/numpy\.libs/msvcp140-[A-Za-z0-9]+\.dll", re.IGNORECASE)

# 로그 설정 전에 실행되므로 남길 내용을 모아 두고 앱이 app.log에 옮긴다.
NOTES: list[str] = []
# add_dll_directory가 돌려준 핸들. 닫히지 않게 프로세스가 끝날 때까지 들고 있는다.
_added: list = []


class RuntimeMissing(Exception):
    """VC++ 런타임이 없거나 오래됨. 사용자가 공식 설치로 해결한다."""


class SetupRejected(Exception):
    """패키지의 별칭 목록이 규칙에 맞지 않거나 준비에 실패함."""


class _FixedFileInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "dwSignature", "dwStrucVersion", "dwFileVersionMS", "dwFileVersionLS", "dwProductVersionMS",
        "dwProductVersionLS", "dwFileFlagsMask", "dwFileFlags", "dwFileOS", "dwFileType", "dwFileSubtype",
        "dwFileDateMS", "dwFileDateLS")]


def file_version(path: Path) -> tuple[int, int, int, int] | None:
    """DLL의 고정 파일 버전(FileVersionInfo.FileMajorPart 등과 같은 값). 읽지 못하면 None."""
    from ctypes import wintypes

    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if (not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
            or not pointer.value or length.value < ctypes.sizeof(_FixedFileInfo)):
        return None
    info = _FixedFileInfo.from_address(pointer.value)
    if info.dwSignature != 0xFEEF04BD:
        return None
    return (info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF)


def system_directory() -> Path:
    # 환경 변수(SystemRoot)가 아니라 Windows가 알려 주는 실제 System32. 64비트 EXE라 WOW64 경로 바꿈도 없다.
    buffer = ctypes.create_unicode_buffer(260)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
        raise OSError("System32 경로를 알 수 없습니다.")
    return Path(buffer.value)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_new(source: Path, target: Path) -> None:
    """target이 없을 때만 만든다. 같은 폴더의 임시 이름에 쓰고 검증한 뒤 이름을 바꾼다(기존 파일은 덮어쓰지 않음)."""
    data = source.read_bytes()
    temporary = target.with_name(f"{target.name}.{os.getpid()}-{os.urandom(4).hex()}.tmp")
    try:
        with open(temporary, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != data:
            raise OSError(f"복사본 검증 실패: {target.name}")
        os.rename(temporary, target)        # Windows의 rename은 대상이 있으면 실패한다
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_aliases(app_root: Path) -> list[tuple[str, Path]]:
    """runtime-setup.json의 별칭을 검사해 (System32 이름, 앱 폴더 안 절대 경로)로 돌려준다."""
    try:
        manifest = json.loads((app_root / MANIFEST).read_text(encoding="utf-8"))
        entries = manifest.get("local_aliases") or []
    except (OSError, ValueError, AttributeError) as error:
        raise SetupRejected(f"{MANIFEST}을 읽지 못했습니다: {error}") from None
    root = app_root.resolve()
    aliases = []
    for entry in entries:
        system_name = str(entry.get("system_name", "")) if isinstance(entry, dict) else ""
        relative = str(entry.get("relative_path", "")) if isinstance(entry, dict) else ""
        if system_name.lower() != ALIAS_SYSTEM_NAME or not ALIAS_PATH.fullmatch(relative):
            raise SetupRejected(f"허용한 런타임 별칭이 아닙니다: {system_name} → {relative}")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.parent.is_dir():
            raise SetupRejected(f"앱 내부 런타임 경로가 아닙니다: {relative}")
        aliases.append((ALIAS_SYSTEM_NAME, target))
    return aliases


def check_runtime(system_dir: Path, version_of: Callable[[Path], tuple | None] = file_version) -> None:
    problems = []
    for name in REQUIRED:
        path = system_dir / name
        found = version_of(path) if path.is_file() else None
        if found is None:
            problems.append(f"{name} 없음")
        elif tuple(found[:2]) < MIN_VERSION:
            problems.append(f"{name} {'.'.join(map(str, found))}")
    if problems:
        raise RuntimeMissing(", ".join(problems))


def prepare(app_root: Path, system_dir: Path, fallback_dir: Path,
            version_of: Callable[[Path], tuple | None] = file_version,
            add_dll_directory: Callable[[str], object] = os.add_dll_directory) -> list[str]:
    """런타임을 확인하고 별칭을 준비한다. 남길 기록을 돌려준다. 실패는 RuntimeMissing·SetupRejected."""
    aliases = read_aliases(app_root)            # 패키지 검사를 먼저: 잘못된 목록은 런타임 유무와 상관없이 거부
    check_runtime(system_dir, version_of)
    # add_dll_directory는 절대 경로만 받는다(NOSEOUT_HOME을 상대 경로로 주면 WinError 87).
    fallback_dir = Path(os.path.abspath(fallback_dir))
    notes: list[str] = []
    use_fallback = False
    for system_name, target in aliases:
        source = system_dir / system_name
        if target.exists():
            if _digest(target) != _digest(source):
                notes.append(f"runtime alias differs from installed {system_name}, kept as is: {target.name}")
            continue
        try:
            _copy_new(source, target)
            notes.append(f"runtime alias prepared in app folder: {target.name}")
            continue
        except FileExistsError:
            continue                            # 동시에 시작한 다른 실행이 먼저 만들었다
        except OSError as error:
            notes.append(f"app folder not writable ({error.__class__.__name__}), using {fallback_dir}")
        # 앱 폴더에 쓸 수 없음: 사용자 설정 폴더에 같은 이름으로 둔다
        alternate = fallback_dir / target.name
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            if alternate.exists():
                if _digest(alternate) != _digest(source):
                    notes.append(f"runtime alias differs from installed {system_name}, kept as is: {alternate}")
            else:
                try:
                    _copy_new(source, alternate)
                except FileExistsError:
                    pass
        except OSError as error:
            raise SetupRejected(f"런타임 별칭을 준비하지 못했습니다: {error}") from None
        use_fallback = True
    if use_fallback:
        _added.append(add_dll_directory(str(fallback_dir)))
    return notes


def show_error(text: str) -> None:
    """콘솔 없는 EXE에서도 보이게 Windows 메시지 상자로 알린다. '예'를 누르면 Microsoft 안내 페이지를 연다."""
    MB_YESNO, MB_ICONERROR, MB_SETFOREGROUND, IDYES = 0x4, 0x10, 0x10000, 6
    answer = ctypes.windll.user32.MessageBoxW(None, text, "NoseOut", MB_YESNO | MB_ICONERROR | MB_SETFOREGROUND)
    if answer == IDYES:
        os.startfile(REDIST_URL)


def ensure_for_frozen_app(home: Path, notify: Callable[[str], None] = show_error) -> None:
    """공개용 EXE(runtime-setup.json이 있는 PyInstaller 폴더)에서만 동작한다. 개인용 EXE·소스 실행은 그대로 둔다.

    준비하지 못하면 이유를 home/startup-error.log에 남기고 메시지를 보인 뒤 SystemExit(1)."""
    if not getattr(sys, "frozen", False):
        return
    app_root = Path(sys.executable).resolve().parent
    if not (app_root / MANIFEST).is_file():
        return
    try:
        NOTES.extend(prepare(app_root, system_directory(), home / "runtime"))
        return
    except RuntimeMissing as error:
        reason = f"Microsoft Visual C++ runtime missing or older than 14.44: {error}"
        text = ("NoseOut을 실행하려면 Microsoft Visual C++ 2015-2022 x64 재배포 가능 패키지(14.44 이상)가 필요합니다.\n\n"
                f"확인 결과: {error}\n\n"
                f"Microsoft 공식 페이지에서 X64 버전을 설치한 뒤 다시 실행하세요.\n{REDIST_URL}\n\n"
                "지금 안내 페이지를 열까요?")
    except (SetupRejected, OSError) as error:
        reason = f"runtime setup failed: {error}"
        text = (f"NoseOut 실행 준비에 실패했습니다.\n\n{error}\n\n"
                "폴더 전체를 다시 압축 해제하거나, Microsoft Visual C++ x64 런타임을 설치·복구한 뒤 다시 실행하세요.\n"
                f"{REDIST_URL}\n\n지금 안내 페이지를 열까요?")
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "startup-error.log").write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass
    notify(text)
    raise SystemExit(1)
