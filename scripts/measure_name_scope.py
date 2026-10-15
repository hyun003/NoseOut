"""이름 검색 범위 성능 점검(2026-10-14): 처음 이름 등록, 재실행 때 확인 시간, 등록 중 검색 지연, CPU·메모리, 색인 현황 세기.

usage:
  .venv\\Scripts\\python scripts\\measure_name_scope.py --synthetic 100000     # .tmp 합성 폴더 (먼저 검증)
  .venv\\Scripts\\python scripts\\measure_name_scope.py --real                 # 앱 설정에서 이미 고른 이름 검색 범위만

- 실제 측정(--real)은 앱 설정 폴더(%LOCALAPPDATA%\\NoseOut, NOSEOUT_HOME, 아직 옮기지 않은 옛 DigUpWin)의 settings.json의
  name_folders와 제외 폴더만 쓴다. 설정 폴더를 옮기지는 않는다. 설정에
  범위가 없으면 아무것도 걷지 않고 끝낸다(범위를 넓히거나 고르지 않는다). 파일 이름·경로·크기·시각만 읽고 내용은 열지 않는다.
- 이름 목록은 .tmp\\name-scope-measure\\의 별도 names.sqlite에 쓴다: 앱의 기존 색인·설정은 읽기만 하고 바꾸지 않는다.
- 결과(results/name-scope-<mode>.json)에는 개수·시간·자원만 남긴다. 파일 이름·절대 경로·질의 결과 이름은 기록하지 않는다
  (범위는 '드라이브 루트인가'만).
- OS 파일 캐시는 비우지 못한다(관리자 권한 필요): 첫 등록이 '차가운 디스크' 기준이라는 보장은 없다.
"""

from __future__ import annotations

import argparse
import json
import random
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

from noseout import paths, rules, stats  # noqa: E402
from noseout.names import NameIndex  # noqa: E402
from noseout.store import IndexStore  # noqa: E402

WORK = ROOT / ".tmp" / "name-scope-measure"
QUERIES = ("a", "보고", "report", "2024", "img", "zzzz없음")   # 이름이 아닌 일반 단어만(결과 이름은 남기지 않는다)


def settings_scope() -> tuple[list[str], list[str]]:
    base = paths.resolve(do_migrate=False)
    path = base / "settings.json"
    if not path.exists():
        return [], []
    data = json.loads(path.read_text(encoding="utf-8"))   # 읽기만
    return [str(f) for f in data.get("name_folders", [])], [str(f) for f in data.get("exclusions", [])]


def make_synthetic(size: int) -> Path:
    folder = WORK / f"synthetic-{size}"
    if (folder / ".complete").exists():
        return folder
    shutil.rmtree(folder, ignore_errors=True)      # 이 스크립트가 만든 합성 폴더만
    rng = random.Random(size)
    words = ["보고서", "계약서", "report", "invoice", "사진", "img", "memo", "budget", "2024", "회의록"]
    made = 0
    while made < size:
        sub = folder / f"g{made // 2000:03d}" / f"{rng.choice(words)}_{made // 40:05d}"
        sub.mkdir(parents=True, exist_ok=True)
        for _ in range(40):
            (sub / f"{rng.choice(words)}_{made}{rng.choice(['.txt', '.pdf', '.jpg', '.docx', '.mp4'])}").touch()
            made += 1
    (folder / ".complete").touch()
    return folder


