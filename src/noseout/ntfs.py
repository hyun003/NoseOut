"""NTFS 이름 목록: MFT 목록 + USN 변경 기록 (2026-10-15, 원본 DigUp에는 없음). 이름·경로 검색만 쓴다. AI 내용 분석은 그대로다.

실측(일반 권한, 2026-10-15): 볼륨 핸들(\\\\.\\C:)은 읽기로 열 수 없고 MFT 열거(FSCTL_ENUM_USN_DATA)·일반 저널 읽기는 거부(5)된다.
루트 폴더 핸들(C:\\, 접근 권한 0)로는 저널 조회(FSCTL_QUERY_USN_JOURNAL)와 비권한 저널 읽기(FSCTL_READ_UNPRIVILEGED_USN_JOURNAL)가
된다. 비권한 읽기는 이름을 주지 않고 파일·부모 참조 번호와 변경 이유만 준다. 그래서:

- 처음 목록(또는 저널을 잃은 뒤 다시 등록): '빠른 NTFS'를 사용자가 고르고 등록을 누를 때만 관리자 도우미(ntfs_helper.py, UAC 1회)가
  MFT를 열거한다. 도우미는 고른 볼륨의 열거 결과(이름·참조 번호·속성)만 인증된 파이프로 넘기고 끝난다. 그 밖에는 폴더 순회(names.py).
- 변경 반영(재실행 따라잡기, 실행 중): 일반 권한으로 저널을 읽어 바뀐 항목의 부모 폴더를 참조 번호로 열어(OpenFileById, 내용은 읽지 않음)
  경로를 알아내고, 그 폴더의 직접 목록만 다시 읽는다(names.NameIndex.refresh_folders). 자동 시작 때 UAC가 필요 없다.
- 체크포인트(처리한 USN)는 그 변경이 names.sqlite에 커밋된 뒤에만 옮긴다. 볼륨 GUID·일련번호·저널 ID가 바뀌었거나 저장한 위치의 기록이
  지워졌으면(저널 유실·초기화·볼륨 교체·드라이브 문자 변경) 그 볼륨의 범위만 다시 등록한다. 등록이 끝나기 전에는 완료로 표시하지 않는다.
- MFT는 사용자가 열 수 없는 폴더의 이름까지 보여 준다. 폴더 순회와 같은 결과가 되게, 들어갈 폴더는 사용자 권한으로 목록을 열어 본다
  (보안 설명자 ID별로 한 번). 하드링크의 두 번째 이름은 MFT 열거에 나오지 않는다(설정의 '폴더 순회로 전체 확인'으로 맞춘다).
"""

from __future__ import annotations

import ctypes
import logging
import os
import secrets
import stat
import struct
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Iterator, NamedTuple

from . import scope

log = logging.getLogger("noseout")

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_READ_UNPRIVILEGED_USN_JOURNAL = 0x000903AB
ERROR_HANDLE_EOF = 38
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_ENTRY_DELETED = 1181
ERROR_CANCELLED = 1223
FILE_ATTRIBUTE_DIRECTORY = 0x10
ROOT_RECORD = 5                          # NTFS 루트 폴더의 MFT 레코드 번호
RECORD_MASK = (1 << 48) - 1              # 참조 번호 = 레코드 번호(48비트) + 순번(16비트)

USN_REASON_FILE_CREATE = 0x100
USN_REASON_FILE_DELETE = 0x200
USN_REASON_RENAME_OLD_NAME = 0x1000
USN_REASON_RENAME_NEW_NAME = 0x2000
USN_REASON_BASIC_INFO_CHANGE = 0x8000
USN_REASON_HARD_LINK_CHANGE = 0x10000
USN_REASON_REPARSE_POINT_CHANGE = 0x100000
# 이름 목록에 필요한 변경만(생성·삭제·이름 변경·이동·하드링크·숨김 등 속성·링크). 내용 쓰기는 이름이 그대로라 받지 않는다
# (크기·수정 시각은 화면에 쓰지 않고, 그 폴더가 다시 읽힐 때 갱신된다).
NAME_REASONS = (USN_REASON_FILE_CREATE | USN_REASON_FILE_DELETE | USN_REASON_RENAME_OLD_NAME | USN_REASON_RENAME_NEW_NAME
                | USN_REASON_BASIC_INFO_CHANGE | USN_REASON_HARD_LINK_CHANGE | USN_REASON_REPARSE_POINT_CHANGE)
