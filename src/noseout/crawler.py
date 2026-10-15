"""사용자가 고른 폴더만 걷는다. 원본 Crawler.swift처럼 숨김·개발 폴더·비밀 파일을 건너뛰고, 코드 프로젝트 안에서는
스크린샷만 색인하며, 기계가 만든 이름의 미디어가 수백 개 모인 폴더(데이터셋)는 건너뛴다."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import rules, scope

DATASET_MINIMUM = 300
log = logging.getLogger("noseout")


@dataclass
class Found:
    path: str
    kind: str
    size: int
    mtime: float
    file_id: int     # NTFS file ID (os.stat st_ino)
    device: int


@dataclass
class CrawlResult:
    files: list[Found] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # path → 이유 (보고용)
    unreadable: set[str] = field(default_factory=set)        # 열거·stat에 실패한 경로: 지워졌다고 보지 않는다


def root_problem(root: str | Path) -> str | None:
    """고른 폴더를 색인할 수 없는 이유, 없으면 None. crawl과 UI(폴더 고를 때)가 같은 검사를 쓴다."""
    root = Path(root)
    # 고른 폴더 자체가 링크·정션이면 다른 곳을 가리킬 수 있다: 따라가지 않는다
    try:
        if rules.is_link(os.lstat(root)):
            return f"refusing to index {root}: it is a link or junction"
    except FileNotFoundError:
        return f"not a folder: {root}"
    reason = rules.forbidden_root_reason(root)
    if reason:
        return f"refusing to index {root}: {reason}"
    if not root.is_dir():
        return f"not a folder: {root}"
    return None


def file_problem(path: str | Path) -> str | None:
    """이름 결과에서 '내용 분석'으로 하나씩 고른 파일을 AI가 읽어도 되는지(2026-10-14). 그 파일만 승인하고 부모 폴더는 승인하지
    않는다. 폴더 색인과 같은 안전 규칙(링크·정션, 숨김, 비밀 파일, 시스템·개발 폴더, 클라우드 전용 파일, 지원 종류)을 쓴다.
    사용자 폴더 전체·드라이브 루트 바로 아래의 파일도 사용자가 그 파일을 직접 골랐으면 허용한다(폴더 전체 금지 규칙은 그대로).
    돌려주는 값: None | missing | link | folder | hidden | secret | unsupported | system | developer | cloud"""
    path = Path(os.path.abspath(path))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "missing"
    if stat.S_ISDIR(st.st_mode):
        return "folder"
    if not scope.readable_file(path):
        return "link"
    if rules.is_hidden(path.name, st):
        return "hidden"
    verdict, detail = rules.classify(path.name)
    if rules.is_secret(path.name):
        return "secret"
    if verdict != "index":
        return "unsupported"
    if rules.system_folder_reason(path.parent):
        return "system"
    for folder in path.parents:
        if rules.folder_skip_reason(folder):
            return "developer"
    if rules.is_placeholder(st):
        return "cloud"
    return None


def crawl_files(paths: list[str | Path]) -> CrawlResult:
    """하나씩 고른 파일들(file_problem을 통과한 것만). 없어진 파일은 결과에 넣지 않아 색인에서 지워진다."""
    result = CrawlResult()
    for raw in paths:
        path = Path(os.path.abspath(raw))
        problem = file_problem(path)
        if problem == "missing":
            continue
        if problem:
            result.skipped[str(path)] = problem
            continue
        try:
            full = os.stat(path, follow_symlinks=False)
        except OSError:
            result.unreadable.add(str(path))
            continue
        result.files.append(Found(str(path), rules.classify(path.name)[1], full.st_size, full.st_mtime, full.st_ino,
                                  full.st_dev))
    return result


def crawl(roots: list[str | Path], exclusions: list[str | Path] | None = None, code: bool = False) -> CrawlResult:
    """`code`: 코드 색인용으로 걷는다(6단계, 원본 Crawler의 options.code): 코드 파일만(rules.classify_code), 남의 코드·
    빌드 폴더와 서브모듈 제외, git 저장소 안에서는 git이 무시하는 파일 제외. 데이터셋·프로젝트 규칙은 일반 색인에만."""
    exclusions = exclusions or []
    result = CrawlResult()
    for root in roots:
        root = Path(root)
        problem = root_problem(root)
        if problem:
            raise ValueError(problem)
        _walk(root.resolve(), root.resolve(), in_project=False, result=result, exclusions=exclusions, code=code)
    if code:
        _drop_git_ignored(result)
    else:
        _drop_datasets(result, {str(Path(r).resolve()) for r in roots})
    return result


def _walk(folder: Path, root: Path, in_project: bool, result: CrawlResult, exclusions: list[str | Path],
          recursive: bool = True, code: bool = False) -> None:
    if scope.excluded(folder, exclusions):
        result.skipped[str(folder)] = "excluded by user"
        return
    # 고른 폴더 자체가 프로젝트여도 원본처럼 그 안에서는 스크린샷만
    in_project = in_project or rules.is_project_root(folder)
    try:
        entries = list(os.scandir(folder))
    except OSError as error:
        result.skipped[str(folder)] = f"unreadable: {error.strerror}"
        result.unreadable.add(str(folder))
        return
    for entry in sorted(entries, key=lambda e: e.name):
        if scope.excluded(entry.path, exclusions):
            result.skipped[entry.path] = "excluded by user"
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            result.unreadable.add(entry.path)
            continue
        _visit(Path(entry.path), st, root, in_project, result, exclusions, recursive, code)


def _visit(path: Path, st: os.stat_result, root: Path, in_project: bool, result: CrawlResult,
           exclusions: list[str | Path], recursive: bool = True, code: bool = False) -> None:
    """폴더 항목 하나의 판정. 전체 걷기와 감시(바뀐 경로만 살피기)가 같은 규칙을 쓰도록 나눴다."""
    if rules.is_link(st):
        result.skipped[str(path)] = "link"
        return
    if rules.is_hidden(path.name, st):
        return  # 원본처럼 숨김은 보고하지 않고 지나간다
    if stat.S_ISDIR(st.st_mode):
        # 고른 폴더 안에 시스템·앱 데이터 폴더가 있어도 들어가지 않는다
        reason = rules.folder_skip_reason(path) or rules.forbidden_root_reason(path) \
            or (rules.code_skip_reason(path) if code else None)
        if reason:
            result.skipped[str(path)] = reason
        elif recursive:
            _walk(path, root, in_project, result, exclusions, code=code)
        return
    verdict, detail = rules.classify_code(path.name) if code else rules.classify(path.name)
    if verdict == "skip":
        result.skipped[str(path)] = detail
        return
    if verdict != "index":
        return
    if code and rules.extension(path.name) in ("ts", "mts") and rules.is_mpeg_ts(path):
        return  # 영상(MPEG-TS)이지 TypeScript가 아니다
    if not code and in_project and detail != "screenshot":
        result.skipped[str(path)] = "inside a code project"
        return
    if rules.is_placeholder(st):
        result.skipped[str(path)] = "cloud-only file"
        return
    # Windows에서 DirEntry.stat()은 st_ino·st_dev를 0으로 준다: 색인할 파일만 os.stat으로 실제 file ID를 읽는다
    try:
        full = os.stat(path, follow_symlinks=False)
    except OSError:
        result.unreadable.add(str(path))
        return
    limit = rules.MAX_NOTEBOOK_BYTES if rules.extension(path.name) == "ipynb" else rules.MAX_CODE_BYTES
    if code and full.st_size > limit:
        result.skipped[str(path)] = "too big to be written by hand"
        return
    result.files.append(Found(str(path), detail, full.st_size, full.st_mtime, full.st_ino, full.st_dev))


def crawl_targets(roots: list[str | Path], targets: list[str | Path],
                  exclusions: list[str | Path] | None = None, code: bool = False) -> tuple[CrawlResult, set[str]]:
    """감시가 알려 준 경로만 살핀다(6단계). 결과와 함께 '살핀 범위'(normcase 경로)를 준다: 그 경로(폴더면 그 아래)의
    기존 색인만 찾은 결과와 맞춘다. 전체 걷기와 같은 판정을 쓰려고 고른 폴더에서 대상까지의 상위 폴더도 다시 확인한다.
    - 없어진 경로(FileNotFoundError)만 살핀 범위에 넣는다: 지워진 것으로 맞춘다.
    - 접근이 안 되는 경로·고른 폴더 자체의 문제는 unreadable로 두어 기존 색인을 지우지 않는다.
    - 상위 폴더가 링크·정션·숨김·건너뛸 폴더가 되었으면 전체 걷기처럼 그 아래 파일을 색인 대상에서 뺀다."""
    exclusions = exclusions or []
    result = CrawlResult()
    covered: set[str] = set()
    whole: list[Path] = []   # 하위까지 다 걸은 폴더 (데이터셋 판정에 폴더 전체가 들어 있다)
    resolved_roots = [Path(r).resolve() for r in roots]
    targets = [Path(os.path.abspath(t)) for t in targets]
    # 같은 묶음에 상위 폴더가 있으면 그 폴더를 걸을 때 함께 본다 (예: 복사해 넣은 폴더와 그 안의 파일 이벤트)
    targets = [t for t in targets if not any(o != t and scope.contains(o, t) for o in targets)]
    for target in targets:
        target = Path(os.path.abspath(target))
        owners = [r for r in resolved_roots if scope.contains(r, target)]
        if not owners or scope.excluded(target, exclusions):
            continue
        root = max(owners, key=lambda r: len(str(r)))
        if root_problem(root):
            result.unreadable.add(str(target))   # 고른 폴더가 사라졌거나 열리지 않음: 삭제로 보지 않는다
            continue
        in_project, gone, blocked = rules.is_project_root(root), False, False
        folder = root
        try:
            for part in target.relative_to(root).parts[:-1]:
                folder = folder / part
                st = os.lstat(folder)
                if rules.is_link(st) or rules.is_hidden(part, st) or not stat.S_ISDIR(st.st_mode) \
                        or rules.folder_skip_reason(folder) or rules.forbidden_root_reason(folder) \
                        or (code and rules.code_skip_reason(folder)):
                    blocked = True
                    break
                in_project = in_project or rules.is_project_root(folder)
        except FileNotFoundError:
            gone = True
        except OSError:
            result.unreadable.add(str(target))
            continue
        if target == root:
            covered.add(os.path.normcase(str(target)))
            _walk(target, root, False, result, exclusions, code=code)
            whole.append(target)
            continue
        covered.add(os.path.normcase(str(target)))
        if gone or blocked:
            continue
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError:
            covered.discard(os.path.normcase(str(target)))
            result.unreadable.add(str(target))
            continue
        if stat.S_ISDIR(st.st_mode) and not rules.is_link(st):
            whole.append(target)
        _visit(target, st, root, in_project, result, exclusions, code=code)
    if code:
        _drop_git_ignored(result)
    else:
        _drop_datasets(result, {str(r) for r in resolved_roots}, whole, exclusions)
    return result, covered


def _drop_datasets(result: CrawlResult, roots: set[str], whole: list[Path] | None = None,
                   exclusions: list[str | Path] | None = None) -> None:
    groups: dict[tuple[str, str], list[Found]] = defaultdict(list)
    for f in result.files:
        folder = str(Path(f.path).parent)
        if f.kind in ("image", "screenshot") and folder not in roots:
            groups[(folder, f.kind)].append(f)
    drop = set()
    for (folder, kind), files in groups.items():
        if whole is not None and not any(scope.contains(w, folder) for w in whole):
            # 감시로 일부 파일만 살폈다: 전체 걷기와 같은 판정이 되도록 그 폴더의 파일 전체(하위 폴더 제외)로 센다
            try:
                many = len(os.listdir(folder)) >= DATASET_MINIMUM
            except OSError:
                many = False
            if not many:
                continue
            listing = CrawlResult()
            _walk(Path(folder), Path(folder), _in_project(Path(folder), roots), listing, exclusions or [],
                  recursive=False)
            everything = [f for f in listing.files if f.kind == kind]
        else:
            everything = files
        if len(everything) >= DATASET_MINIMUM:
            machine = sum(rules.looks_machine_named(Path(f.path).stem) for f in everything)
            if machine / len(everything) >= 0.7:
                drop.update(f.path for f in files)
                result.skipped[folder] = f"looks like a dataset ({len(everything)} files)"
    result.files = [f for f in result.files if f.path not in drop]


def _in_project(folder: Path, roots: set[str]) -> bool:
    """전체 걷기처럼 고른 폴더부터 이 폴더까지만 프로젝트 표지를 찾는다(고른 폴더 위는 보지 않는다)."""
    for path in (folder, *folder.parents):
        if rules.is_project_root(path):
            return True
        if any(os.path.normcase(str(path)) == os.path.normcase(r) for r in roots):
            return False
    return False


# git이 무시하는 파일 (코드 색인, 원본 GitRepos·GitFiles)

GIT = shutil.which("git")


def _repo_of(folder: Path, cache: dict[str, str | None]) -> str | None:
    """folder가 든 git 저장소(.git 폴더가 있는 가장 가까운 상위 폴더). 홈 폴더·드라이브 루트에서 멈춘다."""
    key = os.path.normcase(str(folder))
    if key in cache:
        return cache[key]
    answer = None
    if (folder / ".git").is_dir():
        answer = str(folder)
    elif folder.parent != folder and folder != Path.home():
        answer = _repo_of(folder.parent, cache)
    cache[key] = answer
    return answer


def git_ignored(repo: str, relative: list[str]) -> set[str] | None:
    """repo 기준 상대 경로 가운데 git이 무시하는 것. git이 없거나 답하지 못하면 None(이 필터를 쓰지 않음).
    추적 중인 파일은 무시 규칙에 걸려도 나오지 않는다(git check-ignore 기본 동작, 원본의 ls-files와 같은 결과).
    읽기만 한다: 저장소 잠금을 잡지 않는다(GIT_OPTIONAL_LOCKS=0)."""
    if GIT is None or not relative:
        return None
    paths = "\0".join(r.replace(os.sep, "/") for r in relative) + "\0"
    try:
        reply = subprocess.run(
            [GIT, "-C", repo, "check-ignore", "-z", "--stdin"], input=paths.encode("utf-8"), capture_output=True,
            timeout=60, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0",
                             # 찾은 저장소 위로 올라가지 않게: .git이 깨진 폴더에서 바깥 저장소의 무시 규칙을 쓰면
                             # 엉뚱한 파일이 빠진다(2026-10-14, 평가 중 재현). 그때는 git이 답하지 못해 필터를 쓰지 않는다.
                             "GIT_CEILING_DIRECTORIES": str(Path(repo).parent)},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        log.warning("git check-ignore failed in %s", repo, exc_info=True)
        return None
    if reply.returncode == 1:
        return set()
    if reply.returncode != 0:
        log.warning("git check-ignore exited %s in %s", reply.returncode, repo)
        return None
    out = reply.stdout.decode("utf-8", "replace").split("\0")
    return {os.path.normcase(os.path.normpath(p)) for p in out if p}


def _drop_git_ignored(result: CrawlResult) -> None:
    cache: dict[str, str | None] = {}
    by_repo: dict[str, list[Found]] = defaultdict(list)
    for f in result.files:
        repo = _repo_of(Path(f.path).parent, cache)
        if repo:
            by_repo[repo].append(f)
    drop = set()
    for repo, files in by_repo.items():
        ignored = git_ignored(repo, [os.path.relpath(f.path, repo) for f in files])
        for f in files:
            if ignored and os.path.normcase(os.path.normpath(os.path.relpath(f.path, repo))) in ignored:
                drop.add(f.path)
                result.skipped[f.path] = "ignored by git"
    result.files = [f for f in result.files if f.path not in drop]
