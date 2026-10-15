"""무엇을 읽고 무엇을 건너뛰는지 (모델 없이)."""

import os
from pathlib import Path

import pytest
from PIL import Image

from noseout import crawler, rules


@pytest.mark.parametrize("name, expected", [
    ("report.pdf", ("index", "pdf")),
    ("notes.md", ("index", "doc")),
    ("letter.docx", ("index", "doc")),
    ("page.HTML", ("index", "doc")),
    ("photo.HEIC", ("index", "image")),
    ("Screenshot 2026-10-01 093000.png", ("index", "screenshot")),
    ("스크린샷 2026-10-01.png", ("index", "screenshot")),
    ("server.key", ("skip", "key or password file")),
    ("id_ed25519", ("skip", "key or password file")),
    (".env.local", ("skip", "key or password file")),
    ("credentials.json", ("skip", "key or password file")),
    ("data.csv", ("skip", "code or data file")),
    ("main.py", ("skip", "code or data file")),
    ("song.mp3", ("index", "audio")),
    ("clip.MOV", ("index", "video")),
    ("archive.zip", ("ignore", "")),
])
def test_classify(name, expected):
    assert rules.classify(name) == expected


@pytest.mark.parametrize("stem, machine", [
    ("clip_000123", True), ("frame-0042", True), ("3f9a1c0e-1b2c", True),
    ("IMG_1234", False), ("KakaoTalk_20260101_123456", False), ("beach day", False),
])
def test_machine_names(stem, machine):
    assert rules.looks_machine_named(stem) is machine


def test_forbidden_roots():
    assert rules.forbidden_root_reason(Path("C:/")) == "drive root"
    assert rules.forbidden_root_reason(Path.home()) == "whole user folder"
    assert rules.forbidden_root_reason(Path(os.environ["SystemRoot"]) / "System32").startswith("system folder")
    assert rules.forbidden_root_reason(Path(os.environ["LOCALAPPDATA"]) / "Temp").startswith("system folder")
    assert rules.forbidden_root_reason(Path(__file__).parent) is None
    with pytest.raises(ValueError, match="drive root"):
        crawler.crawl(["C:/"])


def test_crawl_test_data(test_data):
    result = crawler.crawl([test_data])
    found = {Path(f.path).relative_to(test_data).as_posix(): f.kind for f in result.files}
    assert found["documents/lease_agreement.pdf"] == "pdf"
    assert found["documents/invoice_INV-4821.docx"] == "doc"
    assert found["documents/김치찌개_레시피.txt"] == "doc"
    assert found["pictures/sunset_beach.heic"] == "image"
    assert found["pictures/Screenshots/Screenshot 2026-10-01 093000.png"] == "screenshot"
    # 코드 프로젝트 안에서는 스크린샷만
    assert found["code_project/Screenshot 2026-10-02 retry.png"] == "screenshot"
    assert "code_project/NOTES.md" not in found
    # 비밀·숨김·개발 폴더는 읽지 않는다
    for never in ["id_rsa", "certs/server.key", ".hidden/diary.txt", "node_modules/left-pad/README.md",
                  "measurements.csv"]:
        assert never not in found
    skipped = {Path(p).relative_to(test_data).as_posix(): why for p, why in result.skipped.items()}
    assert skipped["id_rsa"] == "key or password file"
    assert skipped["node_modules"] == "developer folder"
    assert skipped["code_project/NOTES.md"] == "inside a code project"
    assert not any(".hidden" in p for p in skipped)  # 숨김은 보고도 하지 않는다


def test_dataset_folder_is_skipped(tmp_path):
    root = tmp_path / "pics"
    (root / "frames").mkdir(parents=True)
    tiny = Image.new("RGB", (8, 8))
    for i in range(crawler.DATASET_MINIMUM):
        tiny.save(root / "frames" / f"frame_{i:05d}.png")
    tiny.save(root / "IMG_0001.png")
    result = crawler.crawl([root])
    assert [Path(f.path).name for f in result.files] == ["IMG_0001.png"]
    assert result.skipped[str((root / "frames").resolve())].startswith("looks like a dataset")


def test_links_are_not_followed(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret_plan.md").write_text("outside the chosen folder", encoding="utf-8")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except OSError:
        # 심볼릭 링크는 개발자 모드나 관리자 권한이 필요하다: 권한 없이 되는 디렉터리 정션으로 대신 확인
        import subprocess

        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], capture_output=True)
        if made.returncode != 0:
            pytest.skip("can't create a symlink or junction here")
    result = crawler.crawl([root])
    assert result.files == []
    assert list(result.skipped.values()) == ["link"]