STRUCTURE = USN_REASON_FILE_DELETE | USN_REASON_RENAME_OLD_NAME | USN_REASON_RENAME_NEW_NAME
METHODS = ("auto", "ntfs", "walk")        # 설정: 자동 / 빠른 NTFS / 폴더 순회
BATCH_SECONDS = 0.5


class Record(NamedTuple):
    frn: int
    parent: int
    usn: int
    reason: int
    attrs: int
    name: str
    security_id: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_DIRECTORY)


class JournalInfo(NamedTuple):
    journal_id: int
    first_usn: int
    next_usn: int


class Unsupported(Exception):
    """이 경로·볼륨에서는 USN을 쓸 수 없다(네트워크, NTFS 아님, 저널 꺼짐 등): 폴더 순회로."""


class JournalGap(Exception):
    """저장한 위치 뒤의 기록이 지워졌거나 저널이 바뀌었다: 그 볼륨의 범위를 다시 등록한다."""


class HelperDenied(Exception):
    """관리자 권한 요청을 사용자가 거부했다(UAC 취소)."""


class HelperFailed(Exception):
    pass


# USN 레코드 해석 (V2: 64비트 참조 번호, V3: 128비트)


def parse_records(buffer: bytes, offset: int = 8) -> list[Record]:
    out = []
    pos = offset
    while pos + 8 <= len(buffer):
        length, major = struct.unpack_from("<IH", buffer, pos)
        if length < 8 or pos + length > len(buffer):
            break
        if major == 2 and length >= 60:
            frn, parent, usn, _ts, reason, _src, sec, attrs, name_len, name_off = \
                struct.unpack_from("<QQqqIIIIHH", buffer, pos + 8)
        elif major == 3 and length >= 76:
            lo, hi, plo, phi, usn, _ts, reason, _src, sec, attrs, name_len, name_off = \
                struct.unpack_from("<QQQQqqIIIIHH", buffer, pos + 8)
            frn, parent = lo | hi << 64, plo | phi << 64
        else:
            pos += length
            continue
        name = buffer[pos + name_off:pos + name_off + name_len].decode("utf-16-le", "replace") if name_len else ""
        out.append(Record(frn, parent, usn, reason, attrs, name, sec))
        pos += length
    return out


def pack_record(frn: int, parent: int, name: str = "", attrs: int = 0, reason: int = 0, usn: int = 0,
                security_id: int = 0) -> bytes:
    """USN_RECORD_V2 한 개(시험용 합성 레코드, 가짜 도우미)."""
    raw = name.encode("utf-16-le")
    length = (60 + len(raw) + 7) // 8 * 8
    head = struct.pack("<IHHQQqqIIIIHH", length, 2, 0, frn, parent, usn, 0, reason, 0, security_id, attrs, len(raw), 60)
    return (head + raw).ljust(length, b"\0")


# Windows API


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE]
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k.OpenFileById.restype = wintypes.HANDLE
    k.OpenFileById.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD]
    k.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


INVALID_HANDLE = wintypes.HANDLE(-1).value
SHARE_ALL = 7
BACKUP_SEMANTICS = 0x02000000
FILE_READ_ATTRIBUTES = 0x80
FILE_LIST_DIRECTORY = 0x1


class _FileId(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("type", wintypes.DWORD), ("id", ctypes.c_ubyte * 16)]


def volume_of(path: str) -> tuple[str | None, str]:
    """(드라이브 루트 'C:\\' 또는 None, 이유). 로컬 고정·이동식 NTFS 드라이브 문자만. 네트워크·UNC·NTFS 아님·폴더에 붙인
    볼륨은 None(폴더 순회)."""
    if sys.platform != "win32":
        return None, "Windows가 아님"
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return None, "네트워크 경로"
    k = _kernel32()
    buf = ctypes.create_unicode_buffer(1024)
    if not k.GetVolumePathNameW(path, buf, 1024):
        return None, "볼륨을 확인하지 못함"
    mount = buf.value
    if len(mount) != 3 or mount[1:] != ":\\":
        return None, "폴더에 연결한 볼륨"
    kind = k.GetDriveTypeW(mount)
    if kind not in (2, 3):                    # DRIVE_REMOVABLE, DRIVE_FIXED
        return None, "네트워크·광학 드라이브"
    fs = ctypes.create_unicode_buffer(64)
    if not k.GetVolumeInformationW(mount, None, 0, None, None, None, fs, 64):
        return None, "볼륨 정보를 읽지 못함"
    if fs.value.upper() != "NTFS":
        return None, f"{fs.value or '알 수 없는'} 파일 시스템"
    return mount.upper(), ""


