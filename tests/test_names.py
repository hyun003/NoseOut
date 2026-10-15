"""빠른 이름 검색(names.py): 저장·증분 갱신·검색·범위 규칙. 합성 빈 파일을 pytest 임시 폴더(.tmp) 안에만 만든다."""

import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

from noseout import names, rules
from noseout.names import NameIndex


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def found(index: NameIndex, query: str, **kw) -> list[str]:
    return [Path(h.path).name for h in index.search(query, **kw)]


def paths(index: NameIndex) -> set[str]:
    return {p for (p,) in index.db.execute("SELECT path FROM entries")}


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "chosen"
    touch(root / "보고서" / "2024" / "의료비청구서_3월.PDF")
    touch(root / "Report-Final.docx")
    touch(root / "run.bat")
    touch(root / "사진" / "beach.jpg")
    return root


def test_fold_case_nfd_fullwidth_and_two_letter_korean(tmp_path, tree):
    touch(tree / unicodedata.normalize("NFD", "계약서_최종.hwp"))
    touch(tree / "ＦＵＬＬ_width.txt")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([tree], [])
        assert found(index, "청구") == ["의료비청구서_3월.PDF"]          # 2글자 한국어, 단어 가운데
        assert found(index, "비청") == ["의료비청구서_3월.PDF"]
        assert found(index, "REPORT") == ["Report-Final.docx"]
        assert found(index, "port-fin") == ["Report-Final.docx"]           # 부분 문자열, 기호 포함
        assert found(index, "계약서") == [unicodedata.normalize("NFD", "계약서_최종.hwp")]   # NFD 이름을 NFC 검색어로
        assert found(index, "full_WIDTH") == ["ＦＵＬＬ_width.txt"]
        # 단어는 모두 이름이나 고른 폴더 아래 경로에 있어야 한다(AND)
        assert found(index, "2024 청구서") == ["의료비청구서_3월.PDF"]
        assert found(index, "2025 청구서") == []
        # 고른 폴더 위의 경로 이름(chosen)으로는 찾지 않는다
        assert found(index, "chosen") == []


def test_ranking_exact_then_prefix_then_inside_then_folder_only(tmp_path):
    root = tmp_path / "chosen"
    for name in ("tax", "tax.pdf", "taxes_2024.pdf", "my_tax_note.txt"):
        touch(root / name) if "." in name else (root / name).mkdir(parents=True)
    touch(root / "tax" / "inside.txt")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([root], [])
        hits = index.search("tax")
        assert [(Path(h.path).name, h.tier) for h in hits] == [
            ("tax", 0), ("tax.pdf", 0), ("taxes_2024.pdf", 1), ("my_tax_note.txt", 2), ("inside.txt", 3)]
        assert hits[0].is_dir and hits[0].kind == "folder" and hits[1].kind == "pdf"


def test_scope_rules_keep_safety_but_not_content_rules(tmp_path, tree, monkeypatch):
    touch(tree / ".hidden.txt")
    touch(tree / ".git" / "config")
    touch(tree / "node_modules" / "lib" / "index.js")
    touch(tree / "id_rsa")
    touch(tree / "server.pem")
    touch(tree / "private" / "diary.txt")
    # 코드 프로젝트 안: 내용 분석은 스크린샷만 하지만 이름 목록에는 안전한 이름을 모두 넣는다
    touch(tree / "app" / "package.json")
    touch(tree / "app" / "main.py")
    touch(tree / "app" / "setup.exe")
    # 시스템 폴더 이름과 같을 뿐인 폴더는 들어간다. 실제 금지 폴더(여기서는 LOCALAPPDATA로 가장)는 들어가지 않는다
    touch(tree / "Windows" / "note.txt")
    touch(tree / "Local" / "cache.txt")
    monkeypatch.setenv("LOCALAPPDATA", str(tree / "Local"))
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tree / "link"), str(tree.parent)], capture_output=True)
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([tree], [tree / "private"])
        names_found = {Path(p).name for p in paths(index)}
    assert {"main.py", "package.json", "setup.exe", "Windows", "note.txt", "run.bat"} <= names_found
    assert not names_found & {".hidden.txt", ".git", "config", "node_modules", "index.js", "id_rsa", "server.pem",
                              "private", "diary.txt", "Local", "cache.txt", "link"}
    if made.returncode != 0:
        pytest.skip("can't create a junction here (the rest passed)")


