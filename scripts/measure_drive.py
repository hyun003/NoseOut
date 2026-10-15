"""드라이브 전체 이름 검색 측정: .tmp/names-bench/drive-<N>의 합성 폴더를 '드라이브 루트'로 다룬다(실제 드라이브는 걷지 않음).
결과: results/drive-bench.json.

usage: .venv\\Scripts\\python scripts\\measure_drive.py [--size 100000] [--clean]

- 합성 드라이브: Users\\me 아래 문서·바탕 화면·다운로드·사진·작업 폴더(이름 목록에 들어감)와 Windows·Program Files·ProgramData·
  AppData(이름 목록에서 빠짐)에 0바이트 파일. 시스템 폴더 환경 변수와 사용자 폴더를 이 안으로 돌리고 rules.is_drive_root가 이
  폴더를 드라이브 루트로 보게 한다(이 프로세스 안에서만).
- 잰다: 등록 시간·항목 수·빠진 시스템 폴더, 등록 중 검색 지연, 일시정지가 걸리는 시간·이어서 끝나는 시간, 앱 전체(실제
  ReadDirectoryChangesW 감시)에서 시스템 폴더 쓰기 폭주·사용자 폴더 변경 때의 CPU·메모리·이름 반영 지연, 변경이 없을 때의 CPU,
  종료 시간과 남은 감시 스레드. CPU는 GetProcessTimes(이 프로세스, 검색 엔진은 띄우지 않음).
- OS 파일 캐시는 비우지 못한다(관리자 권한 필요): 합성 데이터를 만든 직후의 따뜻한 캐시 기준이다.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from measure_names import KO, EN, EXTS, memory, percentiles  # noqa: E402

BENCH = ROOT / ".tmp" / "names-bench"


def cpu_seconds() -> float:
    k = ctypes.WinDLL("kernel32")
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    k.GetProcessTimes(k.GetCurrentProcess(), *[ctypes.byref(t) for t in times])
    return sum((t.dwHighDateTime << 32 | t.dwLowDateTime) / 1e7 for t in times[2:])


def make_drive(size: int) -> Path:
    drive = BENCH / f"drive-{size}"
    if (drive / ".complete").exists():
        return drive
    shutil.rmtree(drive, ignore_errors=True)
    rng = random.Random(size)
    me = drive / "Users" / "me"
    user = [me / n for n in ("Documents", "Desktop", "Downloads", "Pictures", "Projects")]
    system = [drive / "Windows" / "System32", drive / "Program Files" / "Vendor", drive / "ProgramData" / "Cache",
              me / "AppData" / "Local" / "Browser" / "Cache", me / "AppData" / "Roaming" / "App"]
    made = 0
    while made < size:
        # 80%는 사용자 폴더, 20%는 시스템 폴더(이름 목록에서 빠져야 함)
        base = rng.choice(user) if rng.random() < 0.8 else rng.choice(system)
        folder = base / f"{rng.choice(KO)}_{rng.randrange(400):03d}" / f"{rng.choice(EN)} {rng.randrange(10)}"
        folder.mkdir(parents=True, exist_ok=True)
        for _ in range(min(25, size - made)):
            (folder / f"{rng.choice(KO)}_{rng.choice(EN)}_{made}.{rng.choice(EXTS)}").touch()
            made += 1
    (drive / "Recovery").mkdir(exist_ok=True)
    (drive / "Recovery" / "winre.wim").touch()
    (me / "Documents" / "고유표식_drive.pdf").touch()
    (drive / ".complete").touch()
    return drive


def pretend_drive(drive: Path) -> None:
    from noseout import rules

    me = drive / "Users" / "me"
    # 환경 변수(SystemRoot 등)를 바꾸면 Winsock 등 Windows 구성요소가 깨진다: 시스템 폴더 목록 함수만 돌린다
    system = [drive / "Windows", drive / "Program Files", drive / "Program Files (x86)", drive / "ProgramData",
              me / "AppData" / "Local", me / "AppData" / "Roaming", me / "AppData"]
    for folder in system:
        folder.mkdir(parents=True, exist_ok=True)
    rules._system_folders = lambda: [str(f) for f in system]
    real = rules.is_drive_root
    rules.is_drive_root = lambda p: os.path.normcase(str(Path(p).resolve())) == os.path.normcase(str(drive)) or real(p)


def measure_names(drive: Path, out: dict) -> None:
    from noseout import services
    from noseout.names import NameIndex

    db = BENCH / "drive-names.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    docs = drive / "Users" / "me" / "Documents"
    service = services.NameService(db)
    finished: list[dict] = []
    service.finished.connect(lambda s: finished.append(s))
    during, done = [], threading.Event()
    first: dict[str, float] = {}
    started = time.perf_counter()

    def poll():
        reader = NameIndex(db, readonly=True)
        i = 0
        while not done.is_set():
            q = ("보고", "report", "고유표식", "invoice")[i % 4]
            i += 1
            t = time.perf_counter()
            hits = reader.search(q, roots=[str(drive)], exclusions=[])
            during.append((time.perf_counter() - t) * 1000)
            if hits:
                first.setdefault("any", time.perf_counter() - started)
            if any(h.name.startswith("고유표식") for h in hits):
                first.setdefault("documents_marker", time.perf_counter() - started)
            time.sleep(0.005)
        reader.close()

    memory_before = memory()
    reader_thread = threading.Thread(target=poll, daemon=True)
    reader_thread.start()
    walker = threading.Thread(target=service.scan, args=([str(docs)], [], [], [str(drive)]), daemon=True)
    walker.start()
    # 등록이 진행되는 도중(약 1/3 지점) 일시정지 → 2초 → 이어하기
    while time.perf_counter() - started < 600 and walker.is_alive():
        index = NameIndex(db, readonly=True)
        count = index.counts()["files"]
        index.close()
        if count > 0.2 * out["size"]:
            break
        time.sleep(0.05)
    asked = time.perf_counter()
    service.request_pause()
    while not service.paused and walker.is_alive():
        time.sleep(0.001)
    paused_after = time.perf_counter() - asked
    index = NameIndex(db, readonly=True)
    frozen = index.counts()["files"]
    time.sleep(2.0)
    still = index.counts()["files"]
    index.close()
    service.resume()
    walker.join()
    total = time.perf_counter() - started
    done.set()
    reader_thread.join()
    with NameIndex(db, readonly=True) as index:
        index.ready()
        counts = index.counts()
        excluded = index.db.execute(
            "SELECT COUNT(*) FROM entries WHERE path LIKE ? OR path LIKE ? OR path LIKE ? OR path LIKE ? OR path LIKE ?",
            [f"%\\{n}%" for n in ("Windows", "Program Files", "ProgramData", "AppData", "Recovery")]).fetchone()[0]
        per_query = {}
        for q in ("보고", "청구서", "report", "invoice 2", "a", "zzzz없음", "documents 고유표식"):
            runs = []
            for _ in range(5):
                t = time.perf_counter()
                index.search(q, roots=[str(drive)], exclusions=[])
                runs.append((time.perf_counter() - t) * 1000)
            per_query[q] = round(sorted(runs)[2], 2)
    out["names"] = {
        "registered_s_including_2s_pause": round(total, 2), "registered_s_without_pause": round(total - 2.0, 2),
        "files": counts["files"], "folders": counts["folders"], "system_entries_listed": excluded,
        "first_result_s": {k: round(v, 3) for k, v in first.items()},
        "pause_took_ms": round(paused_after * 1000, 1), "count_stayed_while_paused": frozen == still,
        "search_during_registration": percentiles(during), "search_after_ms": per_query,
        "memory_before": memory_before, "memory_after": memory(), "walker_done": not walker.is_alive(),
        "db_bytes": sum(Path(f"{db}{s}").stat().st_size for s in ("", "-wal") if Path(f"{db}{s}").exists())}
    # 다시 확인(앱 재시작): 바뀐 것 없음
    service.index = None     # 등록 스레드의 연결은 그 스레드 것이라 여기서 닫지 않는다(프로세스가 끝나며 정리)
    with NameIndex(db) as again:
        t = time.perf_counter()
        again.scan([str(docs)], [], name_roots=[str(drive)])
        out["names"]["rescan_unchanged_s"] = round(time.perf_counter() - t, 2)


def measure_watch(drive: Path, out: dict) -> None:
    """앱 전체: 실제 감시로 드라이브(합성)를 보면서 시스템 폴더 쓰기 폭주와 사용자 폴더 변경을 일으킨다."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from noseout import app as app_module
    from noseout import services
    from noseout.names import NameIndex

    services.SearchService.start = lambda self: None     # 검색 엔진 없이
    qt = QApplication.instance() or QApplication([])
    base = BENCH / "drive-app"
    shutil.rmtree(base, ignore_errors=True)
    docs = drive / "Users" / "me" / "Documents"
    app_module.Settings([str(docs)], name_folders=[str(drive)]).save(base / "settings.json")
    threads_before = threading.active_count()
    application = app_module.App(qt, base, register_hotkey=False, setup=False)
    names_db = base / "names.sqlite"
    finished: list[float] = []
    application.names.finished.connect(lambda s: finished.append(time.perf_counter()))
    started = time.perf_counter()
    application.start()

    def pump(seconds, until=lambda: False):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not until():
            QCoreApplication.processEvents()
            time.sleep(0.002)

    pump(900, lambda: finished)
    result = {"app_registration_s": round(finished[0] - started, 2) if finished else None,
              "watchers": len(application.watch.watchers)}
    pump(1.0)
    # 변경이 없을 때 10초 동안의 CPU
    cpu0, t0 = cpu_seconds(), time.perf_counter()
    pump(10.0)
    result["idle_cpu_percent_of_one_core"] = round((cpu_seconds() - cpu0) / (time.perf_counter() - t0) * 100, 2)
    # 시스템 폴더(앱 데이터 캐시) 쓰기 폭주: 5초 동안 3,000개 만들고 고쳐 쓰기 + 사용자 폴더에 새 파일 200개
    cache = drive / "Users" / "me" / "AppData" / "Local" / "Browser" / "Cache" / "burst"
    cache.mkdir(parents=True, exist_ok=True)
    downloads = drive / "Users" / "me" / "Downloads" / "새로 받은 파일"
    downloads.mkdir(parents=True, exist_ok=True)
    cpu0, t0, mem0 = cpu_seconds(), time.perf_counter(), memory()
    for i in range(3000):
        (cache / f"blob_{i}.tmp").write_bytes(b"x" * 64)
        if i % 15 == 0:
            (downloads / f"probe_download_{i // 15:03d}.pdf").touch()
        if i % 100 == 0:
            QCoreApplication.processEvents()
    last_probe = downloads / "probe_download_199.pdf"
    written = time.perf_counter()

    def probe_found():
        try:
            with NameIndex(names_db, readonly=True) as index:
                return bool(index.search("probe_download_199", roots=[str(drive)], exclusions=[]))
        except Exception:  # noqa: BLE001
            return False

    pump(60, probe_found)
    reflected = time.perf_counter() - written
    pump(3.0)
    elapsed = time.perf_counter() - t0
    with NameIndex(names_db, readonly=True) as index:
        index.ready()
        burst_listed = index.db.execute("SELECT COUNT(*) FROM entries WHERE path LIKE ?", ("%\\burst\\%",)).fetchone()[0]
        probes = index.db.execute("SELECT COUNT(*) FROM entries WHERE name LIKE 'probe_download_%'").fetchone()[0]
    result.update({
        "burst": {"system_folder_writes": 3000, "user_folder_new_files": 200,
                  "cpu_seconds": round(cpu_seconds() - cpu0, 2), "wall_seconds": round(elapsed, 2),
                  "cpu_percent_of_one_core": round((cpu_seconds() - cpu0) / elapsed * 100, 1),
                  "last_user_file_searchable_after_s": round(reflected, 2), "user_files_listed": probes,
                  "system_folder_files_listed": burst_listed, "memory_before": mem0, "memory_after": memory()},
        "watch_state": application.watch.state})
    t = time.perf_counter()
    application.quit()
    result["quit_s"] = round(time.perf_counter() - t, 2)
    pump(0.5)
    result["threads_left_after_quit"] = threading.active_count() - threads_before
    result["watchers_left"] = len(application.watch.watchers)
    for widget in qt.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.processEvents()
    shutil.rmtree(cache, ignore_errors=True)
    shutil.rmtree(downloads, ignore_errors=True)
    out["watch"] = result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=100000)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "drive-bench.json")
    args = parser.parse_args()
    if args.clean:
        for item in BENCH.glob("drive-*"):
            shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink()
        return 0
    started = time.perf_counter()
    drive = make_drive(args.size)
    out = {"size": args.size, "made_s": round(time.perf_counter() - started, 1),
           "machine": "Ryzen 7 7800X3D, 64 GB, NVMe, Windows 11 (따뜻한 파일 캐시, 합성 폴더를 드라이브로 취급)"}
    pretend_drive(drive)
    measure_names(drive, out)
    print(json.dumps(out["names"], ensure_ascii=False))
    measure_watch(drive, out)
    print(json.dumps(out["watch"], ensure_ascii=False))
    text = json.dumps(out, indent=2, ensure_ascii=False).replace(json.dumps(str(ROOT))[1:-1] + "\\\\", "")
    args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