class Volume:
    """일반 권한으로 쓰는 볼륨 연결: 저널 조회·비권한 읽기·참조 번호로 경로 확인. 파일 내용은 열지 않는다."""

    def __init__(self, root: str):
        self.root = root
        self.k = _kernel32()
        self.handle = self.k.CreateFileW(root, 0, SHARE_ALL, None, 3, BACKUP_SEMANTICS, None)
        if self.handle == INVALID_HANDLE:
            raise Unsupported(f"{root} 루트를 열지 못함 (오류 {ctypes.get_last_error()})")

    def close(self) -> None:
        if self.handle not in (None, INVALID_HANDLE):
            self.k.CloseHandle(self.handle)
            self.handle = None

    def identity(self) -> tuple[str, int]:
        guid = ctypes.create_unicode_buffer(64)
        self.k.GetVolumeNameForVolumeMountPointW(self.root, guid, 64)
        serial = wintypes.DWORD()
        self.k.GetVolumeInformationW(self.root, None, 0, ctypes.byref(serial), None, None, None, 0)
        return guid.value, serial.value

    def _ioctl(self, code: int, inbuf: bytes | None, size: int) -> tuple[bool, int, bytes]:
        out = ctypes.create_string_buffer(size)
        got = wintypes.DWORD()
        ok = self.k.DeviceIoControl(self.handle, code, inbuf, len(inbuf) if inbuf else 0, out, size, ctypes.byref(got),
                                    None)
        return bool(ok), ctypes.get_last_error(), out.raw[:got.value]

    def query(self) -> JournalInfo:
        ok, err, out = self._ioctl(FSCTL_QUERY_USN_JOURNAL, None, 80)
        if not ok:
            raise Unsupported("변경 기록(USN 저널)이 꺼져 있음" if err == ERROR_JOURNAL_NOT_ACTIVE
                              else f"변경 기록을 조회하지 못함 (오류 {err})")
        journal_id, first, nxt = struct.unpack_from("<QqQ", out, 0)
        return JournalInfo(journal_id, first, nxt)

    def read(self, start: int, journal_id: int, mask: int = NAME_REASONS) -> tuple[int, list[Record]]:
        """start부터 지금까지(한 번에 64KB). (다음 위치, 레코드). 기록이 지워졌거나 저널이 바뀌었으면 JournalGap."""
        inbuf = struct.pack("<qIIQQQHH4x", start, mask, 0, 0, 0, journal_id, 2, 3)
        ok, err, out = self._ioctl(FSCTL_READ_UNPRIVILEGED_USN_JOURNAL, inbuf, 65536)
        if not ok:
            if err in (ERROR_JOURNAL_ENTRY_DELETED, ERROR_JOURNAL_DELETE_IN_PROGRESS, ERROR_JOURNAL_NOT_ACTIVE, 87):
                raise JournalGap(f"변경 기록을 이어 읽지 못함 (오류 {err})")
            raise OSError(err, f"USN read failed ({err})")
        if len(out) < 8:
            return start, []
        return struct.unpack_from("<q", out, 0)[0], parse_records(out)

    def path_of(self, frn: int) -> str | None:
        """참조 번호의 지금 경로. 없으면 None, 권한이 없으면 PermissionError. 속성 읽기로 열고, 거부되면 목록 읽기로 한 번 더
        연다(폴더 순회와 같은 기준: 목록을 볼 수 있는 폴더면 경로를 얻는다). 내용은 열지 않는다."""
        file_id = _FileId(ctypes.sizeof(_FileId), 0 if frn < 1 << 64 else 2)
        ctypes.memmove(file_id.id, frn.to_bytes(16, "little"), 16)
        handle = INVALID_HANDLE
        for access in (FILE_READ_ATTRIBUTES, FILE_LIST_DIRECTORY):
            handle = self.k.OpenFileById(self.handle, ctypes.byref(file_id), access, SHARE_ALL, None, BACKUP_SEMANTICS)
            if handle != INVALID_HANDLE:
                break
            err = ctypes.get_last_error()
            if err != 5:
                return None
        if handle == INVALID_HANDLE:
            raise PermissionError(5, "access denied")
        try:
            buf = ctypes.create_unicode_buffer(32768)
            got = self.k.GetFinalPathNameByHandleW(handle, buf, 32768, 0)
            if not got or got >= 32768:
                return None
            path = buf.value
        finally:
            self.k.CloseHandle(handle)
        return path[4:] if path.startswith("\\\\?\\") and not path.startswith("\\\\?\\UNC\\") else path

    def frn_of(self, path: str) -> int | None:
        """경로의 참조 번호(접근 권한 0으로 열어 파일 정보만). 목록을 볼 수 없는 범위 안 폴더도 대개 열린다."""
        handle = self.k.CreateFileW(path, 0, SHARE_ALL, None, 3, BACKUP_SEMANTICS, None)
        if handle == INVALID_HANDLE:
            return None
        try:
            info = _FileInfo()
            if not ctypes.windll.kernel32.GetFileInformationByHandle(wintypes.HANDLE(handle), ctypes.byref(info)):
                return None
            return info.index_high << 32 | info.index_low
        finally:
            self.k.CloseHandle(handle)


