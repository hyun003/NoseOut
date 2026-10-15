"""관리자 권한 도우미: 고른 NTFS 볼륨의 MFT 열거 결과만 NoseOut 앱에 보낸다 (2026-10-15).

NoseOut이 '빠른 NTFS' 등록 때만 UAC(runas)로 띄운다. 하는 일은 이것뿐이다:
1. 인자 검사: 드라이브 문자 하나(로컬 고정·이동식 NTFS), 앱이 만든 파이프 이름, 인증 키. 다른 인자·명령은 받지 않는다.
2. 앱의 파이프에 인증해 연결한다(multiprocessing.connection의 HMAC 인증). 앱에서 명령을 받지 않는다.
3. 볼륨을 읽기 전용으로 열어 FSCTL_ENUM_USN_DATA 버퍼(이름·참조 번호·속성)를 그대로 보내고 끝난다.
파일 내용을 읽거나 파일을 쓰지 않고, AI 분석·임의 명령 실행을 하지 않는다. 표준 라이브러리만 쓴다(소스 실행에서 이 파일만 단독 실행).
"""

from __future__ import annotations

import ctypes
import re
import struct
import sys
from ctypes import wintypes

FSCTL_ENUM_USN_DATA = 0x000900B3
ERROR_HANDLE_EOF = 38
PIPE = re.compile(r"^\\\\\.\\pipe\\NoseOut-[0-9a-f]{32}$")
KEY = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1 << 20


def parse_args(argv: list[str]) -> tuple[str, str, bytes]:
    """--volume X --pipe \\\\.\\pipe\\NoseOut-<32hex> --key <64hex>, 이 셋만."""
    if len(argv) != 6 or argv[0::2] != ["--volume", "--pipe", "--key"]:
        raise ValueError("bad arguments")
    volume, pipe, key = argv[1], argv[3], argv[5]
    if not re.fullmatch(r"[A-Za-z]", volume) or not PIPE.match(pipe) or not KEY.match(key):
        raise ValueError("bad arguments")
    return volume.upper(), pipe, bytes.fromhex(key)


def local_ntfs(volume: str) -> bool:
    k = ctypes.windll.kernel32
    root = f"{volume}:\\"
    fs = ctypes.create_unicode_buffer(64)
    return k.GetDriveTypeW(root) in (2, 3) and bool(k.GetVolumeInformationW(root, None, 0, None, None, None, fs, 64)) \
        and fs.value.upper() == "NTFS"


def enumerate_volume(volume: str, send) -> None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE]
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    # GENERIC_READ, 공유 읽기·쓰기·삭제, OPEN_EXISTING: 볼륨을 잠그거나 쓰지 않는다
    handle = k.CreateFileW(f"\\\\.\\{volume}:", 0x80000000, 7, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        send(b"ERR" + str(ctypes.get_last_error()).encode())
        return
    try:
        send(b"OK")
        out = ctypes.create_string_buffer(CHUNK)
        got = wintypes.DWORD()
        start = 0
        while True:
            inbuf = struct.pack("<Qqq", start, 0, 0x7FFFFFFFFFFFFFFF)     # MFT_ENUM_DATA_V0: 모든 레코드
            if not k.DeviceIoControl(handle, FSCTL_ENUM_USN_DATA, inbuf, len(inbuf), out, CHUNK, ctypes.byref(got),
                                     None):
                err = ctypes.get_last_error()
                send(b"" if err == ERROR_HANDLE_EOF else b"ERR" + str(err).encode())
                return
            data = out.raw[:got.value]
            if len(data) <= 8:
                send(b"")
                return
            send(data)
            start = struct.unpack_from("<Q", data, 0)[0]
    finally:
        k.CloseHandle(handle)


def main(argv: list[str]) -> int:
    from multiprocessing.connection import Client

    try:
        volume, pipe, key = parse_args(argv)
    except ValueError:
        return 2
    conn = Client(pipe, family="AF_PIPE", authkey=key)
    try:
        if not local_ntfs(volume):
            conn.send_bytes(b"ERR not a local NTFS volume")
            return 3
        enumerate_volume(volume, conn.send_bytes)
    except (BrokenPipeError, EOFError, OSError):
        return 4               # 앱이 중지·종료함
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