def measure(roots: list[str], exclusions: list[str], mode: str) -> dict:
    db = WORK / f"names-{mode}.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    out: dict = {"mode": mode, "roots": [{"drive_root": rules.is_drive_root(r)} for r in roots],
                 "exclusions": len(exclusions)}
    during: list[float] = []
    done = threading.Event()
    first_hit: list[float] = []
    started = time.perf_counter()

    def poll() -> None:
        reader = NameIndex(db, readonly=True)
        while not reader.ready() and not done.is_set():
            time.sleep(0.01)
        i = 0
        while not done.is_set():
            t = time.perf_counter()
            hits = reader.search(QUERIES[i % len(QUERIES)], roots=roots, exclusions=exclusions)
            during.append((time.perf_counter() - t) * 1000)
            if hits and not first_hit:
                first_hit.append(time.perf_counter() - started)
            i += 1
            time.sleep(0.01)
        reader.close()

    memory_before = memory()
    cpu_before = cpu_seconds()
    reader_thread = threading.Thread(target=poll, daemon=True)
    reader_thread.start()
    with NameIndex(db) as index:
        report = index.scan([], exclusions, name_roots=roots)
        counts = index.counts()
    first_s = time.perf_counter() - started
    cpu_first = cpu_seconds() - cpu_before
    done.set()
    reader_thread.join()
    out["first_registration"] = {
        "seconds": round(first_s, 2), "cpu_seconds": round(cpu_first, 2),
        "cpu_percent_of_one_core": round(100 * cpu_first / max(first_s, 1e-9), 1),
        "files": counts["files"], "folders": counts["folders"], "unreadable_paths": report.trouble,
        "first_hit_s": round(first_hit[0], 3) if first_hit else None,
        "search_during_registration": percentiles(during),
        "memory_before": memory_before, "memory_after": memory()}
    # 재실행: 앱을 다시 켰을 때처럼 같은 범위를 다시 맞춘다(바뀐 것이 거의 없음)
    cpu_before = cpu_seconds()
    t = time.perf_counter()
    with NameIndex(db) as index:
        again = index.scan([], exclusions, name_roots=roots)
    rescan_s = time.perf_counter() - t
    out["restart_check"] = {"seconds": round(rescan_s, 2), "cpu_seconds": round(cpu_seconds() - cpu_before, 2),
                            "added": again.added, "removed": again.removed}
    with NameIndex(db, readonly=True) as index:
        index.ready()
        after = {}
        for q in QUERIES:
            runs = []
            for _ in range(5):
                t = time.perf_counter()
                index.search(q, roots=roots, exclusions=exclusions)
                runs.append((time.perf_counter() - t) * 1000)
            after[f"query{QUERIES.index(q) + 1}"] = round(sorted(runs)[2], 2)
    out["search_after_ms_median"] = after
    out["names_db_bytes"] = sum(Path(f"{db}{s}").stat().st_size for s in ("", "-wal") if Path(f"{db}{s}").exists())
    out["memory_peak"] = memory()
    return out


def measure_stats(size: int) -> dict:
    """설정 > 색인 현황 세기: 합성 AI 색인 행 size개(파일 내용 없음)와 위의 이름 목록으로 stats.collect 시간."""
    base = WORK / "stats-home"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    root = "D:\\synthetic"
    states = ["done"] * 7 + ["pending", "partial", "failed"]
    with IndexStore(base / "index.sqlite") as store:
        store.db.executemany(
            "INSERT INTO files(path, kind, size, mtime, inode, device, state, detail, indexed_at) "
            "VALUES(?, 'doc', 1, 1.0, 0, 0, ?, ?, ?)",
            [(f"{root}\\g{i // 1000}\\f{i}.txt", states[i % 10], "합성 실패" if i % 10 == 9 else None, 1.7e9)
             for i in range(size)])
        store.commit()
    names_db = next(WORK.glob("names-*.sqlite"), None)
    if names_db:
        shutil.copy2(names_db, base / "names.sqlite")
    runs = []
    for _ in range(3):
        t = time.perf_counter()
        data = stats.collect(base, [root], [], [], [f"{root}\\g5"])
        runs.append(time.perf_counter() - t)
    return {"index_rows": size, "seconds_median": round(sorted(runs)[1], 3), "counted_done": data["ai"]["done"],
            "counted_failed": data["ai"]["failed"], "failures_listed": len(data["ai"]["failures"])}


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--synthetic", type=int)
    group.add_argument("--real", action="store_true")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    if args.real:
        roots, exclusions = settings_scope()
        if not roots:
            print("앱 설정에 이미 고른 이름 검색 범위가 없습니다. 실제 측정을 하지 않았습니다(범위를 정해 주세요).")
            return 2
        result = measure(roots, exclusions, "real")
    else:
        folder = make_synthetic(args.synthetic)
        result = measure([str(folder)], [], "synthetic")
        result["synthetic_size"] = args.synthetic
        result["stats_collect"] = measure_stats(args.synthetic)
    result["measured_at"] = time.strftime("%Y-%m-%d %H:%M")
    result["note"] = ("OS 파일 캐시를 비우지 않음(관리자 권한 필요). 이름 목록은 .tmp의 별도 파일. 파일 이름·경로는 기록하지 않음.")
    out = ROOT / "results" / f"name-scope-{result['mode']}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