class _FileInfo(ctypes.Structure):
    _fields_ = [("attrs", wintypes.DWORD), ("created", wintypes.FILETIME), ("accessed", wintypes.FILETIME),
                ("written", wintypes.FILETIME), ("serial", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD)]


# 관리자 도우미 (MFT 열거)


class Helper:
    """'빠른 NTFS'를 고르고 등록할 때만 UAC로 ntfs_helper를 띄운다. 도우미는 그 볼륨의 열거 버퍼만 보낸다(인증된 파이프)."""

    def __init__(self, command: Callable[[], tuple[str, list[str]]] | None = None, timeout: float = 120.0):
        self.command = command or helper_command
        self.timeout = timeout

    def enumerate(self, root: str, should_stop: Callable[[], bool] = lambda: False) -> Iterator[list[Record]]:
        from multiprocessing.connection import Client, Listener

        key = secrets.token_bytes(32)
        name = f"\\\\.\\pipe\\NoseOut-{secrets.token_hex(16)}"
        listener = Listener(name, family="AF_PIPE", authkey=key)
        exe, args = self.command()
        args = args + ["--volume", root[0], "--pipe", name, "--key", key.hex()]
        process = _shell_execute_admin(exe, args)
        exited = threading.Event()

        def watch() -> None:
            # 도우미가 연결 전에 끝나면(실패) accept가 영원히 기다리지 않게 빈 연결로 깨운다
            _wait(process, int(self.timeout * 1000))
            exited.set()
            try:
                Client(name, family="AF_PIPE", authkey=key).close()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=watch, daemon=True).start()
        try:
            conn = listener.accept()
        finally:
            listener.close()
        try:
            try:
                first = conn.recv_bytes()
            except (EOFError, OSError):
                raise HelperFailed("관리자 도우미가 연결하지 못했습니다.") from None
            if first.startswith(b"ERR"):
                raise HelperFailed(f"관리자 도우미 오류: {first[3:].decode(errors='replace')}")
            while True:
                if should_stop():
                    return
                chunk = conn.recv_bytes()
                if not chunk:
                    return
                if chunk.startswith(b"ERR"):
                    raise HelperFailed(f"MFT를 읽지 못했습니다: {chunk[3:].decode(errors='replace')}")
                yield parse_records(chunk)
        except EOFError:
            raise HelperFailed("관리자 도우미가 도중에 끝났습니다.") from None
        finally:
            conn.close()
            _close(process)


def _wait(process, ms: int) -> None:
    ctypes.windll.kernel32.WaitForSingleObject(process, ms)


def _close(process) -> None:
    ctypes.windll.kernel32.CloseHandle(process)


def helper_command() -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        return sys.executable, ["--ntfs-helper"]
    return sys.executable, [str(Path(__file__).with_name("ntfs_helper.py"))]


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def _shell_execute_admin(exe: str, args: list[str]) -> int:
    import subprocess

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100           # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.nShow = 0                      # SW_HIDE
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise HelperDenied("관리자 권한 요청이 거부되었습니다.")
        raise HelperFailed(f"관리자 도우미를 시작하지 못했습니다 (오류 {err}).")
    return info.hProcess


# 볼륨별 이름 목록 맞추기


