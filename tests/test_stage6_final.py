"""6단계 최종 점검에서 고친 것: 검색 전 색인 버전 확인, 자동 갱신 실패 시 멈춤, 로그 크기 제한.
합성 파일과 pytest 임시 폴더(.tmp)의 색인만 사용한다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from noseout import app, services  # noqa: E402
from noseout.indexer import (CODE_COMPONENTS, ENGINE, LEGACY_STAGE2_3, LEGACY_STAGE4, IncompatibleIndex,  # noqa: E402
                               Indexer, check_versions, version_problem)
from noseout.store import IndexStore  # noqa: E402
from test_review_fixes import FakeEmbedder  # noqa: E402
from test_ui import qt, small_folder, wait_for  # noqa: E402,F401


def indexed(tmp_path, test_data) -> tuple[Path, Path]:
    root = small_folder(tmp_path, test_data)
    db = tmp_path / "home" / "index.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with IndexStore(db) as store:
        Indexer(store, FakeEmbedder()).sync([root])
    return root, db


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("case", ["current", "stage4", "stage2-3", "fresh", "other engine", "unknown legacy",
                                  "newer format", "vectors without record"])
def test_version_problem_matches_check_versions_without_writing(tmp_path, test_data, case):
    """검색용 검사는 색인할 때의 판정(check_versions)과 같아야 하고, 색인을 한 글자도 바꾸지 않아야 한다."""
    root, db = indexed(tmp_path, test_data)
    with IndexStore(db) as store:
        meta = {"current": {}, "fresh": None,
                "stage4": {"index_format": None, "engine": None, "components": None, "vector_source": LEGACY_STAGE4},
                "stage2-3": {"index_format": None, "engine": None, "components": None,
                             "vector_source": LEGACY_STAGE2_3},
                "other engine": {"engine": "other-model q4 llama.cpp-b1"},
                "unknown legacy": {"index_format": None, "engine": None, "components": None,
                                   "vector_source": "something old"},
                "newer format": {"index_format": "3"},
                "vectors without record": {"index_format": None, "engine": None, "components": None,
                                           "vector_source": None}}[case]
        if meta is None:
            store.db.execute("DELETE FROM segments")
            store.db.execute("DELETE FROM meta")
        else:
            for key, value in meta.items():
                if value is None:
                    store.db.execute("DELETE FROM meta WHERE key = ?", (key,))
                else:
                    store.set_meta(key, value)
        store.commit()
    before = digest(db)
    with IndexStore(db) as store:
        problem = version_problem(store)
    assert digest(db) == before                                    # 읽기만 한다
    copy = tmp_path / "copy.sqlite"
    shutil.copyfile(db, copy)
    with IndexStore(copy) as store:
        try:
            check_versions(store)
            accepted = True
        except IncompatibleIndex:
            accepted = False
    assert (problem is None) == accepted, (case, problem)
    assert accepted == (case in ("current", "stage4", "stage2-3", "fresh"))


def test_search_service_refuses_other_engine_and_explains(qt, tmp_path, test_data):
    root, db = indexed(tmp_path, test_data)
    with IndexStore(db) as store:
        store.set_meta("engine", "other-model q4 llama.cpp-b1")
    before = digest(db)
    service = services.SearchService(db, roots=[str(root)], exclusions=[])
    ready, results, failed = [], [], []
    service.ready.connect(ready.append)
    service.results.connect(lambda r, t, hits: results.append(hits))
    service.failed.connect(lambda r, message: failed.append(message))
    service.embedder = FakeEmbedder()
    service.embedder.close = lambda: None
    service._open()
    assert service.searcher is None and "다른 모델·엔진" in ready[-1] and "index.sqlite" in ready[-1]
    service.search(1, "Tokyo trip")
    assert not results and "다른 모델·엔진" in failed[-1]          # 맞지 않는 벡터로 결과를 내지 않는다
    service.shutdown()
    assert digest(db) == before


def test_incompatible_code_index_is_named(qt, tmp_path, test_data):
    root, db = indexed(tmp_path, test_data)
    code_db = db.with_name("code.sqlite")
    with IndexStore(code_db) as store:
        check_versions(store, CODE_COMPONENTS)
        store.set_meta("engine", "other-model")
    service = services.SearchService(db, roots=[str(root)], exclusions=[], code_db=code_db, code_roots=[])
    ready, failed = [], []
    service.ready.connect(ready.append)
    service.failed.connect(lambda r, message: failed.append(message))
    service.embedder = FakeEmbedder()
    service.embedder.close = lambda: None
    service._open()
    assert service.searcher is not None and service.code_searcher is None
    assert "코드 색인" in ready[-1] and "code.sqlite" in ready[-1]
    service.search(1, "code: retry")
    assert "code.sqlite" in failed[-1]
    service.shutdown()
    message = services.friendly(IncompatibleIndex("engine 'x'; the index at code.sqlite was left unchanged"))
    assert "코드 색인" in message and "code.sqlite" in message
    assert ENGINE not in message                                  # 내부 문자열은 보이지 않는다


def test_failed_automatic_update_holds_watching(qt, tmp_path, test_data, monkeypatch):
    """자동 갱신이 실패하면(예: 엔진이 다른 색인) 변경마다 같은 실패를 되풀이하지 않게 자동 반영을 멈춘다."""
    monkeypatch.setattr(services.SearchService, "start", lambda self: self.ready.emit("준비됨"))
    root, db = indexed(tmp_path, test_data)
    with IndexStore(db) as store:
        store.set_meta("engine", "other-model q4 llama.cpp-b1")
    base = db.parent
    app.Settings([str(root)]).save(base / "settings.json")
    application = app.App(qt, base, index_factory=FakeEmbedder, register_hotkey=False)
    application.start()
    try:
        # 앱 시작의 놓친 변경 확인이 자동 갱신으로 돌다가 버전 불일치로 실패한다
        assert wait_for(lambda: application.watch.held, 10), application.watch.state
        assert "다른 모델·엔진" in application.folders.state.text()
        assert "자동 반영 멈춤" in application.watch.state
        (root / "late.md").write_text("Written after the failure.", encoding="utf-8")
        wait_for(lambda: False, 1.0)
        assert not application.index_active                       # 다시 시도하지 않는다
    finally:
        application.quit()


def test_log_file_is_size_limited(tmp_path, monkeypatch):
    logger = logging.getLogger("noseout")
    before = list(logger.handlers)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    try:
        app.setup_logging(tmp_path)
        added = [h for h in logger.handlers if h not in before]
        assert len(added) == 1 and isinstance(added[0], RotatingFileHandler)
        assert added[0].maxBytes == 1_000_000 and added[0].backupCount == 2
        assert Path(added[0].baseFilename) == tmp_path / "app.log"
    finally:
        for handler in [h for h in logger.handlers if h not in before]:
            logger.removeHandler(handler)
            handler.close()


def test_missing_engine_folder_gets_a_specific_message(tmp_path):
    """엔진 폴더째 없으면 Popen이 파일 이름 없는 WinError 2를 낸다: 시작 전에 어느 파일이 없는지 알려야 안내가 정확하다."""
    from noseout import engine

    config = engine.ServerConfig(exe=tmp_path / "missing" / "llama-server.exe", media_model=None)
    with pytest.raises(FileNotFoundError) as caught:
        engine.Embedder(config)
    assert caught.value.filename.endswith("llama-server.exe")
    assert "검색 엔진 파일" in services.friendly(caught.value)
