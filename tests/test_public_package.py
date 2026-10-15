"""공개용 패키지·소스 스냅샷의 구성 규칙. 저장소 파일만 읽는다(빌드·내보내기는 하지 않음)."""

from pathlib import Path, PurePosixPath
import importlib.util
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [
    "examples/widgets/dialogs/licensewizard/licensewizard.py",
    "examples/widgets/dialogs/licensewizard/doc/licensewizard.png",
    "examples/widgets/doc/src/licensewizard.qdoc",
    "tools/license_check.py",
    "src/3rdparty/libjpeg/generate_copyright.py",
    "src/3rdparty/freetype/src/tools/update-copyright",
    "src/3rdparty/freetype/src/tools/update-copyright-year",
    "src/3rdparty/freetype/src/tools/no-copyright",
    "src/gui/vulkan/licenseheader.h.in",
    "src/tools/moc/util/licenseheader.cpp.in",
    "cmake/QtPublicSbomLicenseHelpers.cmake",
    "coin/instructions/run_license_check.yaml",
    "tests/auto/corelib/io/qfile/forCopying.txt",
    "tests/manual/examples/widgets/tools/settingseditor/inifiles/licensepage.ini",
    "tests/auto/cmake/RunCMake/Sbom/custom_licenses/LicenseRef-TestLicense.txt",
    "licenseRule.json",
    "sources/pyside6/PySide6/licensecomment.txt",
])
def test_qt_notice_filter_skips_files_that_only_mention_licenses(path):
    assert not load("collect_qt_notices").is_notice(PurePosixPath(path))


@pytest.mark.parametrize("path", [
    "LICENSES/LGPL-3.0-only.txt", "src/3rdparty/zlib/LICENSE", "src/3rdparty/pcre2/LICENCE.md",
    "src/3rdparty/pcre2/LICENSE-SLJIT", "src/3rdparty/freetype/BDF-LICENSE.txt", "src/3rdparty/libjpeg/COPYRIGHT.txt",
    "src/3rdparty/sha3/CC0_LICENSE", "sources/shiboken6/COPYING.libshiboken", "src/gui/opengl/LICENSE.KHRONOS.txt",
    "cmake/3rdparty/kwin/COPYING-CMAKE-SCRIPTS", "src/3rdparty/libjpeg/ijg-license.txt",
])
def test_qt_notice_filter_keeps_notice_texts(path):
    assert load("collect_qt_notices").is_notice(PurePosixPath(path))


def test_bundled_qt_notices_are_only_notice_texts():
    collect = load("collect_qt_notices")
    folder = ROOT / "licenses" / "Qt-6.12.0"
    files = [p.relative_to(folder) for p in folder.rglob("*") if p.is_file()]
    assert files
    assert [p for p in files if not collect.is_notice(PurePosixPath(*p.parts[1:]))] == []


def test_source_snapshot_leaves_out_internal_documents():
    export = load("export_source")
    assert {"AGENTS.md", "docs/progress.md"} <= export.INTERNAL_FILES
    assert "AGENTS.md" not in export.ALLOWED_FILES
    link = re.compile(r"\]\((?:\./)?(?:docs/)?(?:AGENTS|progress)\.md")
    assert not link.search((ROOT / "README.md").read_text(encoding="utf-8"))


def test_first_run_batch_file_is_utf8_without_bom_and_ascii_comments():
    data = (ROOT / "scripts" / "first_run.bat").read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8")
    lines = text.replace("\r\n", "\n").split("\n")
    assert lines[:2] == ["@echo off", "chcp 65001 >nul"]
    # cmd가 긴 UTF-8 주석 줄 뒤에서 줄 위치를 잘못 잡으므로 주석은 ASCII만
    assert all(line.isascii() for line in lines if line.lower().startswith("rem"))
    assert "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist" in text
    assert all(name in text for name in ("vcruntime140.dll", "msvcp140.dll", "NoseOut.exe"))