@dataclass
class VolumeStatus:
    method: str                 # "usn" | "walk"
    detail: str = ""            # 사용자에게 보일 이유 (권한 거부, 저널 유실 등)
    registered_by: str = ""     # "mft" | "walk" (마지막 전체 등록 방법)
    unresolved: int = 0         # 범위 안의 열 수 없는 폴더에서 생긴 변경 수 (그 폴더는 결과에 '상태 미확인')
    outside: int = 0            # 경로를 알 수 없지만 범위 안 폴더가 아닌 곳의 변경 (오류로 보이지 않는다)


@dataclass
class SyncResult:
    walk_roots: list[str] = field(default_factory=list)       # 폴더 순회로 맞출 범위
    statuses: dict[str, VolumeStatus] = field(default_factory=dict)


class NtfsNames:
    """NameIndex 위에서 볼륨 단위로 등록·따라잡기·실행 중 반영을 한다. volume_factory·helper는 시험에서 가짜로 바꾼다."""

    def __init__(self, index, volume_factory: Callable[[str], Volume] = Volume,
                 volume_of: Callable[[str], tuple[str | None, str]] = volume_of, helper: Helper | None = None):
        self.index = index
        self.factory = volume_factory
        self.volume_of = volume_of
        self.helper = helper
        self.volumes: dict[str, Volume] = {}
        self.statuses: dict[str, VolumeStatus] = {}
        self._paths: dict[int, str | None] = {}
        self._denied: set[int] = set()           # 목록도 볼 수 없어 경로를 얻지 못한 폴더 (같은 폴더를 거듭 열지 않게)

    def close(self) -> None:
        for volume in self.volumes.values():
            volume.close()
        self.volumes.clear()

    def _volume(self, root: str) -> Volume:
        if root not in self.volumes:
            self.volumes[root] = self.factory(root)
        return self.volumes[root]

    def groups(self, method: str) -> tuple[dict[str, list[str]], list[str]]:
        from .names import outer_roots

        groups: dict[str, list[str]] = {}
        walk: list[str] = []
        for root in outer_roots(self.index.roots):
            volume, reason = (None, "폴더 순회 선택") if method == "walk" else self.volume_of(root)
            if volume is None:
                walk.append(root)
                self.statuses[root] = VolumeStatus("walk", reason)
            else:
                groups.setdefault(volume, []).append(root)
        return groups, walk

    def sync(self, method: str, report, allow_prompt: bool = False, should_stop: Callable[[], bool] = lambda: False,
             progress: Callable[[int, str], None] | None = None, walk: Callable[[list[str]], None] | None = None,
             force: bool = False) -> SyncResult:
        """앱 시작·'다시 등록'·범위 변경. USN을 쓸 수 있는 볼륨은 저장한 위치부터 따라잡고, 처음이거나 기록을 잃었으면 그 볼륨의
        범위만 다시 등록한다(빠른 NTFS + 사용자 요청이면 MFT, 아니면 폴더 순회 `walk`). force: 저장 상태를 무시하고 다시 등록."""
        result = SyncResult()
        groups, result.walk_roots = self.groups(method)
        for volume_root, roots in groups.items():
            if should_stop():
                report.stopped = True
                break
            try:
                volume = self._volume(volume_root)
                info = volume.query()
                guid, serial = volume.identity()
            except (Unsupported, OSError) as error:
                result.walk_roots += roots
                for root in roots:
                    self.statuses[root] = VolumeStatus("walk", str(error))
                self.index.forget_volume(volume_root)
                continue
            state = self.index.volume_state(volume_root)
            detail = ""
            registered: set[str] = set()
            if state and not force:
                if (state["guid"], state["serial"]) != (guid, serial):
                    detail = "볼륨이 바뀜(교체·드라이브 문자 변경): 다시 등록"
                elif state["journal_id"] != info.journal_id:
                    detail = "변경 기록이 초기화됨: 다시 등록"
                elif state["usn"] < info.first_usn:
                    detail = "앱이 꺼진 동안의 변경 기록이 지워짐: 다시 등록"
                elif not state["complete"]:
                    detail = "지난 등록이 끝나지 않음: 다시 등록"
                else:
                    registered = set(state["roots"])
            keys = {os.path.normcase(r): r for r in roots}
            missing = [r for k, r in keys.items() if k not in registered]
            # 범위별 등록 방법·안내: 이미 등록한 범위는 저장한 값을 그대로, 이번에 등록하는 범위만 새로 정한다
            info_map = {k: v for k, v in (state["root_info"] if state else {}).items() if k in registered}
            usn = state["usn"] if registered else None
            if registered:
                # 저장한 이름 목록으로 이미 찾을 수 있다: 꺼져 있던 동안의 변경만 뒤에서 반영한다
                try:
                    usn = self.catch_up(volume_root, info.journal_id, usn, report, should_stop)
                except JournalGap as gap:
                    detail, registered, missing, usn = f"{gap}: 다시 등록", set(), list(keys.values()), None
                    info_map = {}
            if report.stopped:
                break
            by = state["registered_by"] if state else ""
            if missing:
                start = volume.query().next_usn       # 등록 중 바뀐 것도 놓치지 않게 시작 전 위치부터 이어 읽는다
                self.index.set_volume(volume_root, guid, serial, info.journal_id, usn if registered else start,
                                      complete=False, roots=sorted(registered), registered_by=by, detail=detail,
                                      root_info=info_map)
                for root in missing:
                    self.statuses.pop(root, None)
                by = self._register(volume_root, missing, method, allow_prompt, report, should_stop, progress, walk,
                                    detail)
                for root in missing:
                    note = self.statuses[root].detail if root in self.statuses else ""
                    info_map[os.path.normcase(root)] = {"by": by, "note": note or detail}
                if report.stopped:
                    self.statuses.update({r: VolumeStatus("usn", "등록 중지: 다음에 다시 등록", by) for r in missing})
                    continue          # 완료로 표시하지 않는다
                try:
                    usn = self.catch_up(volume_root, info.journal_id, usn if registered else start, report, should_stop)
                except JournalGap as gap:
                    # 등록하는 사이 기록을 잃었다(아주 큰 변경): 다음에 다시 등록
                    self.statuses.update({r: VolumeStatus("usn", f"{gap}: 다음에 다시 등록", by) for r in roots})
                    continue
                if report.stopped:
                    continue
            self.index.set_volume(volume_root, guid, serial, info.journal_id, usn, complete=True, roots=sorted(keys),
                                  registered_by=by, detail=detail, root_info=info_map)
            for key, root in keys.items():
                entry = info_map.get(key, {"by": by, "note": ""})
                old = self.statuses.get(root, VolumeStatus("usn"))
                self.statuses[root] = VolumeStatus("usn", entry["note"], entry["by"], old.unresolved, old.outside)
        return result

    def _register(self, volume_root, roots, method, allow_prompt, report, should_stop, progress, walk, detail) -> str:
        if method == "ntfs" and allow_prompt and self.helper is not None:
            try:
                records = self.helper.enumerate(volume_root, should_stop)
                self.index.import_mft(volume_root, roots, records, report, should_stop, progress)
                return "mft"
            except HelperDenied as error:
                note = f"{error} 폴더 순회로 등록했습니다."
            except (HelperFailed, OSError) as error:
                log.warning("MFT registration failed: %s", error)
                note = f"{error} 폴더 순회로 등록했습니다."
            for root in roots:
                self.statuses[root] = VolumeStatus("usn", note, "walk")
        elif method == "ntfs":
            for root in roots:
                # 앱 시작·범위 추가처럼 사용자가 등록을 누르지 않은 때: UAC 없이 폴더 순회로 ('다시 등록'을 누르면 MFT)
                self.statuses[root] = VolumeStatus("usn", "권한 요청 없이 폴더 순회로 등록함('다시 등록'을 누르면 MFT)", "walk")
        if walk is not None:
            walk(roots)
        return "walk"

    def _resolve(self, volume: Volume, frn: int) -> str | None:
        if frn in self._denied:
            raise PermissionError(5, "access denied")
        if frn not in self._paths:
            try:
                self._paths[frn] = volume.path_of(frn)
            except PermissionError:
                self._denied.add(frn)
                raise
        return self._paths[frn]

    def _closed_folders(self, volume: Volume) -> dict[int, str]:
        """범위 안의 열 수 없는 폴더(고른 범위 자체와 '상태 미확인'으로 남은 폴더)의 참조 번호 → 그 범위. 경로를 얻지 못한 변경이
        이 폴더들에서 왔을 때만 범위 안의 미확인으로 센다(2026-10-15: 실제 미확인 기록 1,302건·폴더 19곳이 모두 범위 밖이었다)."""
        frn_of = getattr(volume, "frn_of", None)
        if frn_of is None:
            return {}
        out: dict[int, str] = {}
        candidates = list(self.index.roots) + [p for (p,) in self.index.db.execute("SELECT path FROM trouble")]
        for path in candidates:
            owner = self.index._owner(path)
            if owner is None:
                continue
            frn = frn_of(path)
            if frn is not None:
                out[frn & ((1 << 64) - 1)] = owner
        return out

    def catch_up(self, volume_root: str, journal_id: int, usn: int, report, should_stop=lambda: False) -> int:
        """usn부터 지금까지의 변경을 반영하고, 커밋한 뒤 새 위치를 저장한다. 반영한 폴더만 다시 읽는다."""
        volume = self._volume(volume_root)
        roots = list(self.index.roots)
        folders: dict[str, str] = {}
        whole: dict[str, str] = {}
        denied: dict[int, int] = {}              # 경로를 얻지 못한 부모 폴더 → 변경 수
        started = time.perf_counter()
        while True:
            if should_stop():
                report.stopped = True
                break
            nxt, records = volume.read(usn, journal_id)
            for record in records:
                if record.is_dir and record.reason & STRUCTURE:
                    self._paths.clear()          # 폴더가 옮겨지거나 지워짐: 기억한 경로가 낡았다
                try:
                    parent = self._resolve(volume, record.parent)
                except PermissionError:
                    denied[record.parent] = denied.get(record.parent, 0) + 1
                    continue
                if parent is None:
                    continue                     # 부모도 지워짐: 그 삭제 기록이 위 폴더에서 반영된다
                key = os.path.normcase(parent)
                if self.index.allowed(parent) or any(key == os.path.normcase(r) for r in roots):
                    folders[key] = parent
                else:
                    for root in roots:          # 고른 범위의 상위 폴더가 바뀜(범위 자체의 이동·삭제)
                        if scope.contains(parent, root):
                            whole[os.path.normcase(root)] = root
            done = nxt == usn or not records
            usn = nxt
            if folders or whole or done or time.perf_counter() - started > BATCH_SECONDS:
                self.index.refresh_folders(list(folders.values()), report, whole=list(whole.values()))
                self.index.set_checkpoint(volume_root, usn)     # 반영을 커밋한 뒤에만 위치를 옮긴다
                folders, whole, started = {}, {}, time.perf_counter()
            if done:
                break
        if denied:
            closed = self._closed_folders(volume)
            for frn, count in denied.items():
                owner = closed.get(frn & ((1 << 64) - 1))
                if owner is not None:
                    self.statuses.setdefault(owner, VolumeStatus("usn")).unresolved += count
                else:
                    # 범위 밖(사용자가 목록을 볼 수 없는 시스템·다른 계정 폴더 등): 이름 목록과 무관하다. 로그에만 남긴다.
                    for root in roots:
                        if root in self.statuses:
                            self.statuses[root].outside += count
                            break
                    log.debug("USN changes outside the name scope without a readable parent: %d", count)
        return usn

    def poll(self, report, should_stop=lambda: False) -> list[str]:
        """실행 중 반영(약 1초마다). 완료된 볼륨만. 기록을 잃은 볼륨은 돌려준다(앱이 그 범위를 폴더 순회로 다시 등록)."""
        lost = []
        for state in self.index.volume_states():
            if not state["complete"]:
                continue
            try:
                self.catch_up(state["root"], state["journal_id"], state["usn"], report, should_stop)
            except JournalGap as gap:
                self.index.mark_incomplete(state["root"], f"{gap}: 다시 등록")
                lost.append(state["root"])
            except (Unsupported, OSError) as error:
                log.warning("USN poll failed for %s: %s", state["root"], error)
                self.volumes.pop(state["root"], None)
        return lost


def usn_roots(states: Iterable[dict]) -> list[str]:
    """USN으로 맞추고 있는(완료된) 범위: 기존 폴더 감시가 이 범위에 감시 핸들을 따로 열지 않게."""
    return [root for s in states if s["complete"] for root in s["roots"]]


def fake_stat(attrs: int, is_dir: bool) -> SimpleNamespace:
    """MFT 레코드의 속성으로 rules의 판정(숨김·시스템·재분석 지점)을 폴더 순회와 같게 하려는 stat 대용."""
    return SimpleNamespace(st_mode=stat.S_IFDIR if is_dir else stat.S_IFREG, st_file_attributes=attrs,
                           st_reparse_tag=0)
