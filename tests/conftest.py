import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# 테스트(와 테스트가 띄우는 하위 프로세스)는 실제 %LOCALAPPDATA%\NoseOut·DigUpWin을 쓰거나 옮기지 않는다.
os.environ["NOSEOUT_HOME"] = str(ROOT / ".tmp" / "pytest-home")
os.environ.pop("DIGUPWIN_HOME", None)

try:
    # winrt(Windows OCR)를 PySide6보다 먼저 불러오면 PySide6를 불러올 때 프로세스가 죽는다(access violation, 2026-10-13
    # 확인). 앱은 app.py가 PySide6를 먼저 불러오므로 안전하다. 테스트도 같은 순서가 되게 여기서 먼저 불러 둔다.
    import PySide6.QtCore  # noqa: E402,F401
except ImportError:
    pass

from noseout import engine  # noqa: E402

RESULTS = ROOT / "results"
REFERENCE = Path(__file__).parent / "reference"
FIXTURES = Path(__file__).parent / "fixtures"


def _engine_ready() -> str | None:
    for path in (engine.SERVER_EXE, engine.TEXT_MODEL, engine.MEDIA_MODEL):
        if not path.exists():
            return f"missing {path.relative_to(ROOT)}"
    return None


requires_engine = pytest.mark.skipif(_engine_ready() is not None, reason=_engine_ready() or "")


@pytest.fixture(autouse=True)
def no_real_startup_entries(monkeypatch):
    """테스트는 실제 Windows 시작 항목(HKCU Run)을 읽거나 바꾸지 않는다: 기본 저장소를 메모리 대체 구현으로 바꾼다."""
    from noseout import prefs

    monkeypatch.setattr(prefs, "RegistryRun", prefs.MemoryRun)


@pytest.fixture(scope="session", autouse=True)
def delete_leftover_widgets():
    """세션이 끝날 때 남은 최상위 Qt 위젯을 QApplication이 살아 있을 때 지운다. 부모 없는 대화상자가 Python 종료
    단계까지 남으면 QApplication보다 늦게 정리되며 모든 테스트가 통과한 뒤에도 프로세스가 간헐적으로 죽었다
    (0xC0000005/0xC0000409, Git Bash에서는 127·139로 보임; 2026-10-14 재현, docs/progress.md)."""
    yield
    try:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    application = QApplication.instance()
    if application is None:
        return
    for widget in application.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(scope="session")
def embedder():
    RESULTS.mkdir(exist_ok=True)
    with engine.Embedder(engine.ServerConfig(log=RESULTS / "llama-server.log")) as e:
        yield e


TEST_DATA = ROOT / "test_data"


@pytest.fixture(scope="session")
def test_data() -> Path:
    """2단계 합성 테스트 폴더. 없으면 scripts/make_test_data.py로 만든다. 테스트는 이 폴더만 읽는다."""
    if not (TEST_DATA / "documents" / "lease_agreement.pdf").exists():
        import subprocess

        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_test_data.py")], check=True)
    return TEST_DATA


@pytest.fixture(scope="session")
def record():
    """테스트가 측정값을 results/metrics.json에 남긴다."""
    RESULTS.mkdir(exist_ok=True)
    metrics: dict = {}
    yield metrics
    (RESULTS / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
