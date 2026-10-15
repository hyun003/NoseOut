"""빠른 이름 검색 측정: 합성 폴더 트리(.tmp/names-bench)로 최초 등록 시간, 등록 중 검색 지연, 첫 검색 결과 시간, 검색 지연,
다시 확인(재실행) 시간, 감시 변경 반영 시간, 메모리를 잰다. 결과: results/names-bench.json.

usage: .venv\\Scripts\\python scripts\\measure_names.py [--sizes 10000 100000] [--clean]

- 입력은 이 스크립트가 .tmp/names-bench/data-<N>에 만드는 0바이트 합성 파일뿐이다(사용자 파일·기존 .index 사용 안 함).
  --clean은 .tmp/names-bench 안의 data-*·app-* 폴더와 이름 목록 파일만 지운다(로그는 남긴다).
- 등록 중 지연: 등록 스레드가 걷는 동안 다른 스레드(앱의 이름 검색 스레드처럼 별도 연결)가 검색을 반복해 걸린 시간을 모은다.
  한 프로세스 안이라 GIL 경쟁도 포함된다(앱과 같은 조건).
- 메모리: 이 프로세스의 Working Set·Private Bytes(GetProcessMemoryInfo, 추가 패키지 없음). OS 파일 캐시는 비우지 못하므로
  (관리자 권한 필요) 데이터를 만든 직후의 따뜻한 캐시 기준이다.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import statistics
import sys
import threading
import time
import unicodedata
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from noseout.names import NameIndex  # noqa: E402

BENCH = ROOT / ".tmp" / "names-bench"
KO = ["보고서", "계약서", "청구서", "회의록", "견적서", "영수증", "사진", "여행", "가족", "학교", "과제", "발표", "예산",
      "세금", "보험", "병원", "의료비", "월세", "관리비", "급여", "명세서", "일정", "메모", "강의", "논문", "자료"]
EN = ["report", "Invoice", "contract", "meeting", "Budget", "photo", "Trip", "family", "school", "Homework", "slides",
      "tax", "insurance", "receipt", "Notes", "draft", "Final", "backup", "project", "design", "summary", "data"]
EXTS = ["pdf", "docx", "xlsx", "pptx", "txt", "md", "jpg", "png", "heic", "mp4", "mp3", "zip", "hwp", "csv", "py",
        "exe", "lnk", "json", "log", "psd"]
QUERIES = ["보고", "청구서", "의료비 영수증", "report", "INVOICE", "Final 2024", "계약서 2023", "trip photo", "tax",
           "고유표식", "zzzz없는이름", "a", "pdf", "2024-03", "회의록 v2", "Homework", "명세", "backup zip",
           "slides final", "학교 과제"]


class _MEMORY(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def memory() -> dict:
    counters = _MEMORY()
    counters.cb = ctypes.sizeof(_MEMORY)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MEMORY), wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    mb = 1024 * 1024
    return {"working_set_mb": round(counters.WorkingSetSize / mb, 1),
            "peak_working_set_mb": round(counters.PeakWorkingSetSize / mb, 1),
            "private_mb": round(counters.PrivateUsage / mb, 1)}


def make_data(size: int) -> Path:
    """폴더 3단계(+일부 더 깊게) 아래 size개의 0바이트 파일. 같은 size면 다시 만들지 않는다."""
    folder = BENCH / f"data-{size}"
    marker = folder / ".complete"
    if marker.exists():
        return folder
    if folder.exists():
        shutil.rmtree(folder)
    rng = random.Random(size)
    per_folder = 30
    made = 0
    index = 0
    started = time.perf_counter()
    while made < size:
        a, b, c = index // 100, index // 10 % 10, index % 10
        depth = [f"{rng.choice(KO)}_{a:03d}", f"{rng.choice(EN)} {b}", f"{2015 + c}"]
        if index % 7 == 0:
            depth += ["보관", "old", "더 깊은 폴더"]
        target = folder.joinpath(*depth)
        target.mkdir(parents=True, exist_ok=True)
        for i in range(min(per_folder, size - made)):
            word = rng.choice(KO) if rng.random() < 0.5 else rng.choice(EN)
            other = rng.choice(EN) if rng.random() < 0.5 else rng.choice(KO)
            name = f"{word}_{other}_{2015 + rng.randrange(10)}-{rng.randrange(1, 13):02d}_v{rng.randrange(1, 4)}" \
                   f"_{made}.{rng.choice(EXTS)}"
            if i == 0 and index % 11 == 0:
                name = unicodedata.normalize("NFD", name)   # macOS에서 온 NFD 한글 이름
            if i == 1 and index % 13 == 0:
                name = ("아주긴이름" * 20)[:180] + f"_{made}.txt"
            (target / name).touch()
            made += 1
        index += 1
    # 찾을 위치를 알고 있는 표식 파일: 처음·중간·마지막에 만든 폴더
    for label, where in (("first", next(folder.iterdir())), ("last", target)):
        (where / f"고유표식_{label}.pdf").touch()
    (folder / ".complete").touch()
    print(f"made {size} files in {time.perf_counter() - started:.1f} s")
    return folder


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(ordered), "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            "max_ms": round(ordered[-1], 2)}


def timed_search(index: NameIndex, query: str, root: Path) -> tuple[float, int]:
    started = time.perf_counter()
    hits = index.search(query, roots=[str(root)], exclusions=[])
    return (time.perf_counter() - started) * 1000, len(hits)


def measure(size: int) -> dict:
    data = make_data(size)
    db = BENCH / f"names-{size}.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    out: dict = {"size": size, "memory_before": memory()}   # 이 프로세스: Python + sqlite3만 (Qt 불러오기 전)

    # 1) 최초 등록 + 등록 중 검색 (별도 연결·별도 스레드)
    writer = NameIndex(db)
    reader = NameIndex(db, readonly=True)
    during: list[float] = []
    first_seen: dict[str, float] = {}
    done = threading.Event()
    started = time.perf_counter()

    def poll():
        queries = ["보고", "report", "고유표식", "청구서 2019"]
        i = 0
        while not done.is_set():
            q = queries[i % len(queries)]
            i += 1
            t = time.perf_counter()
            hits = reader.search(q, roots=[str(data)], exclusions=[])
            during.append((time.perf_counter() - t) * 1000)
            for h in hits:
                if h.name.startswith("고유표식_"):
                    first_seen.setdefault(h.name, time.perf_counter() - started)
            if hits and "any" not in first_seen:
                first_seen["any"] = time.perf_counter() - started
            time.sleep(0.005)
        reader.close()

    thread = threading.Thread(target=poll)
    thread.start()
    report = writer.scan([data], [])
    registered = time.perf_counter() - started
    time.sleep(0.2)
    done.set()
    thread.join()
    out["initial"] = {"seconds": round(registered, 2), "entries": writer.counts(), "folders_walked": report.folders,
                      "first_result_s": {k: round(v, 3) for k, v in first_seen.items()},
                      "search_during_registration": percentiles(during), "memory_after": memory()}

    # 2) 등록 뒤 검색 지연 (질의마다 5회 중앙값). sqlite 연결은 만든 스레드에서만 쓰므로 새로 연다
    reader = NameIndex(db, readonly=True)
    per_query = {}
    for q in QUERIES:
        runs = [timed_search(reader, q, data) for _ in range(5)]
        per_query[q] = {"median_ms": round(statistics.median(r[0] for r in runs), 2), "hits": runs[0][1]}
    out["search"] = {"per_query": per_query,
                     "overall": percentiles([v["median_ms"] for v in per_query.values()]),
                     "memory_after": memory()}

    # 3) 다시 확인(앱 재실행): 바뀐 것 없음
    started = time.perf_counter()
    again = writer.scan([data], [])
    out["rescan_unchanged"] = {"seconds": round(time.perf_counter() - started, 2), "added": again.added,
                               "removed": again.removed}

    # 4) 감시 변경 반영: 파일 추가, 파일 이름 변경, 폴더 이름 변경(하위 항목 수), 삭제
    sample = next(p for p in data.rglob("*") if p.is_dir() and p.name.isdigit())
    events = {}
    new_file = sample / "새파일_watchprobe.txt"
    new_file.touch()
    t = time.perf_counter()
    writer.apply([str(new_file)])
    events["add_file_ms"] = round((time.perf_counter() - t) * 1000, 2)
    renamed = sample / "이름바꿈_watchprobe.txt"
    os.rename(new_file, renamed)
    t = time.perf_counter()
    writer.apply([str(new_file), str(renamed)])
    events["rename_file_ms"] = round((time.perf_counter() - t) * 1000, 2)
    top = next(p for p in data.iterdir() if p.is_dir())
    moved = top.with_name(top.name + "_renamed")
    children = sum(1 for _ in top.rglob("*"))
    os.rename(top, moved)
    t = time.perf_counter()
    writer.apply([str(top), str(moved)])
    events["rename_folder_ms"] = round((time.perf_counter() - t) * 1000, 2)
    events["rename_folder_children"] = children
    events["found_after_folder_rename"] = bool(reader.search(moved.name, roots=[str(data)], exclusions=[]))
    os.rename(moved, top)
    writer.apply([str(moved), str(top)])
    renamed.unlink()
    t = time.perf_counter()
    writer.apply([str(renamed)])
    events["delete_file_ms"] = round((time.perf_counter() - t) * 1000, 2)
    out["watch_apply"] = events

    out["db_bytes"] = sum(Path(f"{db}{s}").stat().st_size for s in ("", "-wal") if Path(f"{db}{s}").exists())
    out["memory_end"] = memory()
    reader.close()
    writer.close()
    return out


def measure_app(size: int) -> dict:
    """실제 앱 경로: 폴더를 고른 순간부터 검색 창에 이름 결과가 처음 보일 때까지, 등록하는 동안 검색 창에서 보낸 이름 검색이
    돌아오는 시간. 검색 엔진은 띄우지 않는다(로딩 중 상태 그대로 = 엔진과 무관함을 함께 확인)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    from noseout import app as app_module
    from noseout import services

    services.SearchService.start = lambda self: None
    qt = QApplication.instance() or QApplication([])
    data = make_data(size)
    base = BENCH / f"app-{size}"
    shutil.rmtree(base, ignore_errors=True)
    application = app_module.App(qt, base, register_hotkey=False)
    application.start()
    window = application.window
    arrivals: dict[int, tuple[float, int]] = {}
    # 검색 스레드에서 결과를 낸 순간을 잰다(화면 그리기 전)
    application.name_search.results.connect(lambda r, t, h: arrivals.setdefault(r, (time.perf_counter(), len(h))),
                                            type=Qt.ConnectionType.DirectConnection)
    finished: list[float] = []
    # 시작 때 빈 목록 확인의 완료 신호는 빼고, 고른 폴더를 등록한 완료만 센다
    application.names.finished.connect(lambda s: finished.append(time.perf_counter()) if s.get("files") else None)
    memory_before = memory()
    started = time.perf_counter()
    application._save_folders([str(data)])
    latencies, first, first_any = [], None, None
    probes = ["고유표식", "보고", "report", "청구서 2019"]
    i = 0
    while time.perf_counter() - started < 600:
        query = probes[i % len(probes)]
        i += 1
        window.box.setText(query)
        window._send()
        request, sent = window.request, time.perf_counter()
        while request not in arrivals and time.perf_counter() - sent < 10:
            QCoreApplication.processEvents()
            time.sleep(0.001)
        QCoreApplication.processEvents()
        if request in arrivals:
            at, count = arrivals[request]
            if not finished:
                latencies.append((at - sent) * 1000)
            if query == "고유표식" and count and first is None:
                first = at - started
            if count and first_any is None:
                first_any = at - started
        if finished and first is not None:
            break
    out = {"first_any_result_s": round(first_any, 3) if first_any else None,
           "marker_found_s": round(first, 3) if first else None,
           "registered_s": round(finished[0] - started, 2) if finished else None,
           "ui_name_search_during_registration": percentiles(latencies),
           "engine_ready": window.engine_ready, "ai_index_created": (base / "index.sqlite").exists(),
           "memory_before": memory_before, "memory_after": memory()}
    application.quit()
    for widget in qt.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.processEvents()
    return out


