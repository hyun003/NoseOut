"""이름 목록 방식 비교(2026-10-15): 폴더 순회 vs USN(일반 권한 변경 기록) vs MFT 레코드 처리. 합성 파일만(.tmp).

usage: .venv\\Scripts\\python scripts\\measure_ntfs.py [--size 100000] [--changes 1000]

- 같은 합성 폴더(.tmp\\ntfs-measure\\synthetic-<N>, 0바이트 파일)를 이름 범위로 잡고 측정 DB는 .tmp에 따로 만든다.
- 폴더 순회: 처음 등록, 재실행 확인(전체 다시 걷기), 변경 N개 뒤 재실행 확인.
- USN(실제 API, 일반 권한): 처음 등록(= 폴더 순회 + 등록 중 변경 따라잡기), 재실행 따라잡기(변경 없음 / 변경 N개), 실행 중 변경 1개 반영.
- MFT: 관리자 권한이 필요해 실제 열거는 하지 않는다. 같은 폴더를 MFT 레코드 모양으로 바꾼 합성 레코드를 import_mft가 처리하는
  시간만 잰다(실제 MFT 열거·전송 시간은 포함하지 않음).
- 결과: results/ntfs-bench.json. 파일 이름·절대 경로는 남기지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from measure_drive import cpu_seconds  # noqa: E402
from measure_names import memory, percentiles  # noqa: E402
from measure_name_scope import make_synthetic as _make  # noqa: E402

import measure_name_scope  # noqa: E402
from noseout import names, ntfs  # noqa: E402

WORK = ROOT / ".tmp" / "ntfs-measure"
QUERIES = ("보고", "report", "2024", "img", "zzzz없음")


def fresh_db(name: str) -> Path:
    db = WORK / name
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    return db


def db_bytes(db: Path) -> int:
    return sum(Path(f"{db}{s}").stat().st_size for s in ("", "-wal") if Path(f"{db}{s}").exists())


def searching(db: Path, root: str, done: threading.Event, out: list[float]) -> threading.Thread:
    def run():
        reader = names.NameIndex(db, readonly=True)
        while not reader.ready() and not done.is_set():
            time.sleep(0.01)
        i = 0
        while not done.is_set():
            t = time.perf_counter()
            reader.search(QUERIES[i % len(QUERIES)], roots=[root], exclusions=[])
            out.append((time.perf_counter() - t) * 1000)
            i += 1
            time.sleep(0.01)
        reader.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def timed(work) -> dict:
    cpu, t = cpu_seconds(), time.perf_counter()
    work()
    return {"seconds": round(time.perf_counter() - t, 3), "cpu_seconds": round(cpu_seconds() - cpu, 3)}


def make_changes(folder: Path, count: int, tag: str) -> None:
    target = folder / f"changes_{tag}"
    target.mkdir(exist_ok=True)
    for i in range(count):
        (target / f"변경_{i}.txt").touch()


def first_registration(db: Path, root: str, usn: bool) -> tuple[dict, names.NameIndex, ntfs.NtfsNames | None]:
    during: list[float] = []
    done = threading.Event()
    reader = searching(db, root, done, during)
    memory_before = memory()
    index = names.NameIndex(db)
    engine = ntfs.NtfsNames(index) if usn else None

    def work():
        report = names.ScanReport()
        if usn:
            index.set_scope([], [], name_roots=[root])
            engine.sync("auto", report, walk=lambda roots: index.scan([], [], name_roots=[root], only=roots,
                                                                       report=report))
        else:
            index.scan([], [], name_roots=[root])

    result = timed(work)
    done.set()
    reader.join()
    counts = index.counts()
    result.update(files=counts["files"], folders=counts["folders"], search_during_registration=percentiles(during),
                  memory_before=memory_before, memory_after=memory(), db_bytes=db_bytes(db))
    return result, index, engine


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=100_000)
    parser.add_argument("--changes", type=int, default=1000)
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    measure_name_scope.WORK = WORK
    folder = _make(args.size)
    root = str(folder)
    volume, reason = ntfs.volume_of(root)
    out: dict = {"size": args.size, "changes": args.changes, "volume_usn": volume is not None, "volume_reason": reason,
                 "note": "합성 0바이트 파일, OS 파일 캐시를 비우지 않음(관리자 권한 필요). 파일 이름·경로는 기록하지 않음."}

    # 폴더 순회
    walk_db = fresh_db("walk.sqlite")
    first, index, _ = first_registration(walk_db, root, usn=False)
    out["walk"] = {"first_registration": first}
    out["walk"]["restart_unchanged"] = timed(lambda: index.scan([], [], name_roots=[root]))
    make_changes(folder, args.changes, "walk")
    out["walk"]["restart_after_changes"] = timed(lambda: index.scan([], [], name_roots=[root]))
    index.close()

    # USN (실제 API, 일반 권한)
    if volume is not None:
        usn_db = fresh_db("usn.sqlite")
        first, index, engine = first_registration(usn_db, root, usn=True)
        out["usn"] = {"first_registration": first}
        engine.close()

        def restart():
            report = names.ScanReport()
            again = ntfs.NtfsNames(index)
            walked = []
            again.sync("auto", report, walk=lambda roots: walked.append(roots))
            again.close()
            if walked:
                raise RuntimeError("restart unexpectedly walked")

        out["usn"]["restart_unchanged"] = timed(restart)
        make_changes(folder, args.changes, "usn")
        out["usn"]["restart_after_changes"] = timed(restart)
        out["usn"]["files_after_changes"] = index.counts()["files"]
        live = ntfs.NtfsNames(index)
        live.poll(names.ScanReport())
        (folder / "changes_usn" / "live_one.txt").touch()
        t = time.perf_counter()
        while time.perf_counter() - t < 5:
            live.poll(names.ScanReport())
            if index.db.execute("SELECT 1 FROM entries WHERE name = 'live_one.txt'").fetchone():
                break
        out["usn"]["live_change_visible_ms"] = round((time.perf_counter() - t) * 1000, 1)
        polls = []
        for _ in range(20):
            p = time.perf_counter()
            live.poll(names.ScanReport())
            polls.append((time.perf_counter() - p) * 1000)
        out["usn"]["idle_poll"] = percentiles(polls)
        live.close()
        index.close()

    # MFT 레코드 처리 (합성 레코드, 실제 열거 시간 미포함)
    sys.path.insert(0, str(ROOT / "tests"))
    from test_ntfs import mft_records  # noqa: E402

    records, _ = mft_records(folder, os.path.splitdrive(root)[0] + "\\")
    mft_db = fresh_db("mft.sqlite")
    index = names.NameIndex(mft_db)
    index.set_scope([], [], name_roots=[root])
    memory_before = memory()
    result = timed(lambda: index.import_mft(os.path.splitdrive(root)[0] + "\\", [root],
                                           (records[i:i + 5000] for i in range(0, len(records), 5000)),
                                           names.ScanReport()))
    counts = index.counts()
    result.update(records=len(records), files=counts["files"], folders=counts["folders"], memory_before=memory_before,
                  memory_after=memory(), db_bytes=db_bytes(mft_db),
                  note="관리자 도우미의 실제 MFT 열거·파이프 전송 시간은 포함하지 않음(UAC 필요, 미측정)")
    out["mft_records_processing"] = result
    index.close()
    out["measured_at"] = time.strftime("%Y-%m-%d %H:%M")
    (ROOT / "results" / "ntfs-bench.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    shutil.rmtree(folder / "changes_walk", ignore_errors=True)
    shutil.rmtree(folder / "changes_usn", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
