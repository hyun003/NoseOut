"""5단계 남은 UI: 격자·종류 필터·폴더 범위·커밋 경계에서 일시정지. 합성 입력과 pytest 임시 색인만 사용한다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QObject, Qt, QThread, Signal  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from noseout import app, crawler, scope, services  # noqa: E402
from noseout.indexer import Indexer  # noqa: E402
from noseout.searcher import Searcher  # noqa: E402
from noseout.store import IndexStore  # noqa: E402
from test_long_files import long_text  # noqa: E402
from test_review_fixes import FakeEmbedder  # noqa: E402
from test_ui import FakeHit, qt, small_folder, wait_for  # noqa: E402,F401


def test_grid_reuses_selection_opening_and_filter(qt, test_data, monkeypatch):
    window = app.SearchWindow()
    window.engine_is_ready("준비됨")
    hits = [FakeHit(str(test_data / "pictures" / "stop_sign.jpg"), "image", "image", None),
            FakeHit("report.pdf", "pdf", "page", 7.0)]
    window.show_results(window.request, "그림", hits)
    window.set_expanded(True)
    assert window.views.currentWidget() is window.grid and window.grid.count() == 2
    icon_before = window.grid.item(0).icon().cacheKey()
    assert wait_for(lambda: not window._thumbnail_rows, 2)
    assert window.grid.item(0).icon().cacheKey() != icon_before
    opened = []
    monkeypatch.setattr(app.opener, "open_result", lambda *a: opened.append(a))
    QApplication.sendEvent(window.box, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down,
                                                Qt.KeyboardModifier.NoModifier))
    assert window.current_hit() is hits[1]
    window.open_selected()
    assert opened == [("report.pdf", 7, None)]
    window.view_mode.setCurrentIndex(1)
    assert window.views.currentWidget() is window.table and window.current_hit() is hits[1]
    old_request = window.request
    window.kind_filter.setCurrentIndex(1)
    assert window.kind_filter.currentData() == {"image", "screenshot"} and window.request > old_request
    window.show_results(old_request, "옛 검색", hits)
    assert not window.hits
    window.set_expanded(False)
    assert window.views.currentWidget() is window.table and window.kind_filter.currentData() is None
    window.close()


def test_settings_exclusions_and_folder_dialog(qt, tmp_path, test_data, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings = app.Settings([str(test_data)], exclusions=[str(test_data / "media")])
    settings.save(settings_path)
    assert app.Settings.load(settings_path) == settings
    dialog = app.FoldersDialog([str(test_data)])
    warnings = []
    monkeypatch.setattr(dialog, "warn_folder", warnings.append)
    assert dialog.add_exclusion(str(tmp_path)) is False and warnings
    assert dialog.add_exclusion(str(test_data / "media"))
    assert not dialog.add_exclusion(str(test_data / "media").upper())
    dialog.excluded_list.setCurrentRow(0)
    dialog.remove_exclusion()
    assert dialog.exclusions() == []


def test_scope_respects_case_and_path_boundaries(tmp_path):
    root = tmp_path / "chosen"
    assert scope.allows(root / "a.txt", [str(root).upper()], [])
    assert not scope.allows(tmp_path / "chosen2" / "a.txt", [root], [])
    assert not scope.allows(root / "nested" / "a.txt", [root], [root / "nested"])
    assert scope.allows(root / "nested2" / "a.txt", [root], [root / "nested"])
    assert not scope.allows(root / "a.txt", [], [])


def test_excluded_index_is_preserved_and_unexclude_reuses_vectors(tmp_path, test_data, monkeypatch):
    root = small_folder(tmp_path, test_data)
    excluded = root / "private"
    excluded.mkdir()
    shutil.copy2(test_data / "documents" / "tokyo_trip.md", excluded / "saved.md")
    db = tmp_path / "index.sqlite"
    with IndexStore(db) as store:
        Indexer(store, FakeEmbedder()).sync([root])
        before = list(store.db.execute("SELECT id, vec FROM segments WHERE file IN "
                                       "(SELECT id FROM files WHERE path = ?)", (str(excluded / "saved.md"),)))
        scan = crawler.os.scandir

        def guarded(path):
            assert Path(path) != excluded
            return scan(path)

        monkeypatch.setattr(crawler.os, "scandir", guarded)
        result = Indexer(store, FakeEmbedder(fail_texts=AssertionError("다시 임베딩하면 안 됨"))).sync(
            [root], exclusions=[excluded])
        assert result.removed == 0 and not result.results
        after = list(store.db.execute("SELECT id, vec FROM segments WHERE file IN "
                                      "(SELECT id FROM files WHERE path = ?)", (str(excluded / "saved.md"),)))
        assert before == after
        monkeypatch.setattr(crawler.os, "scandir", scan)
        result = Indexer(store, FakeEmbedder(fail_texts=AssertionError("다시 임베딩하면 안 됨"))).sync([root])
        assert not result.results and result.unchanged == 5


def test_search_scope_and_kind_filter_precede_candidate_limit(tmp_path):
    with IndexStore(tmp_path / "index.sqlite") as store:
        for n in range(150):
            store.upsert_file(str(tmp_path / "removed" / f"token-{n}.txt"), "doc", 0, 0, n, 0)
        visible = store.upsert_file(str(tmp_path / "chosen" / "token.png"), "image", 0, 0, 151, 0)
        store.commit()
        searcher = Searcher(store)
        hits = searcher.search("token", None, allowed_files={visible}, kinds={"image"}, limit=1)
        assert len(hits) == 1 and Path(hits[0].path).name == "token.png"
        assert searcher.search("token", None, allowed_files=set()) == []
        assert searcher.search("token", None, allowed_files={visible}, kinds={"doc"}) == []
        assert searcher.search("token", None, kinds={"image"})[0].score == hits[0].score


def test_search_service_remove_exclude_restore_without_changing_index(qt, tmp_path):
    root = tmp_path / "chosen"
    with IndexStore(tmp_path / "index.sqlite") as store:
        store.upsert_file(str(root / "token.txt"), "doc", 0, 0, 1, 0)
        store.upsert_file(str(root / "private" / "token.md"), "doc", 0, 0, 2, 0)
        store.commit()
        before = list(store.db.iterdump())
        service = services.SearchService(store.path, roots=[str(root)])
        service.store = store
        service.searcher = Searcher(store)
        service.embedder = FakeEmbedder()
        service._scope_files()
        outputs = []
        service.results.connect(lambda _r, _q, hits: outputs.append(hits))
        service.search(1, "token")
        assert len(outputs[-1]) == 2
        service.set_scope([str(root)], [str(root / "private")])
        service.search(2, "token")
        assert len(outputs[-1]) == 1
        service.set_scope([], [])
        service.search(3, "token")
        assert outputs[-1] == []
        service.set_scope([str(root)], [])
        service.search(4, "token", {"doc"})
        assert len(outputs[-1]) == 2 and list(store.db.iterdump()) == before


def test_app_scope_change_clears_old_results_and_passes_filter(qt, tmp_path, test_data):
    application = app.App(qt, tmp_path / "home", register_hotkey=False)
    try:
        sent = []
        application.search_request.connect(lambda *a: sent.append(a))
        window = application.window
        window.engine_is_ready("준비됨")
        window.box.setText("token")
        window.set_expanded(True)
        window.kind_filter.setCurrentIndex(1)
        assert sent[-1][2] == {"image", "screenshot"}
        request = window.request
        application._save_folders([str(test_data)])
        window.show_results(request, "token", [FakeHit("old.jpg", "image", "image", None)])
        assert not window.hits
    finally:
        application.quit()


class CountEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.closed = False

    def embed_texts(self, texts):
        self.calls += len(texts)
        return super().embed_texts(texts)

    def embed_image(self, *args, **kwargs):
        self.calls += 1
        return super().embed_image(*args, **kwargs)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("stop", [False, True], ids=["resume", "stop-while-paused"])
def test_pause_keeps_same_sync_and_engine_at_committed_boundary(qt, tmp_path, test_data, stop):
    folder = small_folder(tmp_path, test_data)
    embedder = CountEmbedder()
    service = services.IndexService(tmp_path / "index.sqlite", embedder_factory=lambda: embedder)
    worker = QThread()
    service.moveToThread(worker)
    finished = []
    service.finished.connect(finished.append)
    service.progress.connect(lambda done, *_: service.request_pause() if done == 1 else None,
                             Qt.ConnectionType.DirectConnection)
    class Runner(QObject):
        run = Signal(list)

    runner = Runner()
    runner.run.connect(service.run)
    service.prepare()
    worker.start()
    runner.run.emit([str(folder)])
    try:
        assert wait_for(lambda: service.paused, 3)
        calls = embedder.calls
        with IndexStore(service.db) as store:
            assert store.db.execute("SELECT COUNT(*) FROM files WHERE state='done'").fetchone()[0] == 1
        time.sleep(0.05)
        assert embedder.calls == calls and not embedder.closed and service.running
        service.request_stop() if stop else service.resume()
        assert wait_for(lambda: finished, 5)
        assert finished[-1]["done"] == (1 if stop else 4)
        assert finished[-1]["stopped"] == stop and embedder.closed
    finally:
        service.request_stop()
        worker.quit()
        assert worker.wait(5000)


def test_pause_long_file_does_not_repeat_committed_chunks(qt, tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    path = long_text(root / "book.txt", 45)
    embedder = CountEmbedder()
    with IndexStore(tmp_path / "index.sqlite") as store:
        indexer = Indexer(store, embedder)
        calls_at_boundary = []

        def checkpoint():
            calls_at_boundary.append(embedder.calls)

        indexer.sync([root], checkpoint=checkpoint)
        locations = [row[0] for row in store.db.execute("SELECT loc FROM segments WHERE kind='chunk'")]
        assert len(calls_at_boundary) > 1 and calls_at_boundary[1] == 40
        assert len(locations) == len(set(locations)) == embedder.calls


def test_changed_long_file_after_pause_restarts_only_that_file(tmp_path):
    root = tmp_path / "chosen"
    root.mkdir()
    path = long_text(root / "book.txt", 45)
    with IndexStore(tmp_path / "index.sqlite") as store:
        boundaries = []

        def checkpoint():
            boundaries.append(True)
            if len(boundaries) == 2:
                path.write_text("changed book with a single fresh section", encoding="utf-8")

        Indexer(store, FakeEmbedder()).sync([root], checkpoint=checkpoint)
        assert store.db.execute("SELECT COUNT(*) FROM segments WHERE kind='chunk'").fetchone()[0] == 1
        assert store.db.execute("SELECT state FROM files").fetchone()[0] == "done"


@pytest.mark.parametrize("quit_paused", [False, True], ids=["button-resume", "quit-while-paused"])
def test_app_pause_button_and_resume_or_quit(qt, tmp_path, test_data, monkeypatch, quit_paused):
    entered, release = threading.Event(), threading.Event()
    instances = []

    class WaitingEmbedder(CountEmbedder):
        def embed_texts(self, texts):
            entered.set()
            assert release.wait(5)
            return super().embed_texts(texts)

    def factory():
        instances.append(WaitingEmbedder())
        return instances[-1]

    monkeypatch.setattr(services.SearchService, "start", lambda self: self.ready.emit("준비됨"))
    application = app.App(qt, tmp_path / "home", index_factory=factory, register_hotkey=False)
    finished = []
    application.indexing.finished.connect(finished.append)
    application.start()
    try:
        application.start_indexing([str(small_folder(tmp_path, test_data))])
        assert wait_for(entered.is_set, 3)
        application.folders.pause_button.click()
        assert application.indexing._pause
        release.set()
        assert wait_for(lambda: application.folders.paused, 3)
        assert application.folders.pause_button.text() == "이어하기" and not instances[0].closed
        if quit_paused:
            application.quit()
            assert not application.index_thread.isRunning() and instances[0].closed
        else:
            application.folders.pause_button.click()
            assert wait_for(lambda: finished, 5)
            assert finished[-1]["done"] == 4 and not finished[-1]["stopped"]
            assert len(instances) == 1 and instances[0].closed
            with IndexStore(application.db) as store:
                assert store.db.execute("SELECT COUNT(*) FROM segments WHERE vec IS NOT NULL").fetchone()[0] \
                    == instances[0].calls
    finally:
        release.set()
        application.quit()


def test_scope_filter_preserves_semantic_scores_and_sets_scale(tmp_path, test_data):
    root = small_folder(tmp_path, test_data)
    with IndexStore(tmp_path / "index.sqlite") as store:
        Indexer(store, FakeEmbedder()).sync([root])
        searcher = Searcher(store)
        query = searcher.matrix[0].astype(np.float32)
        before_scale = searcher.row_sets_scale.copy()
        full = searcher.search("", query, z_gate=-10)
        selected = next(file for file, kind in searcher.file_kind.items() if kind == "image")
        filtered = searcher.search("", query, z_gate=-10, kinds={"image"}, allowed_files={selected})
        assert len(filtered) == 1
        previous = next(hit for hit in full if hit.path == filtered[0].path)
        assert filtered[0].score == previous.score and filtered[0].z == previous.z
        assert np.array_equal(before_scale, searcher.row_sets_scale)