def test_forbidden_name_prefilter_matches_the_full_check(tmp_path, monkeypatch):
    """사전 필터(rules.forbidden_names)가 걸러 내지 않은 폴더는 전체 검사에서도 거부되지 않아야 한다."""
    root = tmp_path / "chosen"
    monkeypatch.setenv("APPDATA", str(root / "x" / "Roaming"))
    for folder in ("Windows", "Program Files", "AppData", "Roaming", "x/Roaming", "Users", "a/b/c"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    prefilter = rules.forbidden_names()
    for folder in root.rglob("*"):
        if rules.forbidden_root_reason(folder):
            assert folder.name.casefold() in prefilter, folder
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([root], [])
        assert not any(Path(p).name == "Roaming" and Path(p).parent.name == "x" for p in paths(index))


def test_incremental_add_delete_rename_and_folder_rename(tmp_path, tree):
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([tree], [])
        touch(tree / "새파일.txt")
        assert index.apply([str(tree / "새파일.txt")]).added == 1
        os.rename(tree / "새파일.txt", tree / "바뀐이름.txt")
        index.apply([str(tree / "새파일.txt"), str(tree / "바뀐이름.txt")])
        assert found(index, "새파일") == [] and found(index, "바뀐이름") == ["바뀐이름.txt"]
        # 폴더 이름 변경: 옛 경로 아래를 지우고 새 폴더를 다시 걷는다 (경로 문자열을 고쳐 쓰지 않는다)
        os.rename(tree / "보고서", tree / "문서함")
        index.apply([str(tree / "보고서"), str(tree / "문서함")])
        assert not any("보고서" in p for p in paths(index))
        assert [h.path for h in index.search("청구서")] == [str(tree / "문서함" / "2024" / "의료비청구서_3월.PDF")]
        assert found(index, "문서함 2024") == ["2024", "의료비청구서_3월.PDF"]
        (tree / "바뀐이름.txt").unlink()
        assert index.apply([str(tree / "바뀐이름.txt")]).removed == 1
        shutil.rmtree(tree / "문서함")
        index.apply([str(tree / "문서함")])
        assert not any("문서함" in p for p in paths(index))
        # 감시 결과와 전체 걷기 결과가 같다
        incremental = paths(index)
        index.db.execute("DELETE FROM entries")
        index.scan([tree], [])
        assert paths(index) == incremental


def test_case_only_renames_and_path_boundaries(tmp_path):
    root = tmp_path / "chosen"
    touch(root / "a" / "inside.txt")
    touch(root / "a2" / "neighbour.txt")
    touch(root / "readme.TXT")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([root], [])
        os.rename(root / "readme.TXT", root / "README.txt")
        os.rename(root / "a", root / "A")
        index.apply([str(root / "readme.TXT"), str(root / "README.txt"), str(root / "a"), str(root / "A")])
        stored = paths(index)
        assert str(root / "README.txt") in stored and str(root / "readme.TXT") not in stored
        assert str(root / "A" / "inside.txt") in stored and str(root / "a" / "inside.txt") not in stored
        # "A"를 지워도 이름이 겹치는 "a2"는 남는다 (경로 경계)
        shutil.rmtree(root / "A")
        index.apply([str(root / "A")])
        assert str(root / "a2" / "neighbour.txt") in paths(index)
        assert not any(p.startswith(str(root / "A") + "\\") for p in paths(index))


def test_overlapping_roots_are_listed_once_and_survive_removal(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    touch(inner / "deep" / "plan.txt")
    touch(outer / "top.txt")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([outer, inner], [])
        assert index.db.execute("SELECT COUNT(*) FROM entries WHERE name = 'plan.txt'").fetchone()[0] == 1
        assert found(index, "inner plan") == ["plan.txt"]      # 바깥 폴더 기준 상대 경로
        index.scan([inner], [])                                  # 바깥 폴더를 뺌
        assert found(index, "top") == [] and found(index, "plan") == ["plan.txt"]
        assert found(index, "inner plan") == []                  # 상대 경로를 안쪽 폴더 기준으로 다시 계산
        assert found(index, "deep plan") == ["plan.txt"]
        # 안쪽 폴더를 제외하면 안쪽을 따로 골라도 보이지 않는다(검색 범위 규칙과 같음)
        index.scan([outer, inner], [inner])
        assert found(index, "plan") == [] and found(index, "top") == ["top.txt"]


def test_unreadable_folder_and_missing_root_are_kept_and_marked(tmp_path, tree, monkeypatch):
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([tree], [])
        before = paths(index)
        real = os.scandir
        locked = str(tree / "보고서")

        def denied(path):
            if os.path.normcase(str(path)) == os.path.normcase(locked):
                raise PermissionError(5, "Access is denied")
            return real(path)

        monkeypatch.setattr(names.os, "scandir", denied)
        report = index.scan([tree], [])
        assert report.removed == 0 and paths(index) == before and report.trouble == 1
        hit = index.search("청구서")[0]
        assert hit.status == "unknown"
        assert index.search("beach")[0].status is None
        # 감시 이벤트로 그 아래를 살필 때도 지우지 않는다
        index.apply([str(tree / "보고서" / "2024" / "의료비청구서_3월.PDF")], [locked])
        assert paths(index) == before
        monkeypatch.setattr(names.os, "scandir", real)
        index.scan([tree], [])
        assert index.search("청구서")[0].status is None
        # 고른 폴더가 사라짐(외장 드라이브 분리 등): 지우지 않고 "접근 불가"
        moved = tmp_path / "away"
        os.rename(tree, moved)
        for run in (lambda: index.scan([tree], []), lambda: index.apply([str(tree / "run.bat")], [str(tree)])):
            report = run()
            assert report.removed == 0 and paths(index) == before and report.unavailable
        assert {h.status for h in index.search("r")} == {"unavailable"}
        os.rename(moved, tree)
        index.apply([], [str(tree)])
        assert {h.status for h in index.search("r")} == {None}


def test_scope_and_kind_filters_in_search(tmp_path, tree):
    other = tmp_path / "other"
    touch(other / "report_other.pdf")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([tree, other], [])
        assert sorted(found(index, "report")) == ["Report-Final.docx", "report_other.pdf"]
        assert found(index, "report", roots=[str(tree)], exclusions=[]) == ["Report-Final.docx"]
        assert found(index, "청구서", roots=[str(tree)], exclusions=[str(tree / "보고서")]) == []
        assert found(index, "report", kinds={"pdf"}) == ["report_other.pdf"]
        assert found(index, "  ") == [] and found(index, "\x01") == []


def test_reader_sees_committed_rows_while_registering(tmp_path, tree, monkeypatch):
    for i in range(30):
        touch(tree / f"folder{i:02d}" / f"file{i:02d}.txt")
    monkeypatch.setattr(names, "COMMIT_ROWS", 5)
    seen = []
    with NameIndex(tmp_path / "names.sqlite") as writer, NameIndex(tmp_path / "names.sqlite", readonly=True) as reader:
        assert reader.search("file") == []
        writer.scan([tree], [], progress=lambda *_: seen.append(len(reader.search("file", limit=100))))
        assert 0 < min(n for n in seen if n) < 30                      # 등록이 끝나기 전부터 찾는다
        assert len(reader.search("file", limit=100)) == 30


def test_reader_does_not_create_the_file_and_format_change_rebuilds(tmp_path, tree):
    reader = NameIndex(tmp_path / "names.sqlite", readonly=True)
    assert reader.search("report") == [] and not (tmp_path / "names.sqlite").exists()
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([tree], [])
        index.db.execute("UPDATE meta SET value = '0' WHERE key = 'format'")
        index.db.commit()
    with NameIndex(tmp_path / "names.sqlite") as index:
        assert paths(index) == set()          # 다른 형식의 이름 목록은 다시 만든다(파생 데이터)
        index.scan([tree], [])
        assert found(index, "report") == ["Report-Final.docx"]
    assert found(reader, "report") == ["Report-Final.docx"]     # 파일이 생긴 뒤에는 읽는 쪽도 연다
    reader.close()


def test_candidates_outside_the_scope_fall_back_to_the_exact_query(tmp_path):
    """범위를 막 바꿔 이름 목록에 범위 밖 행이 많이 남은 순간에도 범위 안 결과를 빠뜨리지 않는다."""
    near, far = tmp_path / "near", tmp_path / "far"
    for i in range(names.DEFAULT_LIMIT * names.CANDIDATES + 10):
        touch(far / f"r{i}.txt")                 # 짧은 이름: 순위에서 앞선다
    for i in range(5):
        touch(near / f"report_near_{i}.txt")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([near, far], [])
        hits = index.search("r", roots=[str(near)], exclusions=[])
        assert sorted(Path(h.path).name for h in hits) == [f"report_near_{i}.txt" for i in range(5)]
        assert len(index.search("r")) == names.DEFAULT_LIMIT


def test_code_folders_list_safe_names_and_keep_code_rules(tmp_path):
    """코드 폴더(코드 프로젝트)의 안전한 파일·폴더 이름도 등록한다. 기존 제외(개발 폴더·숨김·비밀 파일)와 코드 색인의 폴더
    제외(남의 코드·빌드 산출물)는 지킨다. 검색 폴더 안의 build 같은 폴더는 일반 규칙대로 둔다."""
    repo, docs = tmp_path / "repo", tmp_path / "docs"
    for name in ("package.json", "src/main.py", "src/util/helpers.ts", "README.md", "assets/logo.png", "Makefile",
                 ".gitignore", ".env", "id_rsa", "server.key", "node_modules/x/index.js", ".venv/lib/a.py",
                 "vendor/lib.c", "build/out.o", "dist/app.js", "__pycache__/m.pyc"):
        touch(repo / name)
    touch(repo / "sub" / ".git")                   # 서브모듈(.git이 파일)
    touch(repo / "sub" / "inner.py")
    touch(docs / "build" / "plan.md")
    with NameIndex(tmp_path / "names.sqlite") as index:
        index.scan([docs], [], code_roots=[repo])
        listed = {Path(p).relative_to(tmp_path).as_posix() for p in paths(index)}
        assert {"repo/package.json", "repo/src", "repo/src/main.py", "repo/src/util", "repo/src/util/helpers.ts",
                "repo/README.md", "repo/assets", "repo/assets/logo.png", "repo/Makefile", "docs/build",
                "docs/build/plan.md"} == listed
        assert found(index, "helpers") == ["helpers.ts"] and found(index, "util helpers") == ["helpers.ts"]
        # 같은 폴더를 검색 폴더로도 고르면 일반 규칙(검색 폴더에서는 build·dist·vendor도 이름을 넣는다)
        index.scan([docs, repo], [], code_roots=[repo])
        assert str(repo / "build" / "out.o") in paths(index) and str(repo / "node_modules") not in paths(index)
        # 코드 폴더를 빼면 그 이름은 사라진다
        index.scan([docs], [], code_roots=[])
        assert not any(p.startswith(str(repo)) for p in paths(index))
