"""NTFS 이름 목록(MFT + USN, 2026-10-15): 합성 레코드·가짜 볼륨·가짜 관리자 도우미로 등록·따라잡기·저널 유실·이동·제외·권한 거부·
대체 방식·중지를 시험한다. 마지막 묶음은 실제 API(일반 권한 USN 읽기)를 pytest 임시 폴더(합성 파일)로만 확인한다. MFT 열거(관리자
권한)는 UAC가 필요해 자동 테스트에서 실제로 부르지 않는다."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import stat  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unicodedata  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from noseout import names, ntfs, ntfs_helper  # noqa: E402
from noseout.ntfs import Record  # noqa: E402

CREATE, DELETE = ntfs.USN_REASON_FILE_CREATE, ntfs.USN_REASON_FILE_DELETE
OLD, NEW = ntfs.USN_REASON_RENAME_OLD_NAME, ntfs.USN_REASON_RENAME_NEW_NAME
DIR = ntfs.FILE_ATTRIBUTE_DIRECTORY


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FakeJournal:
    """볼륨 하나의 변경 기록. 시험이 실제 폴더를 바꾸고 그 부모 폴더 참조 번호로 레코드를 남긴다(비권한 읽기처럼 이름 없음)."""

    def __init__(self):
        self.journal_id, self.first, self.next = 7, 100, 100
        self.records: list[Record] = []
        self.paths: dict[int, str] = {}      # 참조 번호 → 지금 경로 (OpenFileById 대신)
        self.guid, self.serial = "\\\\?\\Volume{test}\\", 1234
        self.reads = 0

    def folder(self, frn: int, path: Path) -> int:
        self.paths[frn] = str(path)
        return frn

    def log(self, parent: int, reason: int, frn: int = 999, attrs: int = 0) -> None:
        self.records.append(Record(frn, parent, self.next, reason, attrs, ""))
        self.next += 1

    def purge(self) -> None:                  # 오래된 기록이 지워짐
        self.first = self.next


class FakeVolume:
    def __init__(self, journal: FakeJournal):
        self.j = journal

    def query(self):
        return ntfs.JournalInfo(self.j.journal_id, self.j.first, self.j.next)

    def identity(self):
        return self.j.guid, self.j.serial

    def read(self, start, journal_id, mask=ntfs.NAME_REASONS):
        self.j.reads += 1
        if journal_id != self.j.journal_id or start < self.j.first:
            raise ntfs.JournalGap("기록 없음")
        records = [r for r in self.j.records if r.usn >= start][:3]     # 여러 번 나눠 읽게
        return (records[-1].usn + 1 if records else start), records

    def path_of(self, frn):
        path = self.j.paths.get(frn)
        if path == "denied":
            raise PermissionError(5, "denied")
        return path if path and os.path.exists(path) else None

    def close(self):
        pass


class FakeHelper:
    """관리자 도우미 대신: 미리 만든 MFT 레코드를 돌려준다. calls로 몇 번 불렸는지(=UAC 요청) 센다."""

    def __init__(self, records=None, error: Exception | None = None):
        self.records, self.error, self.calls = records or [], error, 0

    def enumerate(self, root, should_stop=lambda: False):
        self.calls += 1
        if self.error:
            raise self.error
        for start in range(0, len(self.records), 4):
            yield self.records[start:start + 4]


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "scope"
    (root / "docs").mkdir(parents=True)
    touch(root / "docs" / "report.txt")
    journal = FakeJournal()
    journal.folder(10, root)
    journal.folder(11, root / "docs")
    index = names.NameIndex(tmp_path / "names.sqlite")
    volume_root = os.path.splitdrive(str(tmp_path))[0] + "\\"
    walks = []

    def make(helper=None, exclusions=(), method="auto"):
        index.set_scope([], list(exclusions), name_roots=[str(root)])
        engine = ntfs.NtfsNames(index, lambda r: FakeVolume(journal), lambda p: (volume_root, ""), helper)

        def walk(roots):
            walks.append(list(roots))
            index.scan([], list(exclusions), name_roots=[str(root)], only=roots, report=report)

        report = names.ScanReport()
        return engine, report, walk

    yield root, journal, index, make, walks, volume_root
    index.close()


def listed(index, root) -> list[str]:
    return sorted(os.path.relpath(p, root) for (p,) in index.db.execute("SELECT path FROM entries"))


# 레코드 해석


def test_records_parse_v2_and_v3():
    raw = b"\0" * 8 + ntfs.pack_record(42, 5, "보고서.txt", attrs=0x20, reason=CREATE, usn=9, security_id=3)
    assert ntfs.parse_records(raw) == [Record(42, 5, 9, CREATE, 0x20, "보고서.txt", 3)]
    import struct

    name = "a.txt".encode("utf-16-le")
    v3 = struct.pack("<IHHQQQQqqIIIIHH", 80 + 8, 3, 0, 1, 2, 3, 4, 77, 0, NEW, 0, 0, 0x10, len(name), 76) + name
    record = ntfs.parse_records(b"\0" * 8 + v3.ljust(88, b"\0"))[0]
    assert (record.frn, record.parent, record.usn, record.name, record.is_dir) == (1 | 2 << 64, 3 | 4 << 64, 77, "a.txt", True)
    assert ntfs.parse_records(b"\0" * 8 + b"\x04\0\0\0") == []        # 깨진 길이는 버린다


# 등록·재실행 따라잡기·저널 유실


def test_first_registration_then_restart_catches_up_without_walking(setup):
    root, journal, index, make, walks, volume_root = setup
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    assert walks == [[str(root)]] and listed(index, root) == ["docs", "docs\\report.txt"]
    state = index.volume_state(volume_root)
    assert state["complete"] and state["usn"] == journal.next and state["registered_by"] == "walk"
    # 앱이 꺼진 동안: 추가·이름 변경·삭제
    touch(root / "docs" / "new.txt")
    journal.log(11, CREATE)
    os.rename(root / "docs" / "report.txt", root / "docs" / "REPORT.txt")    # 대소문자만
    journal.log(11, OLD)
    journal.log(11, NEW)
    (root / "music").mkdir()
    touch(root / "music" / "song.mp3")
    journal.log(10, CREATE, attrs=DIR)
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    assert walks == [[str(root)]]                                      # 다시 걷지 않았다
    assert listed(index, root) == ["docs", "docs\\REPORT.txt", "docs\\new.txt", "music", "music\\song.mp3"]
    assert index.volume_state(volume_root)["usn"] == journal.next


def test_changes_during_registration_are_not_lost(setup):
    root, journal, index, make, walks, _ = setup
    engine, report, walk = make()

    def slow_walk(roots):
        walk(roots)
        touch(root / "docs" / "late.txt")      # 걷기가 끝난 폴더에 등록 도중 생긴 파일
        journal.log(11, CREATE)

    engine.sync("auto", report, walk=slow_walk)
    assert "docs\\late.txt" in listed(index, root)


@pytest.mark.parametrize("change, why", [
    (lambda j: j.purge(), "지워짐"),
    (lambda j: setattr(j, "journal_id", 8), "초기화"),
    (lambda j: setattr(j, "guid", "\\\\?\\Volume{other}\\"), "볼륨이 바뀜"),
])
def test_lost_journal_or_new_volume_registers_again(setup, change, why):
    root, journal, index, make, walks, _ = setup
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    touch(root / "docs" / "while_off.txt")    # 기록이 지워져 USN으로는 모르는 변경
    journal.log(11, CREATE)
    change(journal)
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    assert len(walks) == 2 and "docs\\while_off.txt" in listed(index, root)
    assert why in engine.statuses[str(root)].detail


def test_poll_gap_marks_incomplete_and_returns_volume(setup):
    root, journal, index, make, _, volume_root = setup
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    journal.log(11, CREATE)
    journal.purge()
    assert engine.poll(report) == [volume_root]
    assert not index.volume_state(volume_root)["complete"]


def test_checkpoint_moves_only_after_changes_are_saved(setup, monkeypatch):
    root, journal, index, make, _, volume_root = setup
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    before = index.volume_state(volume_root)["usn"]
    touch(root / "docs" / "x.txt")
    journal.log(11, CREATE)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(index, "refresh_folders", broken)
    assert engine.poll(report) == []                                    # 실패는 로그로 남기고 다음 주기에 다시
    assert index.volume_state(volume_root)["usn"] == before            # 저장 실패: 위치를 옮기지 않았다
    monkeypatch.undo()
    engine.poll(report)
    assert "docs\\x.txt" in listed(index, root) and index.volume_state(volume_root)["usn"] == journal.next


def test_stopped_registration_is_not_complete(setup):
    root, journal, index, make, walks, volume_root = setup
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk, should_stop=lambda: report.added > 0)
    state = index.volume_state(volume_root)
    assert report.stopped and state is not None and not state["complete"]
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    assert len(walks) == 2 and index.volume_state(volume_root)["complete"]


# 실행 중 반영: 이동·삭제·제외·권한


def test_poll_applies_moves_deletes_and_exclusions(setup):
    root, journal, index, make, _, _ = setup
    (root / "keep").mkdir()
    journal.folder(12, root / "keep")
    engine, report, walk = make(exclusions=[str(root / "keep")])
    engine.sync("auto", report, walk=walk)
    assert listed(index, root) == ["docs", "docs\\report.txt"]
    # 폴더 이동: docs → archive\docs (옛 부모와 새 부모 모두 기록)
    (root / "archive").mkdir()
    journal.log(10, CREATE, attrs=DIR)
    journal.folder(13, root / "archive")
    os.rename(root / "docs", root / "archive" / "docs")
    journal.folder(11, root / "archive" / "docs")
    journal.log(10, OLD, frn=11, attrs=DIR)
    journal.log(13, NEW, frn=11, attrs=DIR)
    engine.poll(report)
    assert listed(index, root) == ["archive", "archive\\docs", "archive\\docs\\report.txt"]
    # 제외 폴더로 옮긴 파일은 결과에서 빠진다(제외된 부모 아래는 넣지 않음)
    os.rename(root / "archive" / "docs" / "report.txt", root / "keep" / "report.txt")
    journal.log(11, OLD)
    journal.log(12, NEW)
    # 비밀 파일·숨김 폴더·개발 폴더는 들어오지 않는다
    touch(root / "archive" / "id_rsa")
    (root / "archive" / "node_modules").mkdir()
    touch(root / "archive" / "node_modules" / "x.js")
    journal.log(13, CREATE)
    journal.log(13, CREATE, attrs=DIR)
    engine.poll(report)
    assert listed(index, root) == ["archive", "archive\\docs"]
    # 폴더 삭제: 그 아래까지 지운다
    os.rmdir(root / "archive" / "docs")
    journal.log(13, DELETE, frn=11, attrs=DIR)
    engine.poll(report)
    assert listed(index, root) == ["archive"]


def test_unreadable_parents_inside_scope_are_counted_outside_are_not_errors(setup):
    """2026-10-15 실측: '확인하지 못한 변경' 17건은 모두 범위 밖(사용자가 목록을 볼 수 없는 시스템 폴더)이었다. 범위 안의 열 수 없는
    폴더('상태 미확인')에서 온 기록만 그 범위의 미확인으로 세고, 범위 밖 기록은 오류처럼 보이지 않게 따로 센다."""
    root, journal, index, make, _, _ = setup
    (root / "locked").mkdir()
    engine, report, walk = make()
    engine.sync("auto", report, walk=walk)
    index._trouble(str(root / "locked"), "unreadable: access denied")      # 범위 안의 열 수 없는 폴더
    index.db.commit()
    journal.paths[50] = "denied"
    journal.paths[60] = "denied"
    opened = []
    FakeVolume.frn_of = lambda self, path: 50 if Path(path) == root / "locked" else None
    real_path_of = FakeVolume.path_of
    FakeVolume.path_of = lambda self, frn: opened.append(frn) or real_path_of(self, frn)
    try:
        journal.log(50, CREATE)
        for _ in range(3):
            journal.log(60, CREATE)                                       # 범위 밖: 같은 폴더의 기록 3건
        state = index.volume_states()[0]
        engine.catch_up(state["root"], journal.journal_id, state["usn"], report)
    finally:
        del FakeVolume.frn_of
        FakeVolume.path_of = real_path_of
    status = engine.statuses[str(root)]
    assert (status.unresolved, status.outside) == (1, 3)
    assert opened.count(60) == 1                                          # 거부된 폴더를 거듭 열지 않는다
    from noseout import app
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    dialog = app.FoldersDialog([])
    dialog.show_method_state({"method": "auto", "roots": {str(root): {"method": "usn", "detail": "",
                              "registered_by": "walk", "unresolved": 0, "outside": 3}}})
    assert "확인하지 못함" not in dialog.method_state.text()             # 범위 밖 기록만 있으면 오류로 보이지 않는다
    dialog.show_method_state({"method": "auto", "roots": {str(root): {"method": "usn", "detail": "",
                              "registered_by": "walk", "unresolved": 1, "outside": 3}}})
    assert "열 수 없는 폴더의 변경 1건" in dialog.method_state.text()
    dialog.deleteLater()


def test_unsupported_paths_use_folder_walk(setup):
    root, journal, index, make, walks, _ = setup
    index.set_scope([], [], name_roots=[str(root)])
    engine = ntfs.NtfsNames(index, lambda r: FakeVolume(journal), lambda p: (None, "네트워크 경로"))
    result = engine.sync("auto", names.ScanReport())
    assert result.walk_roots == [str(root)] and engine.statuses[str(root)].detail == "네트워크 경로"
    assert index.volume_states() == []
    result = ntfs.NtfsNames(index, lambda r: FakeVolume(journal), lambda p: ("C:\\", "")).sync("walk", names.ScanReport())
    assert result.walk_roots == [str(root)]


# MFT (가짜 도우미)


def mft_records(tmp_root: Path, volume_root: str, extra: list[Record] = ()) -> tuple[list[Record], dict]:
    """실제 합성 폴더를 MFT 열거처럼 레코드로 바꾼다(이름·부모·속성). 볼륨 루트부터 상위 폴더도 넣는다."""
    frns: dict[str, int] = {os.path.normcase(volume_root): ntfs.ROOT_RECORD | 1 << 48}
    records = []
    counter = [100]

    def frn_of(path: str) -> int:
        key = os.path.normcase(path)
        if key not in frns:
            counter[0] += 1
            frns[key] = counter[0] | 3 << 48
        return frns[key]

    parts = Path(tmp_root).parts
    current = parts[0]
    for part in parts[1:]:
        parent = current
        current = os.path.join(current, part)
        records.append(Record(frn_of(current), frn_of(parent), 0, 0, DIR, part, 1))
    for folder, subdirs, files in os.walk(tmp_root):
        for name in subdirs + files:
            path = os.path.join(folder, name)
            attrs = os.lstat(path).st_file_attributes | (DIR if name in subdirs else 0)
            records.append(Record(frn_of(path), frn_of(folder), 0, 0, attrs, name, 1))
    return records + list(extra), frns


def set_attrs(path: Path, attrs: int) -> None:
    import ctypes

    ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs)


def test_mft_import_matches_folder_walk(setup, tmp_path):
    root, journal, index, make, walks, volume_root = setup
    touch(root / "docs" / ".dotfile")
    touch(root / "docs" / "id_rsa")
    touch(root / "proj" / "node_modules" / "pkg" / "index.js")
    touch(root / "proj" / "main.py")
    touch(root / "hidden" / "inner.txt")
    set_attrs(root / "hidden", stat.FILE_ATTRIBUTE_HIDDEN)
    touch(root / "sys.dat")
    set_attrs(root / "sys.dat", stat.FILE_ATTRIBUTE_SYSTEM)
    touch(root / "excluded" / "x.txt")
    nfd = unicodedata.normalize("NFD", "한글문서.txt")
    touch(root / "docs" / nfd)
    excluded = [str(root / "excluded")]
    walked = names.NameIndex(tmp_path / "walk.sqlite")
    walked.scan([], excluded, name_roots=[str(root)])
    expected = sorted(p for (p,) in walked.db.execute("SELECT path FROM entries"))
    walked.close()
    records, _ = mft_records(root, volume_root)
    helper = FakeHelper(records)
    engine, report, walk = make(helper=helper, exclusions=excluded)
    engine.sync("ntfs", report, allow_prompt=True, walk=walk)
    assert helper.calls == 1 and walks == []                            # MFT로 등록, 걷지 않음
    assert sorted(p for (p,) in index.db.execute("SELECT path FROM entries")) == expected
    assert index.volume_state(volume_root)["registered_by"] == "mft"
    assert [h.name for h in index.search("한글문서")] == [nfd]           # NFD 이름도 NFC 검색어로


def test_mft_keeps_unlistable_folders_closed(setup):
    root, journal, index, make, walks, volume_root = setup
    touch(root / "other_user" / "private.txt")
    records, _ = mft_records(root, volume_root)
    index.set_scope([], [], name_roots=[str(root)])
    report = names.ScanReport()
    index.import_mft(volume_root, [str(root)], [records], report,
                     listable=lambda p: os.path.basename(p) != "other_user")
    paths = listed(index, root)
    assert "other_user" in paths and "other_user\\private.txt" not in paths   # 폴더 순회와 같게: 이름만, 안은 못 읽음
    trouble = [p for (p,) in index.db.execute("SELECT path FROM trouble")]
    assert trouble == [str(root / "other_user")]
    hit = next(h for h in index.search("other_user"))
    assert hit.status == "unknown"


def test_mft_long_paths_case_changes_and_stale_rows(setup):
    root, journal, index, make, walks, volume_root = setup
    long_dir = root.joinpath(*["긴폴더이름_" + "x" * 40] * 6)
    os.makedirs("\\\\?\\" + str(long_dir))
    with open("\\\\?\\" + str(long_dir / "끝.txt"), "w") as handle:
        handle.write("x")
    assert len(str(long_dir / "끝.txt")) > 260
    records, _ = mft_records(root, volume_root)
    index.set_scope([], [], name_roots=[str(root)])
    index.db.execute("INSERT INTO entries(path, parent, name, is_dir, kind, name_fold, stem_fold, dir_fold) "
                     "VALUES(?, ?, 'gone.txt', 0, 'doc', 'gone.txt', 'gone', '')", (str(root / "gone.txt"), str(root)))
    index.db.execute("INSERT INTO entries(path, parent, name, is_dir, kind, name_fold, stem_fold, dir_fold) "
                     "VALUES(?, ?, 'REPORT.txt', 0, 'doc', 'report.txt', 'report', 'docs')",
                     (str(root / "docs" / "REPORT.txt"), str(root / "docs")))
    index.db.commit()
    report = names.ScanReport()
    index.import_mft(volume_root, [str(root)], [records], report)
    paths = listed(index, root)
    assert "gone.txt" not in paths                                      # MFT에 없는 옛 행은 지운다
    assert "docs\\report.txt" in paths and "docs\\REPORT.txt" not in paths   # 대소문자는 지금 이름으로
    assert any(p.endswith("끝.txt") and len(p) > 260 for p in paths)


def test_helper_denied_or_failed_falls_back_and_never_prompts_on_auto_start(setup):
    root, journal, index, make, walks, volume_root = setup
    helper = FakeHelper(error=ntfs.HelperDenied("관리자 권한 요청이 거부되었습니다."))
    engine, report, walk = make(helper=helper)
    engine.sync("ntfs", report, allow_prompt=False, walk=walk)        # 앱 시작·자동 실행: 요청하지 않는다
    assert helper.calls == 0 and walks == [[str(root)]]
    assert "권한 요청 없이" in engine.statuses[str(root)].detail
    engine, report, walk = make(helper=helper)
    engine.sync("ntfs", report, allow_prompt=True, walk=walk, force=True)   # 사용자가 '다시 등록'
    assert helper.calls == 1 and len(walks) == 2
    assert "거부" in engine.statuses[str(root)].detail and index.volume_state(volume_root)["complete"]
    helper.error = ntfs.HelperFailed("MFT를 읽지 못했습니다: 5")
    engine, report, walk = make(helper=helper)
    engine.sync("ntfs", report, allow_prompt=True, walk=walk, force=True)
    assert len(walks) == 3 and "MFT를 읽지 못했습니다" in engine.statuses[str(root)].detail


# 관리자 도우미: 인자 검사와 파이프 규약 (UAC 없이, 같은 프로세스의 스레드로)


@pytest.mark.parametrize("argv", [
    [], ["--volume", "C", "--pipe", "\\\\.\\pipe\\NoseOut-" + "0" * 32],
    ["--volume", "CD", "--pipe", "\\\\.\\pipe\\NoseOut-" + "0" * 32, "--key", "0" * 64],
    ["--volume", "C", "--pipe", "\\\\.\\pipe\\other", "--key", "0" * 64],
    ["--volume", "C", "--pipe", "\\\\.\\pipe\\NoseOut-" + "0" * 32, "--key", "zz"],
    ["--volume", "C", "--pipe", "\\\\.\\pipe\\NoseOut-" + "0" * 32, "--key", "0" * 64, "--run", "cmd"],
])
def test_helper_rejects_anything_but_one_volume(argv):
    with pytest.raises(ValueError):
        ntfs_helper.parse_args(argv)
    assert ntfs_helper.main(argv) == 2


def test_helper_pipe_protocol(monkeypatch):
    sent = [b"\0" * 8 + ntfs.pack_record(101, ntfs.ROOT_RECORD, "a.txt"),
            b"\0" * 8 + ntfs.pack_record(102, ntfs.ROOT_RECORD, "b", attrs=DIR)]
    seen = {}

    def fake_enumerate(volume, send):
        seen["volume"] = volume
        send(b"OK")
        for chunk in sent:
            send(chunk)
        send(b"")

    monkeypatch.setattr(ntfs_helper, "enumerate_volume", fake_enumerate)
    monkeypatch.setattr(ntfs_helper, "local_ntfs", lambda v: True)
    threads = []

    def fake_launch(exe, args):
        seen["args"] = args
        thread = threading.Thread(target=ntfs_helper.main, args=(args[1:],))
        thread.start()
        threads.append(thread)
        return 0

    monkeypatch.setattr(ntfs, "_shell_execute_admin", fake_launch)
    monkeypatch.setattr(ntfs, "_wait", lambda process, ms: threads[0].join(ms / 1000))
    monkeypatch.setattr(ntfs, "_close", lambda process: None)
    helper = ntfs.Helper(command=lambda: ("NoseOut.exe", ["--ntfs-helper"]))
    batches = list(helper.enumerate("D:\\"))
    assert [r.name for batch in batches for r in batch] == ["a.txt", "b"]
    assert seen["volume"] == "D" and seen["args"][0] == "--ntfs-helper" and seen["args"][1:3] == ["--volume", "D"]


def test_helper_that_dies_before_connecting_is_reported(monkeypatch):
    monkeypatch.setattr(ntfs, "_shell_execute_admin", lambda exe, args: 0)
    monkeypatch.setattr(ntfs, "_wait", lambda process, ms: None)      # 바로 끝난 것처럼
    monkeypatch.setattr(ntfs, "_close", lambda process: None)
    with pytest.raises(ntfs.HelperFailed):
        list(ntfs.Helper(command=lambda: ("x", [])).enumerate("D:\\"))


# 실제 API (일반 권한): pytest 임시 폴더의 합성 파일만. 범위 밖 경로는 저장·출력하지 않는다.


def real_volume(path: Path) -> str | None:
    volume, _ = ntfs.volume_of(str(path))
    if volume is None:
        return None
    try:
        v = ntfs.Volume(volume)
        v.query()
        v.close()
        return volume
    except (ntfs.Unsupported, OSError):
        return None


def test_real_usn_journal_follows_synthetic_changes(tmp_path):
    root = tmp_path / "real"
    touch(root / "a" / "one.txt")
    (root / "skip").mkdir()
    volume = real_volume(root)
    if volume is None:
        pytest.skip("이 볼륨은 일반 권한 USN을 쓸 수 없음")
    exclusions = [str(root / "skip")]
    index = names.NameIndex(tmp_path / "names.sqlite")
    index.set_scope([], exclusions, name_roots=[str(root)])
    report = names.ScanReport()
    walk = lambda roots: index.scan([], exclusions, name_roots=[str(root)], only=roots, report=report)  # noqa: E731
    engine = ntfs.NtfsNames(index)
    engine.sync("auto", report, walk=walk)
    assert index.volume_state(volume)["complete"]
    os.rename(root / "a" / "one.txt", root / "a" / "ONE.txt")
    (root / "b" / "deep").mkdir(parents=True)
    touch(root / "b" / "deep" / "x.txt")
    os.rename(root / "b", root / "a" / "moved")
    touch(root / "a" / "two.txt").rename(root / "skip" / "two.txt")
    deadline = time.time() + 5
    while time.time() < deadline:
        engine.poll(report)
        if "a\\moved\\deep\\x.txt" in listed(index, root):
            break
        time.sleep(0.1)
    assert listed(index, root) == ["a", "a\\ONE.txt", "a\\moved", "a\\moved\\deep", "a\\moved\\deep\\x.txt"]
    engine.close()
    # 앱을 다시 켠 것처럼: 꺼진 동안의 삭제를 저장한 위치부터 따라잡는다(다시 걷지 않음)
    import shutil

    shutil.rmtree(root / "a" / "moved")
    walked = []
    engine = ntfs.NtfsNames(index)
    engine.sync("auto", report, walk=lambda roots: walked.append(roots))
    assert walked == [] and listed(index, root) == ["a", "a\\ONE.txt"]
    engine.close()
    index.close()


# 앱: 설정의 방식 선택, 감시 연동, 실행 중 반영, UAC는 사용자가 고를 때만


def test_app_method_choice_watch_and_polling(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from noseout import app, services
    from test_review_fixes import FakeEmbedder

    qt = QApplication.instance() or QApplication([])
    monkeypatch.setattr(services.SearchService, "start", lambda self: None)

    def wait_for(condition, seconds=10.0):
        end = time.time() + seconds
        while time.time() < end:
            QCoreApplication.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return condition()

    root = tmp_path / "scope"
    touch(root / "docs" / "report.txt")
    journal = FakeJournal()
    journal.folder(10, root)
    journal.folder(11, root / "docs")
    volume_root = os.path.splitdrive(str(tmp_path))[0] + "\\"
    helper = FakeHelper(mft_records(root, volume_root)[0])
    backend = {"volume_factory": lambda r: FakeVolume(journal), "volume_of": lambda p: (volume_root, ""), "helper": helper}
    base = tmp_path / "home"
    app.Settings(name_folders=[str(root)], name_method="ntfs").save(base / "settings.json")
    application = app.App(qt, base, index_factory=FakeEmbedder, register_hotkey=False, setup=False, name_backend=backend)
    try:
        application.start()
        assert wait_for(lambda: application.name_method_state.get("usn_roots"))
        assert helper.calls == 0                                       # 앱 시작: 빠른 NTFS여도 UAC를 띄우지 않는다
        assert "권한 요청 없이" in application.folders.method_state.text()
        watch = application.watch
        assert os.path.normcase(str(root)) not in {os.path.normcase(r) for r in watch._watched()}   # 감시 핸들 없음
        # 실행 중 반영 (약 1초마다 변경 기록을 읽는다)
        touch(root / "docs" / "fresh.txt")
        journal.log(11, CREATE)
        def entry_names():
            reader = names.NameIndex(base / "names.sqlite", readonly=True)
            try:
                return {Path(p).name for (p,) in reader.db.execute("SELECT path FROM entries")} if reader.ready() else set()
            finally:
                reader.close()

        assert wait_for(lambda: "fresh.txt" in entry_names())
        # 사용자가 '다시 등록'을 누름: 빠른 NTFS라 이때만 관리자 도우미(UAC)
        application.folders.name_rescan_button.click()
        assert wait_for(lambda: helper.calls == 1)
        assert wait_for(lambda: "MFT 목록" in application.folders.method_state.text())
        # 폴더 순회로 바꾸면 볼륨 상태를 잊고 감시 핸들을 다시 연다
        application.folders.method_box.setCurrentIndex(application.folders.method_box.findData("walk"))
        assert app.Settings.load(base / "settings.json").name_method == "walk"
        assert wait_for(lambda: application.name_method_state.get("method") == "walk"
                        and not application.name_method_state.get("usn_roots"))
        assert os.path.normcase(str(root)) in {os.path.normcase(r) for r in watch._watched()}
        assert helper.calls == 1
    finally:
        application.quit()


# 범위별 등록 방법 (2026-10-15): 같은 드라이브에 MFT로 등록한 범위와 폴더 순회로 등록한 범위


def test_registration_method_is_kept_per_root(tmp_path):
    volume_dir = tmp_path / "vol"
    a, b = volume_dir / "mft_folder", volume_dir / "walk_folder"
    touch(a / "a_doc.txt")
    touch(b / "b_doc.txt")
    journal = FakeJournal()
    journal.folder(20, a)
    journal.folder(21, b)
    volume_root = os.path.splitdrive(str(tmp_path))[0] + "\\"
    index = names.NameIndex(tmp_path / "names.sqlite")
    helper = FakeHelper(mft_records(volume_dir, volume_root)[0])
    walks = []

    def run(name_roots, prompt=False, force=False):
        index.set_scope([], [], name_roots=[str(r) for r in name_roots])
        engine = ntfs.NtfsNames(index, lambda r: FakeVolume(journal), lambda p: (volume_root, ""), helper)
        report = names.ScanReport()

        def walk(roots):
            walks.append(sorted(roots))
            index.scan([], [], name_roots=[str(r) for r in name_roots], only=roots, report=report)

        engine.sync("ntfs", report, allow_prompt=prompt, walk=walk, force=force)
        return engine, {Path(r).name: (s.registered_by, s.detail) for r, s in engine.statuses.items()}

    _, shown = run([a], prompt=True)                                   # 사용자가 빠른 NTFS로 등록
    assert shown == {"mft_folder": ("mft", "")} and helper.calls == 1 and walks == []
    _, shown = run([a, b])                                             # 범위 추가: 권한 요청 없이 폴더 순회
    assert helper.calls == 1 and walks == [[str(b)]]
    assert shown["mft_folder"] == ("mft", "") and shown["walk_folder"][0] == "walk"
    assert "권한 요청 없이" in shown["walk_folder"][1]
    stored = index.volume_state(volume_root)["root_info"]
    assert {Path(k).name: v["by"] for k, v in stored.items()} == {"mft_folder": "mft", "walk_folder": "walk"}
    # 재실행: 다시 걷거나 도우미를 부르지 않고 범위별 방법을 그대로 보여 준다
    engine, shown = run([a, b])
    assert helper.calls == 1 and walks == [[str(b)]]
    assert shown["mft_folder"][0] == "mft" and shown["walk_folder"][0] == "walk"
    # 두 범위 모두 변경 반영이 유지된다
    touch(a / "a_new.txt")
    journal.log(20, CREATE)
    os.rename(b / "b_doc.txt", b / "b_renamed.txt")
    journal.log(21, OLD)
    journal.log(21, NEW)
    engine.poll(names.ScanReport())
    found = {Path(p).name for (p,) in index.db.execute("SELECT path FROM entries")}
    assert {"a_doc.txt", "a_new.txt", "b_renamed.txt"} <= found and "b_doc.txt" not in found
    # 범위 하나를 빼면 그 범위의 정보만 지운다
    _, shown = run([a])
    assert shown == {"mft_folder": ("mft", "")}
    assert [Path(k).name for k in index.volume_state(volume_root)["root_info"]] == ["mft_folder"]
    # '다시 등록'(사용자): 고른 범위 모두 MFT로
    _, shown = run([a, b], prompt=True, force=True)
    assert helper.calls == 2 and shown == {"mft_folder": ("mft", ""), "walk_folder": ("mft", "")}
    index.close()


def test_old_volume_state_without_per_root_info_still_loads(tmp_path):
    db = tmp_path / "names.sqlite"
    index = names.NameIndex(db)
    index.db.execute("DROP TABLE volumes")
    index.db.execute("""CREATE TABLE volumes(root TEXT PRIMARY KEY COLLATE NOCASE, guid TEXT, serial INTEGER,
                        journal_id INTEGER, usn INTEGER NOT NULL, complete INTEGER NOT NULL, roots TEXT NOT NULL,
                        registered_by TEXT, detail TEXT, updated_at REAL)""")       # 범위별 정보가 없던 형식
    index.db.execute("INSERT INTO volumes VALUES(?, 'g', 1, 7, 100, 1, ?, 'mft', '', 0)",
                     ("C:\\", '["c:\\\\a", "c:\\\\b"]'))
    index.db.commit()
    index.close()
    again = names.NameIndex(db)                                        # 열만 더하고 상태는 지우지 않는다
    state = again.volume_state("C:\\")
    assert state["complete"] and state["usn"] == 100
    assert state["root_info"] == {"c:\\a": {"by": "mft", "note": ""}, "c:\\b": {"by": "mft", "note": ""}}
    again.close()


def test_settings_show_each_root_with_its_own_method():
    from PySide6.QtWidgets import QApplication

    from noseout import app

    QApplication.instance() or QApplication([])
    dialog = app.FoldersDialog([])
    dialog.show_method_state({"method": "ntfs", "roots": {
        "C:\\data\\mft_folder": {"method": "usn", "detail": "", "registered_by": "mft", "unresolved": 0, "outside": 0},
        "C:\\data\\walk_folder": {"method": "usn", "detail": "권한 요청 없이 폴더 순회로 등록함('다시 등록'을 누르면 MFT)",
                                 "registered_by": "walk", "unresolved": 0, "outside": 0}}})
    lines = dialog.method_state.text().splitlines()
    assert "C:\\data\\mft_folder: 변경 기록(USN)으로 갱신 · 등록: MFT 목록(관리자 도우미)" in lines
    assert any(line.startswith("C:\\data\\walk_folder: 변경 기록(USN)으로 갱신 · 등록: 폴더 순회 · 권한 요청 없이")
               for line in lines)
    dialog.deleteLater()


def test_real_usn_with_mft_and_walk_roots_on_one_drive(tmp_path):
    """실제 API(일반 권한 USN): 같은 드라이브의 두 범위를 MFT(합성 레코드 도우미)와 폴더 순회로 등록하고, 실제 변경 반영과
    재실행 뒤 범위별 등록 방법 보존을 확인한다. 실제 관리자 MFT 열거는 UAC가 필요해 여기서 부르지 않는다."""
    volume_dir = tmp_path / "vol"
    a, b = volume_dir / "mft_root", volume_dir / "walk_root"
    touch(a / "a_doc.txt")
    touch(b / "b_doc.txt")
    volume = real_volume(volume_dir)
    if volume is None:
        pytest.skip("이 볼륨은 일반 권한 USN을 쓸 수 없음")
    index = names.NameIndex(tmp_path / "names.sqlite")
    helper = FakeHelper(mft_records(volume_dir, volume)[0])
    walks = []

    def run(roots, prompt=False):
        index.set_scope([], [], name_roots=[str(r) for r in roots])
        engine = ntfs.NtfsNames(index, helper=helper)
        report = names.ScanReport()

        def walk(only):
            walks.append(sorted(only))
            index.scan([], [], name_roots=[str(r) for r in roots], only=only, report=report)

        engine.sync("ntfs", report, allow_prompt=prompt, walk=walk)
        return engine, {Path(r).name: s.registered_by for r, s in engine.statuses.items()}

    engine, shown = run([a], prompt=True)
    engine.close()
    engine, shown = run([a, b])
    assert shown == {"mft_root": "mft", "walk_root": "walk"} and walks == [[str(b)]] and helper.calls == 1
    touch(a / "a_live.txt")
    os.rename(b / "b_doc.txt", b / "b_live.txt")
    deadline = time.time() + 5
    while time.time() < deadline:
        engine.poll(names.ScanReport())
        found = {Path(p).name for (p,) in index.db.execute("SELECT path FROM entries")}
        if {"a_live.txt", "b_live.txt"} <= found:
            break
        time.sleep(0.1)
    assert {"a_doc.txt", "a_live.txt", "b_live.txt"} <= found and "b_doc.txt" not in found
    engine.close()
    os.remove(a / "a_live.txt")                                         # 꺼진 동안의 변경
    engine, shown = run([a, b])                                         # 재실행
    assert shown == {"mft_root": "mft", "walk_root": "walk"} and walks == [[str(b)]] and helper.calls == 1
    assert "a_live.txt" not in {Path(p).name for (p,) in index.db.execute("SELECT path FROM entries")}
    engine.close()
    index.close()
