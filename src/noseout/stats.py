"""색인 현황(설정창 '색인 현황' 탭, 검색 창 작업 줄): 이름 목록·AI 색인·코드 색인을 읽기만 해서 센다(2026-10-14).

- 색인 파일을 만들거나 바꾸지 않는다: 파일이 없으면 '아직 없음'(exists False). 읽는 연결은 query_only이고 저널 모드를 건드리지
  않는다(names.NameIndex의 읽는 쪽과 같은 이유).
- 개수는 지금 검색 범위 기준이다: 폴더를 빼거나 제외해도 색인은 보존되지만(검색에서만 숨김) 여기서는 세지 않는다.
- 화면 스레드에서 부르지 않는다(StatsService 스레드). 10만 파일이면 범위 판정에 수백 ms가 걸릴 수 있다.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

FAILURE_LIMIT = 200


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def _under(folder_key: str, path_key: str) -> bool:
    return path_key == folder_key or path_key.startswith(folder_key + os.sep)


def _wal_ready(path: Path) -> bool:
    """쓰는 쪽이 WAL로 바꾼 뒤에만 연다: 그 전(파일을 막 만드는 중)에 읽으면 읽기 잠금이 쓰는 쪽의 커밋을 막는다.
    SQLite 머리의 18번째 바이트가 2면 WAL이다. 파일 머리만 읽고 잠그지 않는다."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(100)
    except OSError:
        return False
    return len(head) == 100 and head.startswith(b"SQLite format 3\x00") and head[18] == 2


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=1)
    db.execute("PRAGMA query_only = ON")
    return db


def _size(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.stat(f"{path}{suffix}").st_size
        except OSError:
            pass
    return total


def _names(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    if not _wal_ready(path):
        return {"exists": True, "busy": True}
    with closing(_connect(path)) as db:
        try:
            files, folders = db.execute("SELECT COALESCE(SUM(is_dir = 0), 0), COALESCE(SUM(is_dir = 1), 0) "
                                        "FROM entries").fetchone()
            checked = db.execute("SELECT MAX(checked_at) FROM roots").fetchone()[0]
            trouble = db.execute("SELECT COUNT(*) FROM trouble").fetchone()[0]
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):      # 등록 스레드가 막 만드는 중
                return {"exists": False}
            raise
    return {"exists": True, "files": files, "folders": folders, "checked_at": checked, "trouble": trouble}


def _ai(path: Path, roots: list[str], exclusions: list[str], extra: list[str], failures: bool) -> dict:
    out = {"exists": path.exists(), "done": 0, "partial": 0, "pending": 0, "failed": 0, "skipped": 0,
           "last": None, "unchecked_roots": 0, "failures": []}
    if not out["exists"]:
        out["unchecked_roots"] = len(roots)
        return out
    if not _wal_ready(path):
        out["busy"] = True
        return out
    extra_keys = {_key(f) for f in extra}
    root_keys = [_key(r) for r in roots]
    excluded_keys = [_key(e) for e in exclusions]
    seen_roots: set[int] = set()
    with closing(_connect(path)) as db:
        try:
            rows = db.execute("SELECT path, state, detail, indexed_at FROM files").fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                out["exists"] = False
                out["unchecked_roots"] = len(roots)
                return out
            raise
    for path_text, state, detail, indexed_at in rows:
        key = os.path.normcase(path_text)      # 색인의 경로는 이미 절대 경로(scope.contains와 같은 판정)
        inside = [i for i, root in enumerate(root_keys) if _under(root, key)]
        seen_roots.update(inside)
        if not (inside and not any(_under(e, key) for e in excluded_keys)) and key not in extra_keys:
            continue
        if state in out and state not in ("failures", "last"):
            out[state] += 1
        if indexed_at and (out["last"] is None or indexed_at > out["last"]):
            out["last"] = indexed_at
        if state == "failed" and failures and len(out["failures"]) < FAILURE_LIMIT:
            out["failures"].append((path_text, detail or ""))
    # 고른 폴더 중 색인에 파일이 하나도 없는 곳: 아직 목록을 확인하지 않았다(빈 폴더일 수도 있다). 숫자를 짐작하지 않는다.
    out["unchecked_roots"] = sum(1 for i in range(len(roots)) if i not in seen_roots)
    return out


def collect(base: Path, folders: list[str], code_folders: list[str], extra_files: list[str],
            exclusions: list[str], failures: bool = True) -> dict:
    """설정 폴더(base)의 index.sqlite·code.sqlite·names.sqlite를 읽기만 해서 센다. 읽지 못한 색인은 그 칸에 'error'."""
    started = time.perf_counter()
    base = Path(base)
    out: dict = {}
    for key, work in (("names", lambda: _names(base / "names.sqlite")),
                      ("ai", lambda: _ai(base / "index.sqlite", list(folders), list(exclusions), list(extra_files),
                                         failures)),
                      ("code", lambda: _ai(base / "code.sqlite", list(code_folders), list(exclusions), [], False))):
        try:
            out[key] = work()
        except sqlite3.Error as error:
            out[key] = {"exists": True, "error": str(error)}
    logs = sum(_size(p) for p in base.glob("app.log*")) if base.exists() else 0
    out["storage"] = {"index": _size(base / "index.sqlite"), "code": _size(base / "code.sqlite"),
                      "names": _size(base / "names.sqlite"), "logs": logs}
    out["storage"]["total"] = sum(out["storage"].values())
    out["seconds"] = time.perf_counter() - started
    return out


def size_text(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB" if size else "0 KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.2f} GB"


def time_text(stamp: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "아직 없음"
