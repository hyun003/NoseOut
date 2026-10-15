"""6단계 감시 지연 측정: 파일을 쓴 뒤 (1) 감시가 이벤트를 받기까지, (2) 묶기·안정성 확인 뒤 색인 작업을 넘기기까지,
(3) 색인이 끝나기까지, (4) 실제 검색(텍스트 엔진)에서 내용으로 찾히기까지를 따로 잰다.

입력은 test_data의 합성 파일 사본만, 설정·색인은 .tmp/watch-measure만 쓴다. 기존 .index와 사용자 파일은 건드리지 않는다.
실제 엔진(vendor/llama, models)이 필요하다. 결과: results/stage6-watch.json

    .venv\\Scripts\\python scripts\\measure_watch.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402  (winrt보다 먼저)
from PySide6.QtWidgets import QApplication  # noqa: E402

from noseout import app, watcher  # noqa: E402

TEST_DATA = ROOT / "test_data"
WORK = ROOT / ".tmp" / "watch-measure"


def pump(seconds: float, until=None) -> bool:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QCoreApplication.processEvents()
        if until and until():
            return True
        time.sleep(0.005)
    return bool(until and until())


class Probe:
    """검색 서비스에 직접 질의를 보내 내용 구간(이름 구간 아님)으로 찾히는지 본다."""

    def __init__(self, application):
        self.app = application
        self.request = 10_000
        self.answers: dict[int, list] = {}
        application.search.results.connect(lambda r, _t, hits: self.answers.__setitem__(r, hits))

    def found(self, query: str, name: str, timeout: float = 120) -> float | None:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            self.request += 1
            request = self.request
            self.app.search.latest = request
            self.app.search_request.emit(request, query, None)
            pump(5, lambda: request in self.answers)
            hits = self.answers.pop(request, [])
            if any(Path(h.path).name == name and h.segment != "name" for h in hits[:5]):
                return time.perf_counter()
            pump(0.05)
        return None


FINISHED: list = []   # (시각, 요약): 색인 작업 완료 신호 전체
PROGRESS: list = []   # (시각, 경로, 결과): 파일·긴 파일 단계마다
DISPATCHED: list = []  # (시각, 경로들, 다시 확인할 폴더들)


def measure(application, probe, root: Path, name: str, query: str, write, cold: bool = False) -> dict:
    if cold:
        application.release_engine.emit()
        pump(3, lambda: application.indexing._embedder is None)
    path = str(root / name)

    def result_of():
        return next((s["results"][path] for _, s in reversed(FINISHED) if path in s.get("results", {})), "")

    def done_at():
        return next((t for t, s in FINISHED if s.get("results", {}).get(path, "").startswith("done")), None)

    started = time.perf_counter()
    write(root / name)
    written = time.perf_counter()
    pump(300, lambda: done_at() is not None)
    searchable = probe.found(query, name)
    timeline = application.watch.timeline.get(os.path.normcase(path), {})

    def ms(t):
        return None if t is None else round((t - written) * 1000)

    first_step = next((t for t, p, _r in PROGRESS if p == path), None)
    row = {"file": name, "bytes": (root / name).stat().st_size, "engine": "cold" if cold else "warm",
           "write_ms": round((written - started) * 1000),
           "first_event_ms": ms(timeline.get("event")), "last_event_ms": ms(timeline.get("last")),
           "dispatch_ms": ms(timeline.get("dispatch")), "dispatches": sum(path in d[1] for d in DISPATCHED),
           "first_step_ms": ms(first_step), "indexed_ms": ms(done_at()), "searchable_ms": ms(searchable),
           "steps": sum(p == path for _t, p, _r in PROGRESS), "result": result_of()}
    print(json.dumps(row, ensure_ascii=False), flush=True)
    pump(0.5)
    return row


def copier(source: Path):
    return lambda target: shutil.copyfile(source, target)


def text(words: str):
    return lambda target: target.write_text(words, encoding="utf-8")


def slow_writer(chunks: int, pause: float):
    """복사처럼 여러 번 나눠 쓰는 큰 텍스트: 쓰는 도중에 반쯤 읽지 않는지(안정성 확인) 본다."""
    line = "Routine sensor calibration log line for the tidal observatory, nothing notable here.\n"

    def write(target: Path):
        with open(target, "w", encoding="utf-8") as f:
            for n in range(chunks):
                f.write(line * 2000)
                if n == chunks - 1:
                    f.write("The final paragraph mentions the albatross migration census at Kestrel Point.\n")
                f.flush()
                os.fsync(f.fileno())
                time.sleep(pause)
    return write


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    root, base = WORK / "chosen", WORK / "home"
    root.mkdir(parents=True)
    shutil.copy2(TEST_DATA / "documents" / "tokyo_trip.md", root)
    app.Settings([str(root)]).save(base / "settings.json")
    qt = QApplication.instance() or QApplication([])
    application = app.App(qt, base, register_hotkey=False)
    application.indexing.finished.connect(lambda summary: FINISHED.append((time.perf_counter(), summary)))
    application.indexing.progress.connect(lambda _d, _n, path, result: PROGRESS.append((time.perf_counter(), path,
                                                                                         result)))
    application.watch.dispatch.connect(lambda targets, rescans: DISPATCHED.append((time.perf_counter(), targets,
                                                                                   rescans)))
    application.start()
    rows = []
    try:
        if not pump(120, lambda: application.window.engine_ready):
            raise SystemExit("search engine did not start")
        application.start_indexing([str(root)])
        if not pump(120, lambda: "색인을 마쳤습니다" in application.folders.state.text()):
            raise SystemExit("initial index failed: " + application.folders.state.text())
        pump(1)
        probe = Probe(application)
        cases = [
            ("note_cold.txt", "kestrel lighthouse quartermaster ledger", text(
                "The kestrel lighthouse quartermaster ledger lists lamp oil deliveries."), True),
            ("note_warm1.txt", "otter ferry timetable for the winter solstice", text(
                "The otter ferry timetable for the winter solstice leaves at dawn."), False),
            ("note_warm2.txt", "copper kettle repair receipt from the harbour smithy", text(
                "A copper kettle repair receipt from the harbour smithy, paid in full."), False),
            ("note_warm3.txt", "migrating cranes over the rice terraces", text(
                "Field notes: migrating cranes over the rice terraces at sunrise."), False),
            ("watch_photo.jpg", "a red stop sign", copier(TEST_DATA / "pictures" / "stop_sign.jpg"), False),
            ("watch_notice.pdf", "엘리베이터 점검", copier(TEST_DATA / "documents" / "엘리베이터_점검_안내.pdf"), False),
            ("watch_manual.pdf", "facilities manual", copier(TEST_DATA / "documents" / "long" / "facilities_manual.pdf"),
             False),
            ("watch_clip.mp4", "a birthday cake with candles", copier(TEST_DATA / "media" / "clip_2026-08-30.mp4"), False),
            ("slow_copy.txt", "albatross migration census at Kestrel Point", slow_writer(6, 0.3), False),
        ]
        for name, query, write, cold in cases:
            rows.append(measure(application, probe, root, name, query, write, cold))
    finally:
        application.quit()
    out = {"measured": time.strftime("%Y-%m-%d %H:%M"), "quiet_seconds": watcher.QUIET,
           "notes": "ms는 파일 쓰기를 마친 시각 기준(음수 = 쓰는 도중). first/last_event=감시 스레드가 첫·마지막 이벤트를 "
                    "받은 시각(변경 감지 지연), dispatch=묶기·안정성 확인 뒤 색인 작업을 넘긴 시각, dispatches=그 파일이 든 "
                    "작업 수(1이면 쓰는 도중 읽지 않음), first_step=첫 파일/단계 처리 신호, indexed=그 파일 색인 완료 신호, "
                    "searchable=실제 텍스트 엔진 검색에서 내용 구간으로 상위 5위 안에 찾힘(긴 파일은 질의 위치에 따라 다름).",
           "rows": rows}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "stage6-watch.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
