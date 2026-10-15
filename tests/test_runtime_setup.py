"""공개용 EXE의 런타임 별칭 자동 준비 (runtime_setup.py). 실제 System32와 앱 폴더 대신 pytest 임시 폴더(.tmp)의
가짜 DLL과 버전만 쓴다. 시스템 DLL은 file_version 확인에서 읽기만 한다."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from noseout import runtime_setup

ROOT = Path(__file__).resolve().parents[1]
ALIAS = "_internal/numpy.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
NEW = (14, 50, 35719, 0)


def fake_system(tmp_path) -> Path:
    system = tmp_path / "System32"
    system.mkdir()
    for name in runtime_setup.REQUIRED:
        (system / name).write_bytes(f"official {name}".encode())
    return system


def versions(table=None):
    table = table or {}
    return lambda path: table.get(path.name, NEW)


def package(tmp_path, aliases=None) -> Path:
    app = tmp_path / "NoseOut"
    (app / "_internal" / "numpy.libs").mkdir(parents=True)
    aliases = [{"system_name": "msvcp140.dll", "relative_path": ALIAS}] if aliases is None else aliases
    (app / runtime_setup.MANIFEST).write_text(json.dumps({"local_aliases": aliases}), encoding="utf-8")
    return app


class Recorder:
    def __init__(self):
        self.added = []

    def __call__(self, folder):
        self.added.append(folder)
        return object()


def test_prepares_the_alias_in_the_app_folder_from_the_installed_runtime(tmp_path):
    system, app, added = fake_system(tmp_path), package(tmp_path), Recorder()
    notes = runtime_setup.prepare(app, system, tmp_path / "home" / "runtime", versions(), added)
    assert (app / ALIAS).read_bytes() == (system / "msvcp140.dll").read_bytes()
    assert added.added == [] and not (tmp_path / "home").exists()      # 앱 폴더에 썼으니 대체 경로는 쓰지 않는다
    assert any("app folder" in note for note in notes)
    assert not list((app / "_internal" / "numpy.libs").glob("*.tmp"))
    # 다시 실행해도 같은 내용이면 그대로
    assert runtime_setup.prepare(app, system, tmp_path / "home" / "runtime", versions(), added) == []


@pytest.mark.parametrize("alias", [
    {"system_name": "vcruntime140.dll", "relative_path": ALIAS},
    {"system_name": "msvcp140.dll", "relative_path": "_internal/PySide6/msvcp140-abc.dll"},
    {"system_name": "msvcp140.dll", "relative_path": "_internal/numpy.libs/vcruntime140-abc.dll"},
    {"system_name": "msvcp140.dll", "relative_path": "_internal/numpy.libs/msvcp140-../../evil.dll"},
    {"system_name": "msvcp140.dll", "relative_path": "_internal/numpy.libs/msvcp140-abc.exe"},
    {"system_name": "..\\msvcp140.dll", "relative_path": ALIAS},
    "msvcp140.dll",
])
def test_rejects_aliases_outside_the_allowed_pattern(tmp_path, alias):
    system, app = fake_system(tmp_path), package(tmp_path, [alias])
    with pytest.raises(runtime_setup.SetupRejected):
        runtime_setup.prepare(app, system, tmp_path / "runtime", versions(), Recorder())
    assert not list(app.rglob("*.dll"))


def test_rejects_a_target_that_resolves_outside_the_app_folder(tmp_path):
    system, app = fake_system(tmp_path), package(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    libs = app / "_internal" / "numpy.libs"
    libs.rmdir()
    # 디렉터리 연결(관리자 권한 불필요)로 numpy.libs가 앱 폴더 밖을 가리키게 한다
    subprocess.run(["cmd", "/c", "mklink", "/J", str(libs), str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(runtime_setup.SetupRejected, match="앱 내부"):
            runtime_setup.prepare(app, system, tmp_path / "runtime", versions(), Recorder())
        assert not list(outside.iterdir())
    finally:
        os.rmdir(libs)                       # 연결만 지운다(대상 폴더는 그대로)


def test_rejects_a_missing_numpy_folder(tmp_path):
    system, app = fake_system(tmp_path), package(tmp_path)
    (app / "_internal" / "numpy.libs").rmdir()
    with pytest.raises(runtime_setup.SetupRejected):
        runtime_setup.prepare(app, system, tmp_path / "runtime", versions(), Recorder())


def test_does_not_overwrite_an_existing_alias_with_different_content(tmp_path):
    system, app = fake_system(tmp_path), package(tmp_path)
    (app / ALIAS).write_bytes(b"older copy")
    notes = runtime_setup.prepare(app, system, tmp_path / "runtime", versions(), Recorder())
    assert (app / ALIAS).read_bytes() == b"older copy"
    assert any("kept as is" in note for note in notes)


def test_uses_the_user_folder_when_the_app_folder_is_not_writable(tmp_path, monkeypatch):
    system, app, added = fake_system(tmp_path), package(tmp_path), Recorder()
    original = runtime_setup._copy_new

    def read_only_app(source, target):
        if target.is_relative_to(app):
            raise PermissionError(13, "Access is denied", str(target))
        return original(source, target)

    monkeypatch.setattr(runtime_setup, "_copy_new", read_only_app)
    fallback = tmp_path / "home" / "runtime"
    notes = runtime_setup.prepare(app, system, fallback, versions(), added)
    name = Path(ALIAS).name
    assert not (app / ALIAS).exists()
    assert (fallback / name).read_bytes() == (system / "msvcp140.dll").read_bytes()
    assert added.added == [str(fallback)]
    assert any("not writable" in note for note in notes)
    # 대체 경로에도 다른 내용이 있으면 덮어쓰지 않고 그대로 쓴다
    (fallback / name).write_bytes(b"older copy")
    runtime_setup.prepare(app, system, fallback, versions(), added)
    assert (fallback / name).read_bytes() == b"older copy" and added.added == [str(fallback)] * 2


def test_a_relative_user_folder_is_added_as_an_absolute_path(tmp_path, monkeypatch):
    system, app, added = fake_system(tmp_path), package(tmp_path), Recorder()
    original = runtime_setup._copy_new

    def read_only_app(source, target):
        if target.is_relative_to(app):
            raise PermissionError(13, "Access is denied", str(target))
        return original(source, target)

    monkeypatch.setattr(runtime_setup, "_copy_new", read_only_app)
    monkeypatch.chdir(tmp_path)
    runtime_setup.prepare(app, system, Path("home") / "runtime", versions(), added)
    assert added.added == [str(tmp_path / "home" / "runtime")]           # 실제 EXE에서 상대 경로는 WinError 87


def test_fails_when_both_the_app_and_user_folders_are_not_writable(tmp_path, monkeypatch):
    system, app = fake_system(tmp_path), package(tmp_path)

    def denied(source, target):
        raise PermissionError(13, "Access is denied", str(target))

    monkeypatch.setattr(runtime_setup, "_copy_new", denied)
    with pytest.raises(runtime_setup.SetupRejected):
        runtime_setup.prepare(app, system, tmp_path / "runtime", versions(), Recorder())


@pytest.mark.parametrize("table", [{"msvcp140.dll": (14, 42, 34433, 0)}, {"vcruntime140_1.dll": None}])
def test_old_or_missing_runtime_is_reported(tmp_path, table):
    system, app = fake_system(tmp_path), package(tmp_path)
    with pytest.raises(runtime_setup.RuntimeMissing):
        runtime_setup.prepare(app, system, tmp_path / "runtime", versions(table), Recorder())
    (system / "msvcp140_2.dll").unlink()
    with pytest.raises(runtime_setup.RuntimeMissing, match="msvcp140_2.dll"):
        runtime_setup.prepare(app, system, tmp_path / "runtime", versions(), Recorder())
    assert not (app / ALIAS).exists()


def test_frozen_start_shows_the_official_link_and_logs_the_reason(tmp_path, monkeypatch):
    system, app = fake_system(tmp_path), package(tmp_path)
    (system / "msvcp140.dll").unlink()
    shown = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "NoseOut.exe"))
    monkeypatch.setattr(runtime_setup, "system_directory", lambda: system)
    home = tmp_path / "home"
    with pytest.raises(SystemExit) as stopped:
        runtime_setup.ensure_for_frozen_app(home, notify=shown.append)
    assert stopped.value.code == 1
    assert runtime_setup.REDIST_URL in shown[0]
    assert "msvcp140.dll" in (home / "startup-error.log").read_text(encoding="utf-8")


def test_does_nothing_outside_the_public_exe(tmp_path, monkeypatch):
    shown = []
    runtime_setup.ensure_for_frozen_app(tmp_path / "home", notify=shown.append)   # 소스 실행
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "personal" / "NoseOut.exe"))
    runtime_setup.ensure_for_frozen_app(tmp_path / "home", notify=shown.append)   # 개인용 EXE (목록 없음)
    assert shown == [] and not (tmp_path / "home").exists()


def test_reads_the_version_of_a_real_system_dll():
    found = runtime_setup.file_version(runtime_setup.system_directory() / "kernel32.dll")
    assert found is not None and found[0] >= 10


def test_runs_before_numpy_is_imported():
    # 모듈 자체가 NumPy를 불러오지 않고, 진입점이 앱(NumPy)보다 먼저 부른다
    done = subprocess.run([sys.executable, "-c", "import sys; from noseout import runtime_setup; "
                           "sys.exit('numpy' in sys.modules)"],
                          cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert done.returncode == 0
    source = (ROOT / "scripts" / "run_app.py").read_text(encoding="utf-8")
    assert source.index("ensure_for_frozen_app(") < source.index("from noseout.app import")
    helper = source.index("--ntfs-helper")
    assert helper < source.index("ensure_for_frozen_app(")      # 관리자 도우미 경로는 준비하지 않는다
