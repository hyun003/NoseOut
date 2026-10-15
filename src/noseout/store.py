"""SQLite 색인 하나: 파일, 구간(벡터 768×float16), FTS5 키워드 색인. 원본 IndexStore.swift의 스키마를 따른다.
Windows에서는 inode/device 자리에 NTFS file ID와 볼륨 번호(os.stat의 st_ino, st_dev)를 넣는다."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from . import search_text

SCHEMA_VERSION = 1
DIM = 768
TOKENIZER = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'"
FTS_COLUMNS = f"name, body, original UNINDEXED, tokenize = \"{TOKENIZER}\""


def signed64(value: int) -> int:
    """SQLite INTEGER(부호 있는 64비트)에 들어가게. NTFS 볼륨 번호는 2^63을 넘고, ReFS file ID는 128비트일 수 있다.
    같은 값은 늘 같은 결과가 되므로 변경 감지에는 충분하다."""
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= 1 << 63 else value


class IndexStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
            self._create()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _create(self) -> None:
        self.db.executescript(f"""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files(
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                inode INTEGER NOT NULL,
                device INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',   -- pending | partial | done | skipped | failed
                detail TEXT,
                info TEXT,                                -- JSON: width, height, taken, pages, chunks
                indexed_at REAL
            );
            CREATE INDEX files_inode ON files(device, inode);
            CREATE INDEX files_state ON files(state, kind);
            CREATE TABLE segments(
                id INTEGER PRIMARY KEY,
                file INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,       -- name | image | ocr | page | page_image | chunk | audio | frame
                modality TEXT NOT NULL,   -- none | text | image | audio
                loc REAL,                 -- PDF 페이지 번호, 문서 청크 번호, 음성·영상의 시작 초
                loc_end REAL,
                excerpt TEXT,
                vec BLOB                  -- 768 × float16, L2 정규화
            );
            CREATE INDEX segments_file ON segments(file);
            CREATE VIRTUAL TABLE fts USING fts5({FTS_COLUMNS});
            PRAGMA user_version = {SCHEMA_VERSION};
        """)
        self.db.commit()

    # meta

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        self.db.commit()

    # files

    def files_under(self, root: str) -> dict[str, tuple[int, int, float, int]]:
        """root 아래 파일: path → (id, size, mtime, inode)."""
        # Windows 경로는 대소문자를 가리지 않는다: 폴더 이름의 대소문자만 바뀌어도 기존 행을 찾는다.
        # '\'(0x5C) 다음 글자가 ']'(0x5D)라서 이 범위는 "C:\data\…"만 잡고 "C:\data2\…"는 잡지 않는다.
        prefix = root.rstrip("\\/") + "\\"
        rows = self.db.execute("""SELECT id, path, size, mtime, inode FROM files
                                  WHERE path >= ? COLLATE NOCASE AND path < ? COLLATE NOCASE""",
                               (prefix, prefix[:-1] + "]"))
        return {path: (id_, size, mtime, inode) for id_, path, size, mtime, inode in rows}

    def file_at(self, path: str) -> tuple[str, tuple[int, int, float, int]] | None:
        """경로 하나의 행: (저장된 경로, (id, size, mtime, inode)). 감시가 바뀐 파일 하나만 맞출 때 쓴다."""
        row = self.db.execute("SELECT id, path, size, mtime, inode FROM files WHERE path = ? COLLATE NOCASE",
                              (path,)).fetchone()
        return (row[1], (row[0], row[2], row[3], row[4])) if row else None

    def upsert_file(self, path: str, kind: str, size: int, mtime: float, inode: int, device: int) -> int:
        """새 파일이거나 바뀐 파일: 대기 상태로 두고 이름 구간만 남긴다."""
        inode, device = signed64(inode), signed64(device)
        row = self.db.execute("SELECT id FROM files WHERE path = ? COLLATE NOCASE", (path,)).fetchone()
        if row:
            file_id = row[0]
            self._delete_segments(file_id)
            self.db.execute("""UPDATE files SET path=?, kind=?, size=?, mtime=?, inode=?, device=?, state='pending',
                               detail=NULL, info=NULL, indexed_at=NULL WHERE id=?""",
                            (path, kind, size, mtime, inode, device, file_id))
        else:
            file_id = self.db.execute("""INSERT INTO files(path, kind, size, mtime, inode, device)
                                         VALUES(?, ?, ?, ?, ?, ?)""",
                                      (path, kind, size, mtime, inode, device)).lastrowid
        self._add_name_row(file_id, path)
        return file_id

    def remove_file(self, file_id: int) -> None:
        self._delete_segments(file_id)
        self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def pending(self) -> list[tuple[int, str, str]]:
        order = "CASE kind WHEN 'screenshot' THEN 0 WHEN 'image' THEN 1 WHEN 'pdf' THEN 2 WHEN 'doc' THEN 3 ELSE 4 END"
        return self.db.execute(f"SELECT id, path, kind FROM files WHERE state = 'pending' ORDER BY {order}, path") \
            .fetchall()

    def partial(self) -> list[tuple[int, str, str]]:
        """첫 단계만 읽은 긴 파일들 (원본 IndexStore.unfinished)."""
        order = "CASE kind WHEN 'pdf' THEN 0 ELSE 1 END"
        return self.db.execute(f"SELECT id, path, kind FROM files WHERE state = 'partial' ORDER BY {order}, path") \
            .fetchall()

    def info(self, file_id: int) -> dict:
        row = self.db.execute("SELECT info FROM files WHERE id = ?", (file_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def finish(self, file_id: int, state: str, detail: str | None = None, info: dict | None = None) -> None:
        self.db.execute("UPDATE files SET state=?, detail=?, info=?, indexed_at=? WHERE id=?",
                        (state, detail, json.dumps(info, ensure_ascii=False) if info else None, time.time(), file_id))

    # segments

    def add_segment(self, file_id: int, kind: str, modality: str, loc: float | None = None,
                    excerpt: str | None = None, text: str | None = None, vector: np.ndarray | None = None,
                    loc_end: float | None = None) -> int:
        blob = None
        if vector is not None:
            blob = np.asarray(vector, dtype=np.float16).tobytes()
        segment = self.db.execute(
            "INSERT INTO segments(file, kind, modality, loc, loc_end, excerpt, vec) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (file_id, kind, modality if vector is not None else "none", loc, loc_end, excerpt, blob)).lastrowid
        if text:
            normalized = search_text.normalized(text)
            self.db.execute("INSERT INTO fts(rowid, name, body, original) VALUES(?, '', ?, ?)",
                            (segment, normalized, None if normalized == text else text))
        return segment

    def _add_name_row(self, file_id: int, path: str) -> None:
        segment = self.db.execute("INSERT INTO segments(file, kind, modality) VALUES(?, 'name', 'none')",
                                  (file_id,)).lastrowid
        p = Path(path)
        # 파일 이름은 name 열(가중치 3), 폴더 이름은 body 열(가중치 1)
        self.db.execute("INSERT INTO fts(rowid, name, body) VALUES(?, ?, ?)",
                        (segment, search_text.normalized(p.name), search_text.normalized(" ".join(p.parent.parts[-3:]))))

    def clear_content(self, file_id: int) -> None:
        """이름 구간만 남기고 내용 구간을 지운다(커밋하지 않음: 다시 읽기와 한 트랜잭션)."""
        self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE file = ? AND kind != 'name')",
                        (file_id,))
        self.db.execute("DELETE FROM segments WHERE file = ? AND kind != 'name'", (file_id,))

    def _delete_segments(self, file_id: int) -> None:
        self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE file = ?)", (file_id,))
        self.db.execute("DELETE FROM segments WHERE file = ?", (file_id,))

    def commit(self) -> None:
        self.db.commit()

    # 통계

    def counts(self) -> dict:
        by_kind: dict = {}
        for kind, state, n in self.db.execute("SELECT kind, state, COUNT(*) FROM files GROUP BY kind, state"):
            by_kind.setdefault(kind, {})[state] = n
        segments, vectors = self.db.execute(
            "SELECT COUNT(*), COUNT(vec) FROM segments WHERE kind != 'name'").fetchone()
        return {"files": by_kind, "segments": segments, "vectors": vectors}
