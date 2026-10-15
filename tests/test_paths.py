"""설정·색인 폴더 위치와 옛 DigUpWin 폴더 이전 (paths.py). 실제 %LOCALAPPDATA% 대신 pytest 임시 폴더(.tmp)를
LOCALAPPDATA로 넘겨 시험한다. 실제 설정 폴더는 읽지도 옮기지도 않는다."""

import hashlib
import shutil
import sqlite3

import pytest

from noseout import paths


@pytest.fixture(autouse=True)
def fresh_notes(monkeypatch):
    monkeypatch.setattr(paths, "NOTES", [])


def digest_tree(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def old_home(local):
    old = local / "DigUpWin"
    (old / "runtime").mkdir(parents=True)
    (old / "settings.json").write_text('{"folders": ["D:\\\\문서"]}', encoding="utf-8")
    (old / "index.sqlite").write_bytes(b"SQLite format 3\0" + bytes(range(256)) * 40)
    (old / "index.sqlite-wal").write_bytes(b"wal" * 1000)
    (old / "index.sqlite-shm").write_bytes(b"shm" * 1000)
    (old / "names.sqlite").write_bytes(b"names")
    (old / "app.log").write_text("old log\n", encoding="utf-8")
    (old / "runtime" / "msvcp140-abc.dll").write_bytes(b"dll")
    return old


def test_new_folder_only(tmp_path):
    new = tmp_path / "NoseOut"
    new.mkdir()
    assert paths.resolve({"LOCALAPPDATA": str(tmp_path)}) == new
    assert not (tmp_path / "DigUpWin").exists() and paths.NOTES == []


def test_fresh_install_uses_the_new_name(tmp_path):
    assert paths.resolve({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "NoseOut"
    assert list(tmp_path.iterdir()) == []                        # 만들지는 않는다(앱이 처음 쓸 때 만든다)


def test_old_folder_only_is_copied_verified_and_kept(tmp_path):
    old = old_home(tmp_path)
    (old / "app.lock").write_text("123\nhost\nNoseOut\n", encoding="utf-8")   # 끝난 실행이 남긴 잠금 파일
    before = digest_tree(old)
    home = paths.resolve({"LOCALAPPDATA": str(tmp_path)})
    assert home == tmp_path / "NoseOut"
    after = digest_tree(home)
    marker = after.pop(paths.MARKER)
    assert marker and after == {k: v for k, v in before.items() if k != "app.lock"}   # WAL·SHM 포함, 잠금 파일 제외
    assert digest_tree(old) == before                           # 옛 폴더는 그대로
    assert '"verified": "sha256"' in (home / paths.MARKER).read_text(encoding="utf-8")
    assert any("copied" in note for note in paths.NOTES)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["DigUpWin", "NoseOut"]      # 임시 복사본이 남지 않음


def test_both_folders_prefer_new_and_keep_old(tmp_path):
    old = old_home(tmp_path)
    new = tmp_path / "NoseOut"
    new.mkdir()
    (new / "settings.json").write_text("{}", encoding="utf-8")
    before = digest_tree(old)
    assert paths.resolve({"LOCALAPPDATA": str(tmp_path)}) == new
    assert digest_tree(old) == before
    assert (new / "settings.json").read_text(encoding="utf-8") == "{}" and not (new / "index.sqlite").exists()


def test_a_failed_copy_keeps_using_the_old_folder(tmp_path):
    old = old_home(tmp_path)
    before = digest_tree(old)
    copied = []

    def broken(source, target):
        if source.name == "index.sqlite-wal":
            raise OSError(28, "디스크 공간이 부족합니다")
        copied.append(source.name)
        shutil.copy2(source, target)

    assert paths.migrate(old, tmp_path / "NoseOut", copy=broken) is False
    assert copied                                               # 일부는 복사하다가 실패했다
    assert sorted(p.name for p in tmp_path.iterdir()) == ["DigUpWin"]     # 새 폴더·임시 복사본 없음
    assert digest_tree(old) == before
    assert any("failed" in note for note in paths.NOTES)


def test_a_copy_that_differs_is_not_accepted(tmp_path):
    old = old_home(tmp_path)

    def corrupt(source, target):
        shutil.copy2(source, target)
        if source.name == "names.sqlite":
            target.write_bytes(b"other")

    assert paths.migrate(old, tmp_path / "NoseOut", copy=corrupt) is False
    assert not (tmp_path / "NoseOut").exists()
    environ = {"LOCALAPPDATA": str(tmp_path)}
    assert paths.resolve(environ) == tmp_path / "NoseOut"       # 다음 실행은 정상 복사로 다시 시도한다


def test_does_not_move_while_the_old_app_is_running(tmp_path):
    from PySide6.QtCore import QLockFile

    old = old_home(tmp_path)
    lock = QLockFile(str(old / "app.lock"))                     # app.main과 같은 잠금
    assert lock.tryLock(100)
    try:
        assert paths.in_use(old)
        assert paths.resolve({"LOCALAPPDATA": str(tmp_path)}) == old
        assert not (tmp_path / "NoseOut").exists()
    finally:
        lock.unlock()
    assert not paths.in_use(old)


def test_sqlite_wal_contents_survive_the_copy(tmp_path):
    old = tmp_path / "DigUpWin"
    old.mkdir()
    writer = sqlite3.connect(old / "index.sqlite")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(v)")
    writer.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(500)])
    writer.commit()
    try:
        assert (old / "index.sqlite-wal").stat().st_size > 0      # 아직 본 파일에 반영되지 않은 내용이 WAL에만 있다
        home = paths.resolve({"LOCALAPPDATA": str(tmp_path)})
    finally:
        writer.close()
    reader = sqlite3.connect(home / "index.sqlite")
    try:
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (500,)
    finally:
        reader.close()


def test_environment_variables(tmp_path):
    old_home(tmp_path)
    assert paths.resolve({"NOSEOUT_HOME": str(tmp_path / "mine"), "DIGUPWIN_HOME": "x",
                          "LOCALAPPDATA": str(tmp_path)}) == tmp_path / "mine"
    assert paths.NOTES == []
    assert paths.resolve({"DIGUPWIN_HOME": str(tmp_path / "legacy"), "LOCALAPPDATA": str(tmp_path)}) \
        == tmp_path / "legacy"
    assert any("DIGUPWIN_HOME" in note and "NOSEOUT_HOME" in note for note in paths.NOTES)
    assert not (tmp_path / "NoseOut").exists()                  # 환경 변수로 정하면 옮기지 않는다


def test_scripts_can_look_without_moving(tmp_path):
    old = old_home(tmp_path)
    assert paths.resolve({"LOCALAPPDATA": str(tmp_path)}, do_migrate=False) == old
    assert not (tmp_path / "NoseOut").exists()