def measure_refresh(counts: list[int]) -> dict:
    """AI 색인 중 주기적으로 의미 검색 벡터를 다시 읽는 시간(SearchService.refresh). 검색 스레드는 그동안 다음 검색을
    처리하지 못하므로 이 시간이 '분석 중 검색이 막히는 최대 시간'이다. 합성 벡터를 .tmp/names-bench의 별도 색인에 넣는다."""
    import numpy as np

    from noseout import indexer, services
    from noseout.store import IndexStore

    class Unit:
        def embed_texts(self, texts):
            v = np.ones((len(texts), 768), np.float32)
            return v / np.linalg.norm(v[0])

    out = {}
    rng = np.random.default_rng(0)
    for count in counts:
        db = BENCH / f"refresh-{count}.sqlite"
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db}{suffix}").unlink(missing_ok=True)
        with IndexStore(db) as store:
            indexer.check_versions(store)
            files = max(1, count // 10)
            store.db.executemany("INSERT INTO files(id, path, kind, size, mtime, inode, device, state) "
                                 "VALUES(?, ?, 'doc', 1, 1, ?, 1, 'done')",
                                 [(i + 1, f"C:\\bench\\doc{i}.md", i) for i in range(files)])
            vectors = rng.normal(size=(count, 768)).astype(np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            store.db.executemany("INSERT INTO segments(file, kind, modality, loc, vec) VALUES(?, 'chunk', 'text', ?, ?)",
                                 [(i % files + 1, i // files + 1, vectors[i].astype(np.float16).tobytes())
                                  for i in range(count)])
            store.commit()
        search = services.SearchService(db)
        search.embedder = Unit()
        search._open(announce=False)
        for _ in range(5):
            search.refresh()
        started = time.perf_counter()
        search.searcher.search("harbour lighthouse", Unit().embed_texts(["q"])[0])
        search_ms = (time.perf_counter() - started) * 1000
        out[str(count)] = {"refresh": percentiles(search.refresh_ms), "search_ms": round(search_ms, 1)}
        search.embedder = None
        search.shutdown()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db}{suffix}").unlink(missing_ok=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="*", default=[10000])
    parser.add_argument("--app", action="store_true", help="앱 경로(폴더 선택 → 검색 창) 측정도 한다")
    parser.add_argument("--refresh", type=int, nargs="*", default=None, help="분석 중 다시 읽기 측정 벡터 수들")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "names-bench.json")
    args = parser.parse_args()
    BENCH.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for item in BENCH.iterdir():
            if item.is_dir() and item.name.startswith(("data-", "app-")):
                shutil.rmtree(item)
            elif item.name.startswith(("names-", "refresh-")) and ".sqlite" in item.name:
                item.unlink()
        return 0
    results = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    results.setdefault("runs", {})
    if len(args.sizes) > 1:
        # 크기마다 새 프로세스: 앞 측정의 메모리(특히 Qt)가 다음 크기의 메모리 값에 섞이지 않게
        import subprocess

        for size in args.sizes:
            subprocess.run([sys.executable, __file__, "--sizes", str(size), "--out", str(args.out)]
                           + (["--app"] if args.app else []), check=True)
        if args.refresh:
            subprocess.run([sys.executable, __file__, "--sizes", "--refresh", *map(str, args.refresh),
                            "--out", str(args.out)], check=True)
        return 0
    for size in args.sizes:
        result = measure(size)
        if args.app:
            result["app"] = measure_app(size)
            print("app", json.dumps(result["app"], ensure_ascii=False))
        results["runs"][str(size)] = result
        print(json.dumps({k: result[k] for k in ("size", "initial", "rescan_unchanged", "watch_apply", "db_bytes",
                                                 "memory_end")}, ensure_ascii=False, indent=1))
        print("search", json.dumps(result["search"]["overall"]))
    if args.refresh:
        results["ai_refresh"] = measure_refresh(args.refresh)
        print("refresh", json.dumps(results["ai_refresh"]))
    results["machine"] = "Ryzen 7 7800X3D, 64 GB, NVMe, Windows 11 (따뜻한 파일 캐시)"
    # 결과 파일에 사용자 이름이 든 절대 경로를 남기지 않는다(AGENTS.md): 저장소 기준 상대 경로로
    text = json.dumps(results, indent=2, ensure_ascii=False).replace(json.dumps(str(ROOT))[1:-1] + "\\\\", "")
    args.out.parent.mkdir(exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
