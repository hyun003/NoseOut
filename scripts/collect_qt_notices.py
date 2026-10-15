"""Qt 6.12.0 공식 소스 묶음에서 라이선스·저작권 고지 원문만 골라 licenses/Qt-6.12.0 형식으로 꺼낸다.

실행: .venv/Scripts/python scripts/collect_qt_notices.py --sources <tar.xz 폴더> --output <비어 있는 새 폴더>

원 묶음의 해시는 licenses/corresponding-sources.json과 맞아야 한다. 이름에 license·copying이 들어간다는 이유만으로
고르지 않는다: licensewizard 예제, license_check.py, 라이선스 머리말 템플릿(*.in), SBOM·CI 설정, 시험용 가짜 고지는
고지 원문이 아니어서 뺀다(2026-10-15 공개 검토). 꺼낸 결과는 사람이 검토한 뒤 licenses/에 반영한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
# 고지 원문의 파일 이름: LICENSE, LICENSE.txt, LICENSE-SLJIT, LICENSE.KHRONOS.txt, COPYING, COPYING.libshiboken,
# COPYRIGHT(.txt), NOTICE, 그리고 BDF-LICENSE.txt·MIT_LICENSE.txt·ijg-license.txt처럼 이름 끝이 LICENSE·COPYING인 것.
NOTICE_NAME = re.compile(r"(?i)(?:(?:licen[cs]e|copying|copyright|notice)(?:[.-][A-Za-z0-9_.-]+)?"
                         r"|[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*[-_](?:licen[cs]e|copying)(?:\.(?:txt|md))?)")
# 이름이 맞아도 코드·설정·그림이면 고지 원문이 아니다(예: license_check.py, licenseheader.h.in).
NOT_NOTICE_SUFFIX = {".py", ".cmake", ".in", ".yaml", ".yml", ".json", ".png", ".ini", ".h", ".cpp", ".c",
                     ".qdoc", ".rst", ".pyproject", ".pro", ".qrc", ".sh", ".pl", ".js", ".html"}
# 시험·예제 자료 중 고지처럼 보이는 가짜 파일(실제 제3자 고지가 아님)
FAKE_NOTICES = {"tests/auto/cmake/RunCMake/Sbom/custom_licenses/LicenseRef-TestLicense.txt"}


def is_notice(relative: PurePosixPath) -> bool:
    """relative: 소스 묶음 최상위 폴더 아래 경로(예: src/3rdparty/zlib/LICENSE)."""
    if relative.as_posix() in FAKE_NOTICES or relative.suffix.lower() in NOT_NOTICE_SUFFIX:
        return False
    if "licensewizard" in relative.as_posix().lower():
        return False
    if relative.parts[0] == "LICENSES" and len(relative.parts) == 2 and relative.suffix == ".txt":
        return True                                   # REUSE 형식의 라이선스 전문 폴더
    return bool(NOTICE_NAME.fullmatch(relative.name))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("출력 폴더가 비어 있지 않습니다. 다른 폴더를 지정하세요.")
    components = json.loads((ROOT / "licenses" / "corresponding-sources.json").read_text(encoding="utf-8"))["components"]
    written = 0
    for component in components:
        archive = args.sources / component["file"]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != component["sha256"]:
            raise SystemExit(f"원 소스 해시가 다릅니다: {archive.name}")
        top = component["file"].removesuffix(".tar.xz")
        with tarfile.open(archive) as bundle:
            for member in bundle:
                path = PurePosixPath(member.name)
                if not member.isfile() or path.parts[0] != top or ".." in path.parts or len(path.parts) < 2:
                    continue
                if not is_notice(PurePosixPath(*path.parts[1:])):
                    continue
                target = output / Path(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.extractfile(member).read())
                written += 1
    print(f"QT_NOTICES: {written} files in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
