"""6단계 개인용 최종 점검: 실제 엔진으로 앱을 띄워(창은 화면에 띄우지 않음) 흐름을 차례로 확인한다.

입력은 test_data의 합성 파일 사본, 설정·색인은 .tmp/final-check에만. 기존 .index·앱 설정(%LOCALAPPDATA%\\NoseOut, 옛 DigUpWin)·
사용자 파일은 쓰지 않는다. 결과: results/stage6-final.json (통과/실패와 관찰값, 개인 경로 없음)

    .venv\\Scripts\\python scripts\\final_check.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from noseout import app, engine, services  # noqa: E402
from noseout.store import IndexStore  # noqa: E402

WORK = ROOT / ".tmp" / "final-check"
CHECKS: list[dict] = []


def check(name: str, ok: bool, observed: str = "") -> None:
    CHECKS.append({"check": name, "ok": bool(ok), "observed": observed})
    print(("PASS " if ok else "FAIL ") + name + (f" — {observed}" if observed else ""), flush=True)


def pump(seconds: float, until=None) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


def make_app(qt, base: Path, **kwargs) -> app.App:
    application = app.App(qt, base, register_hotkey=False, **kwargs)
    application.window.summon = lambda: None            # 사용자 화면에 창을 띄우지 않는다
    application.tray.showMessage = lambda *a, **k: None
    application.start()
    return application


def search(application: app.App, text: str, seconds: float = 30) -> list[str]:
    application.window.box.setText(text)
    application.window._send()
    request = application.window.request
    pump(seconds, lambda: application.window.shown_request >= request and application.window.status.text() != "찾는 중…")
    return [Path(h.path).name for h in application.window.hits]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    data, code_root, base = WORK / "data", WORK / "code" / "shop", WORK / "home"
    for sub in ("documents", "pictures", "media"):
        shutil.copytree(ROOT / "test_data" / sub, data / sub, ignore=shutil.ignore_patterns("long"))
    (code_root / "src").mkdir(parents=True)
    (code_root / "src" / "net.py").write_text(
        "def retry_with_backoff(operation, attempts=5):\n    for attempt in range(attempts):\n"
        "        try:\n            return operation()\n        except ConnectionError:\n            pass\n",
        encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=code_root, check=True)
    qt = QApplication.instance() or QApplication([])

    # 1. 첫 실행: 색인 없음 안내
    a = make_app(qt, base)
    pump(120, lambda: a.window.engine_ready)
    check("첫 실행: 검색 엔진 준비와 '색인 없음' 안내", a.window.engine_ready and "아직 색인이 없습니다" in a.window.status.text(),
          a.window.status.text())
    warnings = []
    a.folders.warn_folder = warnings.append
    check("금지 폴더(사용자 폴더 전체) 거부와 한국어 안내", not a.folders.add_folder(str(Path.home())) and warnings
          and "사용자 폴더 전체" in warnings[-1], warnings[-1] if warnings else "")
    check("검색 폴더·코드 폴더 추가", a.folders.add_folder(str(data)) and a.folders.add_code_folder(str(code_root.parent)))

    # 2. 색인: 시작 직후 일시정지 → 이어하기 → 완료
    a.indexing.request_pause()                                       # 첫 파일 뒤 체크포인트에서 멈춘다
    a.folders.pause_button.setText("일시정지")
    a.start_indexing(a.folders.folders())
    a.indexing.request_pause()
    paused = pump(120, lambda: a.indexing.paused)
    state_paused = a.folders.state.text()
    check("색인 일시정지 (현재 파일 뒤에서 대기, 버튼 '이어하기')", paused and a.folders.pause_button.text() == "이어하기",
          state_paused)
    a.folders.pause_button.click()                                   # 이어하기
    finished = pump(300, lambda: "색인을 마쳤습니다" in a.folders.state.text())
    check("이어하기 후 색인 완료 (일반 + 코드)", finished and "코드 파일" in a.folders.state.text(), a.folders.state.text())

    # 3. 검색·코드 검색
    pump(30, lambda: "검색할 수 있는 파일" in a.window.status.text())
    hits = search(a, "a red stop sign")
    check("의미 검색: 'a red stop sign' → stop_sign.jpg 1위", bool(hits) and hits[0] == "stop_sign.jpg", str(hits[:3]))
    hits = search(a, "치과 예약 음성 메모")
    check("한국어 음성 검색(같은 언어): 음성메모 상위", "음성메모_0917.m4a" in hits[:3], str(hits[:3]))
    hits = search(a, "code: retry_with_backoff")
    check("코드 검색: 정의 줄", bool(hits) and hits[0] == "net.py" and a.window.items[0].text(2) == "1번째 줄",
          f"{hits[:1]} {a.window.items[0].text(2) if hits else ''}")

    # 4. 제외 → 숨김, 제외 해제 → 복원 (재색인 없이)
    check("하위 폴더 제외", a.folders.add_exclusion(str(data / "pictures")))
    hits = search(a, "a red stop sign")
    check("제외한 폴더의 결과 즉시 숨김", "stop_sign.jpg" not in hits, str(hits[:3]))
    a.folders.excluded_list.setCurrentRow(0)
    a.folders.remove_exclusion()
    pump(5, lambda: not a.index_active)
    pump(30, lambda: not a.index_active and not a.watch.running_job)
    hits = search(a, "a red stop sign")
    check("제외 해제 후 결과 복원", bool(hits) and hits[0] == "stop_sign.jpg", str(hits[:3]))

    # 5. 자동 감시: 새 파일 → 검색 가능
    (data / "documents" / "lighthouse_notes.md").write_text(
        "Notes about repainting the old lighthouse keeper's cottage next spring.", encoding="utf-8")
    pump(30, lambda: "자동 반영 완료" in a.folders.state.text())
    pump(5, lambda: not a.index_active)
    hits = search(a, "repainting the lighthouse keeper's cottage")
    check("자동 감시: 새 파일이 자동으로 검색됨", bool(hits) and hits[0] == "lighthouse_notes.md",
          f"{hits[:2]} / {a.watch.state}")

    # 6. 종료: 스레드·엔진 정리
    owned = [p for p in (getattr(a.search.embedder, "process", None),) if p]
    a.quit()
    check("종료: 검색·색인 스레드 종료, 감시 스레드 정리",
          not a.search_thread.isRunning() and not a.index_thread.isRunning() and not a.watch.watchers)
    check("종료: 앱이 띄운 검색 엔진 프로세스 종료", all(p.poll() is not None for p in owned), f"{len(owned)} owned")

    # 7. 재실행: 꺼져 있던 동안의 변경 확인
    (data / "documents" / "offline_note.md").write_text("A note about winter tyre storage at the garage.",
                                                        encoding="utf-8")
    a = make_app(qt, base)
    pump(120, lambda: a.window.engine_ready)
    pump(60, lambda: "자동 반영 완료" in a.folders.state.text())
    pump(10, lambda: not a.index_active)
    hits = search(a, "winter tyre storage")
    check("재실행: 설정 유지(폴더·코드 폴더·감시)", a.settings.folders == [str(data)] and a.settings.code_folders
          and a.settings.watch)
    check("재실행: 꺼져 있던 동안 추가한 파일을 확인해 검색됨", bool(hits) and hits[0] == "offline_note.md", str(hits[:2]))
    a.quit()

    # 8. 오류 안내와 로그: 엔진 파일이 없을 때
    broken = WORK / "home-broken"
    app.Settings([str(data)]).save(broken / "settings.json")
    app.setup_logging(broken)
    missing = engine.ServerConfig(exe=WORK / "missing" / "llama-server.exe", media_model=None)
    b = make_app(qt, broken, search_config=missing)
    pump(30, lambda: "찾지 못했습니다" in b.window.status.text())
    check("엔진 파일이 없을 때 한국어 안내(내부 오류 숨김)", "검색 엔진 파일" in b.window.status.text()
          and "Traceback" not in b.window.status.text(), b.window.status.text())
    b.quit()
    log_text = (broken / "app.log").read_text(encoding="utf-8") if (broken / "app.log").exists() else ""
    check("자세한 오류는 app.log에 기록", "search engine start failed" in log_text and "Traceback" in log_text)

    # 9. 색인 버전 불일치: 다른 엔진으로 만든 색인 흉내 (사본에서만)
    other = WORK / "home-other-engine"
    shutil.copytree(base, other, ignore=shutil.ignore_patterns("app.lock", "*.log*"))
    with IndexStore(other / "index.sqlite") as store:
        store.set_meta("engine", "other-model q4 llama.cpp-b1")
    before = digest(other / "index.sqlite")
    c = make_app(qt, other)
    pump(120, lambda: c.window.engine_ready)
    check("버전 불일치: 검색 창 안내, 그 색인은 검색에 쓰지 않음",
          "다른 모델·엔진" in c.window.status.text() and c.search.searcher is None, c.window.status.text())
    pump(30, lambda: c.watch.held)
    check("버전 불일치: 자동 갱신 실패 뒤 자동 반영 멈춤(반복 실패 없음)", c.watch.held, c.watch.state)
    c.start_indexing(c.folders.folders())
    pump(60, lambda: not c.index_active)
    check("버전 불일치: 색인 시작 시 한국어 안내", "다른 모델·엔진" in c.folders.state.text(), c.folders.state.text())
    c.quit()
    check("버전 불일치: 기존 색인 파일은 바뀌지 않음", digest(other / "index.sqlite") == before)

    # 10. 실제 진입점: python -m noseout app 시작·종료 (화면 밖 렌더링, 몇 초 뒤 종료)
    helper = WORK / "entry_check.py"
    helper.write_text(
        "import os, sys\nos.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from PySide6.QtCore import QTimer\nfrom noseout import app\n"
        "original = app.App.start\n"
        "def start(self):\n    original(self)\n    QTimer.singleShot(8000, self.quit)\n"
        "app.App.start = start\nfrom noseout.__main__ import main\nsys.exit(main(['app']))\n", encoding="utf-8")
    env = {**os.environ, "NOSEOUT_HOME": str(WORK / "home-entry")}
    started = time.time()
    done = subprocess.run([sys.executable, str(helper)], env=env, capture_output=True, timeout=120)
    check("실제 진입점 'python -m noseout app' 시작·종료 코드 0", done.returncode == 0,
          f"exit {done.returncode}, {time.time() - started:.1f}s")

    # 한 프로세스에서 앱을 여러 번 만들었다: 남은 창을 QApplication이 살아 있을 때 지운다(종료 단계 충돌 방지, conftest와 같음)
    from PySide6.QtCore import QEvent
    for widget in qt.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    out = {"measured": time.strftime("%Y-%m-%d %H:%M"), "passed": sum(c["ok"] for c in CHECKS), "total": len(CHECKS),
           "checks": [{**c, "observed": c["observed"].replace(str(ROOT), ".")} for c in CHECKS]}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "stage6-final.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out['passed']}/{out['total']} passed")
    return 0 if out["passed"] == out["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
