"""선택·제외 폴더의 경계 판정. 파일을 열거나 링크를 따라가지 않고 UI·검색·색인에서 같은 규칙을 쓴다."""

from __future__ import annotations

import os
from pathlib import Path

from . import rules


def contains(folder: str | Path, path: str | Path) -> bool:
    folder = os.path.normcase(os.path.abspath(folder)).rstrip("\\/")
    path = os.path.normcase(os.path.abspath(path))
    return path == folder or path.startswith(folder + os.sep)


def excluded(path: str | Path, folders: list[str | Path]) -> bool:
    return any(contains(folder, path) for folder in folders)


def allows(path: str | Path, roots: list[str | Path], exclusions: list[str | Path]) -> bool:
    return excluded(path, roots) and not excluded(path, exclusions)


def readable_file(path: str | Path) -> bool:
    """색인 뒤 링크·정션으로 바뀐 파일/상위 폴더의 미리 보기는 따라가지 않는다."""
    try:
        target = Path(path)
        return not any(rules.is_link(os.lstat(part)) for part in (target, *target.parents)) and target.is_file()
    except OSError:
        return False
