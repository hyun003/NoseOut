"""설정·색인 폴더 위치와 옛 이름(DigUpWin)에서의 이전 (2026-10-15 NoseOut 이름 통일, 원본 DigUp에는 없음).

순서: NOSEOUT_HOME > DIGUPWIN_HOME(옛 환경 변수, 경고) > %LOCALAPPDATA%\\NoseOut.
%LOCALAPPDATA%\\NoseOut이 없고 옛 %LOCALAPPDATA%\\DigUpWin만 있으면 첫 실행 때 옮긴다:
- 옛 폴더를 쓰는 앱이 실행 중이면(app.lock을 Qt가 잡고 있음) 옮기지 않고 옛 폴더를 그대로 쓴다.
- 같은 드라이브의 임시 폴더에 복사(SQLite -wal/-shm 포함) → 파일별 SHA256 비교 → 이전 표시 파일을 쓴 뒤 이름을 바꿔
  한 번에 NoseOut으로 만든다. 중간에 실패하면 임시 복사본만 지우고 옛 폴더를 계속 쓴다.
- 옛 폴더는 지우거나 바꾸지 않는다(되돌릴 수 있게 보존).
둘 다 있으면 새 폴더를 쓰고 옛 폴더는 그대로 둔다.

run_app.py가 NumPy·Qt보다 먼저 부르므로 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time

APP_DIR = "NoseOut"
LEGACY_DIR = "DigUpWin"
ENV = "NOSEOUT_HOME"
LEGACY_ENV = "DIGUPWIN_HOME"
MARKER = f"migrated-from-{LEGACY_DIR}.json"
LOCK = "app.lock"          # app.main의 QLockFile
SKIP = {LOCK}              # 옛 실행의 잠금 파일은 옮기지 않는다

# 로그 설정 전에 정해지므로 남길 내용을 모아 두고 앱이 app.log에 옮긴다.
NOTES: list[str] = []
_resolved: Path | None = None


def in_use(folder: Path) -> bool:
    """그 폴더를 쓰는 NoseOut이 실행 중인가. Qt의 QLockFile은 Windows에서 잠금 파일을 쓰기 공유 없이 열어 둔다:
    쓰기로 열리면 잠금이 없거나 끝난 실행이 남긴 파일이다. 판단할 수 없으면 실행 중으로 본다(옮기지 않음)."""
    lock = folder / LOCK
    if not lock.exists():
        return False
    try:
        with open(lock, "r+b"):
            return False
    except OSError:
        return True


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _files(folder: Path) -> list[Path]:
    return sorted(p.relative_to(folder) for p in folder.rglob("*") if p.is_file() and p.name not in SKIP)


def migrate(old: Path, new: Path, copy=shutil.copy2) -> bool:
    """old를 new로 복사해 검증한다. 성공하면 True. new가 이미 있거나 실패하면 아무것도 바꾸지 않고 False.

    copy: 파일 하나를 복사하는 함수(테스트에서 실패를 흉내 낸다)."""
    if new.exists() or not old.is_dir() or in_use(old):
        return False
    staging = new.with_name(f"{new.name}.migrating-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        files = _files(old)
        staging.mkdir(parents=True)
        for relative in files:
            (staging / relative).parent.mkdir(parents=True, exist_ok=True)
            copy(old / relative, staging / relative)
        # 복사 중에 앱이 시작해 옛 폴더를 바꿨을 수 있다: 목록과 내용이 모두 같을 때만 성공으로 본다.
        if in_use(old) or _files(old) != files or _files(staging) != files:
            raise OSError("복사 중 옛 폴더가 바뀌었습니다")
        total = 0
        for relative in files:
            if _digest(old / relative) != _digest(staging / relative):
                raise OSError(f"복사본이 다릅니다: {relative}")
            total += (staging / relative).stat().st_size
        (staging / MARKER).write_text(json.dumps(
            {"from": str(old), "files": len(files), "bytes": total, "verified": "sha256",
             "time": time.strftime("%Y-%m-%d %H:%M:%S"), "old_folder_kept": True},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(staging, new)            # 마지막 한 번에 새 폴더가 생긴다(이미 있으면 실패)
        return True
    except OSError as error:
        NOTES.append(f"settings migration from {old} failed, keeping the old folder: {error}")
        shutil.rmtree(staging, ignore_errors=True)   # 우리가 만든 임시 복사본만 지운다
        return False


def resolve(environ=os.environ, do_migrate: bool = True) -> Path:
    """설정·색인 폴더. do_migrate=False면 옮기지 않고 지금 쓸 폴더만 고른다(측정 스크립트 등)."""
    if environ.get(ENV):
        return Path(environ[ENV])
    if environ.get(LEGACY_ENV):
        NOTES.append(f"{LEGACY_ENV} is deprecated; using it because {ENV} is not set. Rename it to {ENV}.")
        return Path(environ[LEGACY_ENV])
    local = Path(environ.get("LOCALAPPDATA") or Path.home())
    new, old = local / APP_DIR, local / LEGACY_DIR
    if new.exists() or not old.is_dir():
        return new
    if not do_migrate:
        return old
    if in_use(old):
        NOTES.append(f"{old} is in use by a running NoseOut; not migrating this time")
        return old
    if migrate(old, new):
        NOTES.append(f"settings and indexes copied from {old} to {new} and verified; the old folder was kept")
        return new
    return old


def home() -> Path:
    """앱의 설정·색인 폴더(프로세스마다 한 번 정하고 이전도 한 번만 시도)."""
    global _resolved
    if _resolved is None:
        _resolved = resolve()
    return _resolved
