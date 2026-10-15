"""빠른 이름 검색: 고른 폴더 안 파일·폴더의 이름과 경로만 따로 등록해 AI 분석 전부터 찾는다.

원본 DigUp에는 없는 기능이다(Everything처럼 이름으로 바로 찾기를 원하는 사용자 요청, 2026-10-14). 볼륨 전체(MFT·USN)를 읽지
않고 고른 폴더만 os.scandir로 걷는다: 관리자 권한·서비스가 필요 없고 "시스템 전체를 색인하지 않는다"는 규칙과 맞는다.

- 저장: 색인 옆 names.sqlite (index.sqlite와 다른 파일). AI 색인의 스키마·버전 검사와 무관하게 열리고, 엔진이 다르거나 없어도
  쓸 수 있다. 파일 시스템에서 언제든 다시 만들 수 있는 파생 데이터라서 형식이 다르면 지우고 다시 만든다.
- 대상: 고른 폴더 안 모든 확장자의 파일과 폴더 이름. AI 색인과 같은 안전 규칙(금지·숨김·시스템·링크·정션·개발 폴더·비밀 파일)
  은 그대로 지키지만, 내용 분석 규칙(코드 프로젝트 안은 스크린샷만, 데이터셋 폴더 등)은 이름 목록에 쓰지 않는다.
- 갱신: 폴더 단위로 맞춘다(그 폴더의 직접 하위 항목 목록 ↔ 저장된 행). 이름이 바뀐 폴더는 옛 경로 아래를 지우고 새 폴더를 다시
  걷는다(경로 문자열을 고쳐 쓰지 않는다: 파일 시스템에서 다시 읽은 것만 남는다). 대소문자만 바뀐 경우도 같다.
- 열 수 없는 폴더·항목은 삭제로 보지 않는다: 기존 행을 남기고 trouble에 적어 결과에 "상태 미확인"으로 표시한다. 고른 폴더
  자체가 사라지거나 열리지 않으면 roots.state를 unavailable로 두고 결과에 "접근 불가"로 표시한다.
- 검색: NFKC + casefold로 접은 이름(search_text.normalized)에서 부분 문자열(instr)로 찾는다. 검색어의 단어는 모두 이름이나
  고른 폴더 아래 상대 폴더 경로에 있어야 한다(AND). 2글자 한국어도 찾는다(FTS5 trigram은 3글자 미만을 찾지 못한다).

사용자 파일은 읽지 않는다(이름·크기·수정 시각만). 쓰는 곳은 names.sqlite뿐이다.

2026-10-15: 로컬 NTFS는 MFT 목록·USN 변경 기록으로도 맞출 수 있다(ntfs.py). 볼륨 상태(volumes 표)는 기존 이름 목록에 표만 더하므로
형식(FORMAT)을 올리지 않는다: 옛 목록은 그대로 검색되고, 볼륨 상태가 없으면 처음 한 번 다시 맞춘다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from array import array
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import rules, scope, search_text

FORMAT = "2"              # 2: 검색 표에 stem (2026-10-14, 개발 중 측정 파일만 있었음)
FOLD = "nfkc-casefold-1"     # fold()를 바꾸면 올린다: 저장된 접힌 이름을 다시 만든다
COMMIT_ROWS = 2000           # 처음 등록 중에도 앞부분부터 검색되도록 자주 커밋한다
COMMIT_SECONDS = 0.5
DEFAULT_LIMIT = 50
CANDIDATES = 4               # 좁은 표에서 먼저 고르는 후보 수 = 결과 수 × 이것


def fold(text: str) -> str:
    """비교용 이름: NFKC(전각·호환 문자, macOS의 NFD 한글을 조합형으로), 보이지 않는 문자 제거, 대소문자 접기."""
    return search_text.normalized(text).casefold()


def kind_of(name: str, is_dir: bool) -> str:
    """결과 화면의 종류. AI 색인과 같은 이름(doc, pdf, image…)을 쓰고, 그 밖은 code·folder·other."""
    if is_dir:
        return "folder"
    verdict, detail = rules.classify(name)
    if verdict == "index":
        return detail
    if rules.classify_code(name)[0] == "index" or rules.extension(name) in rules.CODE_EXTENSIONS:
        return "code"
    return "other"


def _prefix_range(path: str) -> tuple[str, str]:
    """path 아래(path 자체 제외)를 고르는 범위. '\\' 다음 글자가 ']'라서 "C:\\a\\"는 "C:\\a2\\…"를 잡지 않는다."""
    prefix = path.rstrip("\\/") + "\\"
    return prefix, prefix[:-1] + "]"


def _resolved(root: str | Path) -> str:
    try:
        return str(Path(root).resolve())
    except OSError:
        return os.path.abspath(root)


def outer_roots(roots: list[str | Path]) -> list[str]:
    """겹치는 고른 폴더(A와 A\\B)는 바깥 것만 걷는다. 같은 파일을 두 번 등록하지 않는다."""
    resolved = sorted({os.path.normcase(_resolved(r)): _resolved(r) for r in roots}.values(), key=len)
    out: list[str] = []
    for root in resolved:
        if not any(scope.contains(o, root) for o in out):
            out.append(root)
    return out


@dataclass
class NameHit:
    """이름 일치 결과. 결과 화면이 AI 결과(searcher.Hit)와 같은 열을 쓰도록 같은 이름의 속성을 둔다."""
    path: str
    name: str
    is_dir: bool
    kind: str
    size: int | None
    mtime: float | None
    tier: int                       # 0 이름 완전 일치, 1 이름 앞부분, 2 이름 안, 3 폴더 경로에서만
    status: str | None = None       # None | "unavailable"(고른 폴더 접근 불가) | "unknown"(그 폴더를 읽지 못해 상태 미확인)
    segment: str = "name"
    loc: float | None = None
    excerpt: str | None = None
    context: list | None = None
    focus: int | None = None
    source: str = "name"


@dataclass
class ScanReport:
    added: int = 0
    changed: int = 0
    removed: int = 0
    folders: int = 0
    unavailable: list[str] = field(default_factory=list)
    trouble: int = 0
    stopped: bool = False
    seconds: float = 0.0


class NameIndex:
    """names.sqlite 하나. 쓰는 쪽(NameService 스레드)과 읽는 쪽(검색 스레드)이 각자 연결을 갖는다(WAL)."""

    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self.db: sqlite3.Connection | None = None
        self.roots: list[str] = []
        self.exclusions: list[str] = []
        self._dirty = 0
        self._last_commit = time.perf_counter()
        self._forbidden = rules.forbidden_names()
        self.search_roots: list[str] = []
        self.code_roots: list[str] = []
        self.name_roots: list[str] = []
        self._drive_roots: set[str] = set()
        self.on_commit: Callable[[], None] | None = None
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connect()
            self._create()

    def _connect(self) -> None:
        self.db = sqlite3.connect(self.path, timeout=5)
        if self.readonly:
            # 읽는 쪽은 저널 모드를 건드리지 않는다: 쓰는 쪽이 파일을 막 만드는 중이면 "database is locked"가 났다
            # (2026-10-14 드라이브 측정 중 발견). WAL로 바꾸는 것은 쓰는 쪽(_create 전 _connect)만 한다.
            self.db.execute("PRAGMA query_only = ON")
            return
        self.db.execute("PRAGMA journal_mode = WAL")
        # 파생 데이터라 전원이 꺼져 마지막 커밋을 잃어도 다시 걸으면 된다. WAL + NORMAL은 손상되지는 않는다.
        self.db.execute("PRAGMA synchronous = NORMAL")

    def ready(self) -> bool:
        """읽는 쪽: 파일과 스키마가 생긴 뒤에 연다(쓰는 쪽보다 먼저 열어 빈 파일을 만들지 않게)."""
        if self.db is None:
            if not self.path.exists():
                return False
            self._connect()
        try:
            return self.db.execute("PRAGMA user_version").fetchone()[0] == 1 and self._meta("format") == FORMAT
        except sqlite3.Error:
            return False

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self) -> "NameIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _create(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1 and self._meta("format") == FORMAT and self._meta("fold") == FOLD:
            self._create_volumes()
            return
        # 형식·접기 규칙이 다른 이름 목록: 사용자 데이터가 아니고 파일 시스템에서 다시 만들 수 있으므로 지우고 새로 만든다
        self.db.executescript("""
            DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS roots; DROP TABLE IF EXISTS entries;
            DROP TABLE IF EXISTS trouble; DROP TABLE IF EXISTS search; DROP TABLE IF EXISTS volumes;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE roots(path TEXT PRIMARY KEY COLLATE NOCASE, state TEXT NOT NULL, detail TEXT,
                               checked_at REAL);
            CREATE TABLE entries(
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                parent TEXT NOT NULL COLLATE NOCASE,
                name TEXT NOT NULL,
                is_dir INTEGER NOT NULL,
                size INTEGER,
                mtime REAL,
                kind TEXT NOT NULL,
                name_fold TEXT NOT NULL,
                stem_fold TEXT NOT NULL,
                dir_fold TEXT NOT NULL      -- 고른 폴더 아래 상대 폴더 경로 (접은 것)
            );
            CREATE INDEX entries_parent ON entries(parent);
            -- 검색이 훑는 좁은 표: 접은 이름 + char(1) + 접은 상대 폴더 경로. entries를 그대로 훑으면 행이 넓어(경로 두 번)
            -- 10만 개에서 3배 느렸다(33ms → 12ms, 2026-10-14 측정). 트리거로 entries와 늘 같게 둔다.
            CREATE TABLE search(id INTEGER PRIMARY KEY, f TEXT NOT NULL, stem TEXT NOT NULL);
            CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
                INSERT INTO search(id, f, stem) VALUES(new.id, new.name_fold || char(1) || new.dir_fold, new.stem_fold);
                END;
            CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN DELETE FROM search WHERE id = old.id; END;
            CREATE TRIGGER entries_au AFTER UPDATE OF name_fold, dir_fold, stem_fold ON entries BEGIN
                UPDATE search SET f = new.name_fold || char(1) || new.dir_fold, stem = new.stem_fold WHERE id = new.id;
                END;
            CREATE TABLE trouble(path TEXT PRIMARY KEY COLLATE NOCASE, reason TEXT, since REAL);
        """)
        self.db.executemany("INSERT INTO meta(key, value) VALUES(?, ?)",
                            [("format", FORMAT), ("fold", FOLD), ("outer_roots", "[]")])
        self.db.execute("PRAGMA user_version = 1")
        self._create_volumes()
        self.db.commit()

    def _create_volumes(self) -> None:
        # USN으로 맞추는 볼륨: 볼륨 GUID·일련번호·저널 ID·처리한 USN·등록 완료 여부·등록한 범위(normcase). complete가 1이 되는
        # 것은 등록과 등록 중 변경 반영이 모두 커밋된 뒤뿐이다(ntfs.NtfsNames.sync).
        self.db.execute("""CREATE TABLE IF NOT EXISTS volumes(root TEXT PRIMARY KEY COLLATE NOCASE, guid TEXT, serial INTEGER,
                           journal_id INTEGER, usn INTEGER NOT NULL, complete INTEGER NOT NULL, roots TEXT NOT NULL,
                           registered_by TEXT, detail TEXT, updated_at REAL)""")
        # 범위별 등록 방법·안내 (2026-10-15): 같은 드라이브에 MFT로 등록한 범위와 폴더 순회로 등록한 범위가 함께 있을 수 있다.
        # 이 열이 없던 상태는 지우지 않고 열만 더한다(비어 있으면 registered_by를 모든 범위에 쓴다).
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(volumes)")}
        if "root_info" not in columns:
            self.db.execute("ALTER TABLE volumes ADD COLUMN root_info TEXT")
        self.db.commit()

    def _meta(self, key: str) -> str | None:
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    # 범위

    def set_scope(self, roots: list[str | Path], exclusions: list[str | Path],
                  code_roots: list[str | Path] = (), name_roots: list[str | Path] = ()) -> None:
        """고른 폴더·제외 폴더를 바꾼다. 범위 밖이 된 행은 지운다(이름은 다시 걸으면 되므로 AI 색인처럼 보존하지 않는다).
        사라진 폴더·열 수 없는 폴더는 범위 안이면 지우지 않는다. `code_roots`: 코드 폴더도 이름 목록에 넣는다(코드 프로젝트의
        안전한 이름). 검색 폴더 밖인 코드 폴더 부분에는 코드 색인의 폴더 제외(vendor·build·dist·서브모듈)도 지킨다.
        `name_roots`: 이름 검색만 하는 범위(드라이브 루트 가능, rules.name_root_problem). AI 분석 범위와 무관하다."""
        self.search_roots = [_resolved(r) for r in roots]
        self.code_roots = [_resolved(r) for r in code_roots]
        self.name_roots = [_resolved(r) for r in name_roots]
        self.roots = self.search_roots + self.code_roots + self.name_roots
        self._drive_roots = {os.path.normcase(r) for r in self.roots if rules.is_drive_root(r)}
        self.exclusions = [os.path.abspath(e) for e in exclusions]
        outer = outer_roots(self.roots)
        clauses, params = [], []
        for root in outer:
            low, high = _prefix_range(root)
            clauses.append("(path >= ? AND path < ?)")
            params += [low, high]
        keep = " OR ".join(clauses) or "0"
        self.db.execute(f"DELETE FROM entries WHERE NOT ({keep})", params)
        self.db.execute(f"DELETE FROM trouble WHERE NOT ({keep} OR path IN ({','.join('?' * len(outer)) or 'NULL'}))",
                        params + outer)
        for excluded in self.exclusions:
            self._delete_under(excluded, include_self=True)
        known = [r for (r,) in self.db.execute("SELECT path FROM roots")]
        for root in known:
            if not any(os.path.normcase(root) == os.path.normcase(o) for o in outer):
                self.db.execute("DELETE FROM roots WHERE path = ?", (root,))
        if self._meta("outer_roots") != json.dumps(sorted(os.path.normcase(o) for o in outer)):
            self._refold_dirs(outer)
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('outer_roots', ?)",
                            (json.dumps(sorted(os.path.normcase(o) for o in outer)),))
        self.db.commit()

    def _owner(self, path: str) -> str | None:
        owners = [r for r in outer_roots(self.roots) if scope.contains(r, path)]
        return owners[0] if owners else None

    def _dir_fold(self, parent: str, root: str) -> str:
        relative = os.path.relpath(parent, root)
        return "" if relative == "." else fold(relative)

    def _refold_dirs(self, outer: list[str]) -> None:
        """바깥 고른 폴더가 바뀌면(예: A를 빼고 A\\B만 남김) 상대 폴더 경로를 다시 계산한다."""
        rows = []
        for id_, parent in self.db.execute("SELECT id, parent FROM entries").fetchall():
            root = next((r for r in outer if scope.contains(r, parent)), None)
            if root is not None:
                rows.append((self._dir_fold(parent, root), id_))
        self.db.executemany("UPDATE entries SET dir_fold = ? WHERE id = ?", rows)

    def _code_only(self, path: str) -> bool:
        return any(scope.contains(c, path) for c in self.code_roots) and             not any(scope.contains(r, path) for r in self.search_roots)

    def allowed(self, path: str) -> bool:
        return scope.allows(path, self.roots, self.exclusions)

    # 쓰기

    def scan(self, roots: list[str | Path], exclusions: list[str | Path],
             should_stop: Callable[[], bool] | None = None,
             progress: Callable[[int, str], None] | None = None,
             code_roots: list[str | Path] = (), name_roots: list[str | Path] = (),
             only: list[str] | None = None, report: ScanReport | None = None) -> ScanReport:
        """고른 폴더 전체를 걸어 맞춘다(처음 등록, 앱 시작 때 놓친 변경 확인, '색인 시작'). 검색·코드 폴더를 먼저 걷고
        이름만 검색하는 범위(드라이브 전체 등)를 나중에 걷는다: 드라이브를 걷는 동안에도 고른 폴더의 이름은 먼저 찾을 수 있다.
        이미 걸은 폴더는 드라이브를 걸을 때 다시 들어가지 않는다. `should_stop`이 막고 있으면(일시정지) 폴더 경계에서 기다린다."""
        started = time.perf_counter()
        report = report or ScanReport()
        self.set_scope(roots, exclusions, code_roots, name_roots)
        walked: list[str] = []
        # only: 이 범위만 걷는다(나머지는 USN으로 맞췄다). 걷지 않는 범위도 '걸은 것'으로 두어 바깥 범위를 걸을 때 들어가지 않는다.
        wanted = None if only is None else {os.path.normcase(_resolved(r)) for r in only}
        for root in outer_roots(self.search_roots + self.code_roots) + outer_roots(self.name_roots):
            if should_stop and should_stop():
                report.stopped = True
                break
            if wanted is not None and os.path.normcase(root) not in wanted and \
                    not any(scope.contains(w, root) for w in wanted):
                walked.append(root)
                continue
            if any(scope.contains(w, root) for w in walked) or not self._root_usable(root, report):
                continue
            self._sync(root, self._owner(root) or root, recursive=True, report=report, should_stop=should_stop,
                       progress=progress, skip={os.path.normcase(w) for w in walked})
            if report.stopped:
                break
            walked.append(root)
            self._set_root(root, "ok")
        self._commit(force=True)
        report.trouble = self.db.execute("SELECT COUNT(*) FROM trouble").fetchone()[0]
        report.seconds = time.perf_counter() - started
        return report

    def apply(self, targets: list[str], rescans: list[str] | None = None,
              should_stop: Callable[[], bool] | None = None) -> ScanReport:
        """감시가 알려 준 경로만 맞춘다. 대상의 부모 폴더 목록을 다시 읽어(직접 하위만) 추가·삭제·이름 변경·대소문자 변경을
        알아낸다. 새로 생긴 폴더와 이름이 바뀐 폴더는 그 아래를 걷는다. 전체 다시 확인(rescans)은 그 폴더 아래 전체를 걷는다."""
        started = time.perf_counter()
        report = ScanReport()
        parents: dict[str, str] = {}      # normcase(부모) → 부모
        recurse: set[str] = set()
        whole: dict[str, str] = {}
        for folder in rescans or []:
            folder = os.path.abspath(folder)
            if self.allowed(folder) or any(os.path.normcase(folder) == os.path.normcase(r) for r in self.roots):
                whole[os.path.normcase(folder)] = folder
        for target in targets:
            target = os.path.abspath(target)
            if not self.allowed(target) or any(scope.contains(w, target) for w in whole.values()):
                continue
            root = self._owner(target)
            if root is None or os.path.normcase(target) == os.path.normcase(root):
                if root is not None:
                    whole[os.path.normcase(root)] = root
                continue
            parent = os.path.dirname(target)
            verdict = self._ancestors_ok(root, parent)
            if verdict == "unreadable":
                self._trouble(target, "could not check the folder")
                continue
            if verdict == "gone" or verdict == "blocked":
                # 상위 폴더가 없어졌거나 링크·숨김 등이 되었다: 전체 걷기에서도 나오지 않을 항목이다
                report.removed += self._remove(target)
                continue
            parents[os.path.normcase(parent)] = parent
            try:
                st = os.lstat(target)
                if stat.S_ISDIR(st.st_mode) and not rules.is_link(st):
                    recurse.add(os.path.normcase(target))
            except OSError:
                pass
        for key, folder in sorted(whole.items(), key=lambda kv: len(kv[1])):
            if should_stop and should_stop():
                report.stopped = True
                break
            root = self._owner(folder)
            if root is None or not self._root_usable(root, report):
                continue
            if os.path.normcase(folder) == os.path.normcase(root):
                self._sync(root, root, recursive=True, report=report, should_stop=should_stop)
                self._set_root(root, "ok")
                continue
            verdict = self._ancestors_ok(root, os.path.dirname(folder))
            if verdict in ("gone", "blocked"):
                report.removed += self._remove(folder)
            elif verdict == "ok":
                parents[os.path.normcase(os.path.dirname(folder))] = os.path.dirname(folder)
                recurse.add(key)
        for key, parent in sorted(parents.items(), key=lambda kv: len(kv[1])):
            if should_stop and should_stop():
                report.stopped = True
                break
            root = self._owner(parent)
            if root is None or not self._root_usable(root, report):
                continue
            self._sync(parent, root, recursive=False, report=report, recurse_into=recurse, should_stop=should_stop)
        self._commit(force=True)
        report.seconds = time.perf_counter() - started
        return report

    # USN (ntfs.NtfsNames): 바뀐 폴더만 다시 읽기, MFT 목록 가져오기, 볼륨 상태

    def refresh_folders(self, folders: list[str], report: ScanReport, whole: list[str] = ()) -> None:
        """USN이 알려 준 '안의 항목이 바뀐 폴더'의 직접 목록만 다시 읽는다(새로 생기거나 옮겨 온 폴더는 그 아래를 걷는다).
        규칙은 폴더 순회와 같다: 상위 폴더가 숨김·개발 폴더·제외·링크가 되었으면 지운다. whole: 고른 범위 자체가 옮겨지거나 지워졌을
        수 있는 범위(사라졌으면 '접근 불가', 다시 나타났으면 전체를 다시 걷는다)."""
        for root in whole:
            row = self.db.execute("SELECT state FROM roots WHERE path = ?", (root,)).fetchone()
            if self._root_usable(root, report) and row is not None and row[0] != "ok":
                self._sync(root, root, recursive=True, report=report)
                self._set_root(root, "ok")
        for folder in sorted({os.path.normcase(f): f for f in folders}.values(), key=len):
            root = self._owner(folder)
            if root is None:
                continue
            state = self.db.execute("SELECT state FROM roots WHERE path = ?", (root,)).fetchone()
            if state is not None and state[0] != "ok":
                continue
            if os.path.normcase(folder) == os.path.normcase(root):
                self._sync(root, root, recursive=False, report=report)
                continue
            verdict = self._ancestors_ok(root, folder)
            if verdict == "ok":
                self._sync(folder, root, recursive=False, report=report)
            elif verdict in ("gone", "blocked"):
                report.removed += self._remove(folder)
            else:
                self._trouble(folder, "could not check the folder")
        self._commit(force=True)

    def import_mft(self, volume_root: str, roots: list[str], batches, report: ScanReport,
                   should_stop: Callable[[], bool] | None = None,
                   progress: Callable[[int, str], None] | None = None,
                   listable: Callable[[str], bool] | None = None) -> None:
        """관리자 도우미가 보낸 MFT 열거(ntfs.Record 묶음)로 roots(같은 볼륨)의 이름 목록을 한 번에 맞춘다. 파일·폴더를 열지 않는다.
        폴더 순회와 같은 결과가 되게: 같은 _eligible 규칙(속성은 레코드의 것), 사용자 권한으로 목록을 열 수 없는 폴더는 이름만 넣고
        그 안은 넣지 않는다(trouble, '상태 미확인'). 열 수 있는지는 보안 설명자 ID별로 한 번 확인한다(ID가 0이면 폴더마다).
        기존 행은 지우지 않고 고쳐 쓴 뒤 MFT에 없는 행만 지운다: 등록 중에도 저장된 목록이 검색된다. 크기·수정 시각은 MFT 열거에
        없어 새 행은 비워 둔다(화면에 쓰지 않음)."""
        from .ntfs import RECORD_MASK, ROOT_RECORD, fake_stat

        started = time.perf_counter()
        dirs: dict[int, tuple[int, str, int, int]] = {}
        file_parent, file_attrs, file_names = array("Q"), array("I"), []
        for batch in batches:
            if should_stop and should_stop():
                report.stopped = True
                return
            for r in batch:
                if r.is_dir:
                    dirs[r.frn] = (r.parent, r.name, r.attrs, r.security_id)
                elif r.name:
                    file_parent.append(r.parent)
                    file_attrs.append(r.attrs)
                    file_names.append(r.name)
        usable = [r for r in roots if self._root_usable(r, report)]
        targets = {os.path.normcase(r): r for r in usable}
        ancestors = {os.path.normcase(str(a)) for r in usable for a in Path(r).parents}
        volume_key = os.path.normcase(volume_root)
        verdicts: dict[int, bool] = {}

        def can_list(path: str, security_id: int) -> bool:
            if listable is not None:
                return listable(path)
            if security_id and security_id in verdicts:
                return verdicts[security_id]
            try:
                with os.scandir(path):
                    ok = True
            except OSError:
                ok = False
            if security_id:
                verdicts[security_id] = ok
            return ok

        # 폴더 상태: out(넣지 않음) | anc(고른 범위의 상위) | root | in | locked(이름만, 안은 못 읽음)
        memo: dict[int, tuple[str, str | None]] = {}
        dir_rows: list[tuple] = []

        def state_of(frn: int) -> tuple[str, str | None]:
            chain = []
            cur = frn
            while cur not in memo:
                if cur & RECORD_MASK == ROOT_RECORD:
                    memo[cur] = ("root" if volume_key in targets else "anc" if volume_key in ancestors else "out",
                                 volume_root)
                    break
                info = dirs.get(cur)
                if info is None or len(chain) > 4096:
                    memo[cur] = ("out", None)
                    break
                chain.append(cur)
                cur = info[0]
            for f in reversed(chain):
                parent, name, attrs, security_id = dirs[f]
                pstate, ppath = memo.get(parent, ("out", None))
                if pstate in ("out", "locked") or not name:
                    memo[f] = ("out", None)
                    continue
                path = os.path.join(ppath, name)
                key = os.path.normcase(path)
                if pstate == "anc":
                    memo[f] = ("root", path) if key in targets else ("anc", path) if key in ancestors else ("out", None)
                    continue
                if not self._eligible(path, name, fake_stat(attrs, True)):
                    memo[f] = ("out", None)
                    continue
                memo[f] = ("in", path) if can_list(path, security_id) else ("locked", path)
                dir_rows.append((path, ppath, name, True))
                if memo[f][0] == "locked":
                    self._trouble(path, "unreadable: access denied")
            return memo[frn]

        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS mft_seen(path TEXT PRIMARY KEY COLLATE NOCASE)")
        self.db.execute("DELETE FROM mft_seen")
        before = 0
        for root in usable:
            low, high = _prefix_range(root)
            before += self.db.execute("SELECT COUNT(*) FROM entries WHERE path >= ? AND path < ?", (low, high)).fetchone()[0]
            self.db.execute("DELETE FROM trouble WHERE path >= ? AND path < ?", (low, high))
        folds: dict[str, str] = {}
        written = 0

        def write(path: str, parent: str, name: str, is_dir: bool) -> None:
            nonlocal written
            owner = folds.get(parent)
            if owner is None:
                root = self._owner(parent) or parent
                owner = folds[parent] = self._dir_fold(parent, root)
            stem = name if is_dir else os.path.splitext(name)[0]
            self.db.execute(
                """INSERT INTO entries(path, parent, name, is_dir, size, mtime, kind, name_fold, stem_fold, dir_fold)
                   VALUES(?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET path = excluded.path, parent = excluded.parent, name = excluded.name,
                   is_dir = excluded.is_dir, kind = excluded.kind, name_fold = excluded.name_fold,
                   stem_fold = excluded.stem_fold, dir_fold = excluded.dir_fold""",
                (path, parent, name, int(is_dir), kind_of(name, is_dir), fold(name), fold(stem), owner))
            self.db.execute("INSERT OR IGNORE INTO mft_seen(path) VALUES(?)", (path,))
            written += 1
            self._dirty += 1
            if written % COMMIT_ROWS == 0:
                self._commit()
                if progress:
                    progress(written, parent)

        for i, frn in enumerate(list(dirs)):
            if should_stop and i % 20000 == 0 and should_stop():
                report.stopped = True
                break
            state_of(frn)
            while dir_rows:
                write(*dir_rows.pop())
        if not report.stopped:
            for i, (parent_frn, attrs, name) in enumerate(zip(file_parent, file_attrs, file_names)):
                if should_stop and i % 20000 == 0 and should_stop():
                    report.stopped = True
                    break
                pstate, ppath = state_of(parent_frn)
                if pstate not in ("root", "in"):
                    continue
                path = os.path.join(ppath, name)
                if self._eligible(path, name, fake_stat(attrs, False)):
                    write(path, ppath, name, False)
        if report.stopped:
            self._commit(force=True)       # 넣은 것은 남기되(검색됨) 지우기는 하지 않는다: 완료로 보지 않는다
            return
        removed = 0
        for root in usable:
            low, high = _prefix_range(root)
            removed += self.db.execute("""DELETE FROM entries WHERE path >= ? AND path < ?
                                          AND path NOT IN (SELECT path FROM mft_seen)""", (low, high)).rowcount
            self._set_root(root, "ok")
        after = sum(self.db.execute("SELECT COUNT(*) FROM entries WHERE path >= ? AND path < ?",
                                    _prefix_range(r)).fetchone()[0] for r in usable)
        self.db.execute("DELETE FROM mft_seen")
        report.removed += removed
        report.added += max(0, after - (before - removed))
        report.folders += sum(1 for state, _ in memo.values() if state in ("in", "root"))
        self._dirty += removed
        self._commit(force=True)
        report.seconds += time.perf_counter() - started

    def volume_state(self, root: str) -> dict | None:
        row = self.db.execute("""SELECT root, guid, serial, journal_id, usn, complete, roots, registered_by, detail,
                                 updated_at, root_info FROM volumes WHERE root = ?""", (root,)).fetchone()
        return self._volume_dict(row) if row else None

    def volume_states(self) -> list[dict]:
        return [self._volume_dict(row) for row in self.db.execute(
            "SELECT root, guid, serial, journal_id, usn, complete, roots, registered_by, detail, updated_at, root_info "
            "FROM volumes")]

    @staticmethod
    def _volume_dict(row) -> dict:
        keys = ("root", "guid", "serial", "journal_id", "usn", "complete", "roots", "registered_by", "detail", "updated_at",
                "root_info")
        out = dict(zip(keys, row))
        out["roots"] = json.loads(out["roots"] or "[]")
        stored = json.loads(out["root_info"] or "{}")
        # 옛 상태(범위별 정보 없음): 볼륨 하나의 등록 방법을 각 범위에 쓴다
        out["root_info"] = {key: stored.get(key, {"by": out["registered_by"] or "", "note": ""}) for key in out["roots"]}
        out["complete"] = bool(out["complete"])
        return out

    def set_volume(self, root: str, guid: str, serial: int, journal_id: int, usn: int, complete: bool, roots: list[str],
                   registered_by: str = "", detail: str = "", root_info: dict | None = None) -> None:
        """root_info: normcase(범위) → {"by": "mft"|"walk", "note": 안내}. roots에 있는 범위만 저장한다."""
        info = {key: value for key, value in (root_info or {}).items() if key in set(roots)}
        self.db.execute("""INSERT OR REPLACE INTO volumes(root, guid, serial, journal_id, usn, complete, roots, registered_by,
                           detail, updated_at, root_info) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (root, guid, serial, journal_id, usn, int(complete), json.dumps(sorted(roots)), registered_by,
                         detail, time.time(), json.dumps(info, ensure_ascii=False, sort_keys=True)))
        self._commit(force=True)

    def set_checkpoint(self, root: str, usn: int) -> None:
        """반영한 변경과 같은 커밋으로 위치를 옮긴다(변경이 저장되기 전에 위치만 앞서가지 않게)."""
        self.db.execute("UPDATE volumes SET usn = ?, updated_at = ? WHERE root = ?", (usn, time.time(), root))
        self._commit(force=True)

    def mark_incomplete(self, root: str, detail: str) -> None:
        self.db.execute("UPDATE volumes SET complete = 0, detail = ? WHERE root = ?", (detail, root))
        self._commit(force=True)

    def forget_volume(self, root: str) -> None:
        self.db.execute("DELETE FROM volumes WHERE root = ?", (root,))
        self._commit(force=True)

    def _root_usable(self, root: str, report: ScanReport) -> bool:
        problem = rules.name_root_problem(root)
        if problem is None:
            return True
        # 고른 폴더가 사라졌거나(외장 드라이브 등) 열리지 않는다: 지우지 않고 결과에 "접근 불가"로 표시한다
        self._set_root(root, "unavailable", problem)
        report.unavailable.append(root)
        return False

    def _set_root(self, root: str, state: str, detail: str | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO roots(path, state, detail, checked_at) VALUES(?, ?, ?, ?)",
                        (root, state, detail, time.time()))
        if state == "ok":
            self.db.execute("DELETE FROM trouble WHERE path = ?", (root,))

    def _ancestors_ok(self, root: str, folder: str) -> str:
        """고른 폴더부터 folder까지의 상위 폴더가 전체 걷기에서 들어가는 폴더인가: ok | gone | blocked | unreadable."""
        current = root
        try:
            relative = Path(folder).relative_to(root).parts if os.path.normcase(folder) != os.path.normcase(root) \
                else ()
        except ValueError:
            return "blocked"
        try:
            for part in relative:
                current = os.path.join(current, part)
                st = os.lstat(current)
                if not self._eligible(current, part, st):
                    return "blocked"
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "unreadable"
        return "ok"

    def _eligible(self, path: str, name: str, st: os.stat_result) -> bool:
        """전체 걷기와 감시가 같은 규칙을 쓴다. AI 색인의 안전 규칙은 지키고 내용 분석 규칙은 쓰지 않는다."""
        if rules.is_link(st) or rules.is_hidden(name, st) or (self.exclusions and scope.excluded(path, self.exclusions)):
            return False
        if stat.S_ISDIR(st.st_mode):
            if rules.folder_skip_reason(Path(path)):
                return False
            if self.code_roots and self._code_only(path) and rules.code_skip_reason(Path(path)):
                return False     # 코드 폴더: 남의 코드·빌드 산출물·서브모듈 (코드 색인과 같은 폴더 제외)
            if name.casefold() in rules.DRIVE_SYSTEM_FOLDER_NAMES and                     os.path.normcase(os.path.dirname(path)) in self._drive_roots:
                return False     # 드라이브 최상위의 Windows 관리 폴더(Recovery, PerfLogs, Windows.old 등)
            # 시스템 폴더 검사는 경로를 resolve해 폴더마다 비쌌다(1만 개 등록 시간의 65%). 이름이 시스템·사용자 폴더 이름과
            # 같을 때만 전체 검사를 한다: 링크를 따라가지 않고 위에서부터 걸으므로 결과는 같다(rules.forbidden_names).
            # 사용자 폴더 전체·드라이브 루트는 이름 검색에서 허용한다(AI 색인의 forbidden_root_reason과 다름).
            return not (name.casefold() in self._forbidden and rules.system_folder_reason(Path(path)))
        return not rules.is_secret(name)

    def _sync(self, folder: str, root: str, recursive: bool, report: ScanReport,
              recurse_into: set[str] | None = None, should_stop: Callable[[], bool] | None = None,
              progress: Callable[[int, str], None] | None = None, skip: set[str] | None = None) -> None:
        """`skip`: 이번 전체 걷기에서 이미 걸은 고른 폴더(normcase). 행은 그대로 두고 다시 들어가지 않는다."""
        stack: list[tuple[str, bool]] = [(folder, recursive)]
        while stack:
            if should_stop and should_stop():
                report.stopped = True
                return
            current, deep = stack.pop()
            listing, unstatable = {}, set()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            unstatable.add(os.path.normcase(entry.path))
                            self._trouble(entry.path, "could not read its details")
                            continue
                        if self._eligible(entry.path, entry.name, st):
                            listing[os.path.normcase(entry.path)] = (entry.path, entry.name, st)
            except FileNotFoundError:
                if os.path.normcase(current) != os.path.normcase(root):
                    report.removed += self._remove(current)
                continue
            except OSError as error:
                # 접근 거부 등: 그 폴더의 기존 행은 지우지 않는다(삭제가 아니다)
                self._trouble(current, f"unreadable: {error.strerror or error}")
                continue
            self.db.execute("DELETE FROM trouble WHERE path = ?", (current,))
            report.folders += 1
            dir_fold = self._dir_fold(current, root)
            known = {os.path.normcase(path): (id_, path, is_dir, size, mtime) for id_, path, is_dir, size, mtime in
                     self.db.execute("SELECT id, path, is_dir, size, mtime FROM entries WHERE parent = ?", (current,))}
            for key, (path, name, st) in listing.items():
                is_dir = stat.S_ISDIR(st.st_mode)
                old = known.pop(key, None)
                if old is not None and (old[1] != path or bool(old[2]) != is_dir):
                    # 대소문자만 바뀐 이름, 파일↔폴더: 옛 행(폴더면 그 아래까지)을 지우고 새로 읽는다
                    report.removed += self._remove(old[1])
                    old = None
                size = None if is_dir else st.st_size
                if old is None:
                    stem = os.path.splitext(name)[0] if not is_dir else name
                    self.db.execute(
                        """INSERT INTO entries(path, parent, name, is_dir, size, mtime, kind, name_fold, stem_fold,
                           dir_fold) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (path, current, name, int(is_dir), size, st.st_mtime, kind_of(name, is_dir), fold(name),
                         fold(stem), dir_fold))
                    self.db.execute("DELETE FROM trouble WHERE path = ?", (path,))
                    report.added += 1
                    self._dirty += 1
                    if is_dir and not (skip and key in skip):
                        stack.append((path, True))     # 새 폴더(새로 생겼거나 이름이 바뀜)는 아래를 모른다
                    continue
                if not is_dir and (old[3] != size or old[4] != st.st_mtime):
                    self.db.execute("UPDATE entries SET size = ?, mtime = ? WHERE id = ?", (size, st.st_mtime, old[0]))
                    report.changed += 1
                    self._dirty += 1
                self.db.execute("DELETE FROM trouble WHERE path = ?", (path,))
                if is_dir and (deep or (recurse_into and key in recurse_into)) and not (skip and key in skip):
                    stack.append((path, True))
            for key, old in known.items():
                if key in unstatable:
                    continue    # 있지만 읽지 못한 항목: 지우지 않는다
                report.removed += self._remove(old[1])
            self._commit()
            if progress:
                progress(report.added, current)

    def _remove(self, path: str) -> int:
        """행 하나와(폴더면) 그 아래 전체를 지운다."""
        removed = self.db.execute("DELETE FROM entries WHERE path = ?", (path,)).rowcount
        removed += self._delete_under(path)
        self.db.execute("DELETE FROM trouble WHERE path = ?", (path,))
        self._dirty += removed
        return removed

    def _delete_under(self, path: str, include_self: bool = False) -> int:
        low, high = _prefix_range(path)
        count = self.db.execute("DELETE FROM entries WHERE path >= ? AND path < ?", (low, high)).rowcount
        self.db.execute("DELETE FROM trouble WHERE path >= ? AND path < ?", (low, high))
        if include_self:
            count += self.db.execute("DELETE FROM entries WHERE path = ?", (path,)).rowcount
            self.db.execute("DELETE FROM trouble WHERE path = ?", (path,))
        return count

    def _trouble(self, path: str, reason: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO trouble(path, reason, since) VALUES(?, ?, ?)", (path, reason, time.time()))

    def _commit(self, force: bool = False) -> None:
        now = time.perf_counter()
        if force or self._dirty >= COMMIT_ROWS or now - self._last_commit >= COMMIT_SECONDS:
            self.db.commit()
            changed = self._dirty > 0
            self._dirty = 0
            self._last_commit = now
            if changed and self.on_commit:
                self.on_commit()

    # 읽기

    def counts(self) -> dict:
        if self.db is None and not self.ready():
            return {"files": 0, "folders": 0, "roots": {}, "trouble": 0}
        files, folders = self.db.execute(
            "SELECT COALESCE(SUM(is_dir = 0), 0), COALESCE(SUM(is_dir = 1), 0) FROM entries").fetchone()
        roots = {path: state for path, state in self.db.execute("SELECT path, state FROM roots")}
        trouble = self.db.execute("SELECT COUNT(*) FROM trouble").fetchone()[0]
        return {"files": files, "folders": folders, "roots": roots, "trouble": trouble}

    def search(self, text: str, kinds: set[str] | None = None, limit: int = DEFAULT_LIMIT,
               roots: list[str] | None = None, exclusions: list[str] | None = None) -> list[NameHit]:
        """이름·상대 폴더 경로의 부분 문자열 검색. `roots`/`exclusions`를 주면 그 범위만(화면의 폴더 목록과 같게)."""
        if self.db is None and not self.ready():
            return []
        # 제어 문자는 버린다: 저장된 검색 문자열의 구분자(char(1))와 겹치지 않게
        query = " ".join("".join(c for c in fold(text) if c.isprintable() or c.isspace()).split())
        terms = list(dict.fromkeys(query.split()))
        if not terms:
            return []
        matches = " AND ".join("instr(s.f, ?) > 0" for _ in terms)
        name = "substr(s.f, 1, instr(s.f, char(1)) - 1)"
        tier = f"""CASE WHEN s.stem = ? OR {name} = ? THEN 0 WHEN substr(s.f, 1, ?) = ? THEN 1
                       WHEN {' AND '.join(f"instr({name}, ?) > 0" for _ in terms)} THEN 2 ELSE 3 END"""
        tier_params = [query, query, len(query), query, *terms]
        # 순서: 단계(tier), 이름 길이, 등록 순서(id). 경로 순서로 정렬하면 맞은 행마다 넓은 표를 읽어야 해서 느렸다.
        order = "ORDER BY tier, instr(s.f, char(1)), s.id"
        rows = None
        if kinds is None:
            # 빠른 길: 좁은 표만으로 순위를 매겨 후보를 고른 뒤 그 행만 읽고 범위를 확인한다. 이름 목록은 NameService가
            # 범위에 맞춰 두므로 보통 후보가 모자라지 않는다. 모자라면(범위를 막 바꿈) 아래의 정확한 질의로 다시 찾는다.
            # 30만 개에서 흔한 단어 75→49ms, 'a' 153→103ms (2026-10-14 측정).
            wanted = limit * CANDIDATES
            picked = self.db.execute(f"SELECT s.id, {tier} AS tier FROM search s WHERE {matches} {order} LIMIT ?",
                                     [*tier_params, *terms, wanted]).fetchall()
            found = {}
            for start in range(0, len(picked), 500):
                chunk = [id_ for id_, _ in picked[start:start + 500]]
                for row in self.db.execute(f"""SELECT id, path, name, is_dir, kind, size, mtime FROM entries
                                               WHERE id IN ({','.join('?' * len(chunk))})""", chunk):
                    found[row[0]] = row[1:]
            rows = [(*found[id_], t) for id_, t in picked if id_ in found and
                    (roots is None or scope.allows(found[id_][0], roots, exclusions or []))][:limit]
            if len(rows) < limit and len(picked) == wanted:
                rows = None
        if rows is None:
            where, params = [matches], list(terms)
            if kinds is not None:
                where.append(f"e.kind IN ({','.join('?' * len(kinds))})")
                params += sorted(kinds)
            if roots is not None:
                # 범위는 경로 범위 비교로 SQL 안에서 건다. 행마다 Python 함수를 부르면 흔한 글자 하나("a")로 찾을 때
                # 1만 개에서 28ms였다(2026-10-14 측정). path 열은 NOCASE라 대소문자를 가리지 않는다.
                inside = []
                for root in outer_roots(roots):
                    low, high = _prefix_range(root)
                    inside.append("(e.path >= ? AND e.path < ?)")
                    params += [low, high]
                where.append(f"({' OR '.join(inside) or '0'})")
                for excluded in exclusions or []:
                    low, high = _prefix_range(os.path.abspath(excluded))
                    where.append("NOT (e.path = ? OR (e.path >= ? AND e.path < ?))")
                    params += [os.path.abspath(excluded), low, high]
            # CROSS JOIN: 좁은 표를 먼저 훑고 맞은 행만 entries에서 읽는다(경로 범위 색인부터 도는 계획을 막는다)
            rows = self.db.execute(
                f"""SELECT e.path, e.name, e.is_dir, e.kind, e.size, e.mtime, {tier} AS tier
                    FROM search s CROSS JOIN entries e ON e.id = s.id WHERE {' AND '.join(where)} {order} LIMIT ?""",
                [*tier_params, *params, limit]).fetchall()
        unavailable = [p for (p,) in self.db.execute("SELECT path FROM roots WHERE state != 'ok'")]
        trouble = [p for (p,) in self.db.execute("SELECT path FROM trouble")]
        hits = []
        for path, name, is_dir, kind, size, mtime, tier in rows:
            status = None
            if any(scope.contains(r, path) for r in unavailable):
                status = "unavailable"
            elif any(scope.contains(t, path) for t in trouble):
                status = "unknown"
            hits.append(NameHit(path, name, bool(is_dir), kind, size, mtime, tier, status))
        return hits
