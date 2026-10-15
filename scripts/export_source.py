"""Git 기록·설치 환경·TTS 출력 없이 HEAD 커밋의 소스 스냅샷을 만든다. 공개 업로드는 하지 않는다."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOTS = {"src", "tests", "scripts", "docs", "evals", "results", "licenses"}
ALLOWED_FILES = {".gitignore", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
                 "pytest.ini", "requirements.txt", "requirements-build.txt"}
# 개발 일지·작업 규칙(PC 사양, 내부 진행 기록)은 저장소에만 두고 공개 스냅샷에는 넣지 않는다.
INTERNAL_FILES = {"AGENTS.md", "docs/progress.md"}
EXCLUDED_TTS = {f"tests/fixtures/{name}.wav" for name in
                ("meeting_en", "weather_en", "recipe_en", "train_ko")}
BLOCKED_SUFFIXES = {".exe", ".dll", ".gguf", ".sqlite", ".log", ".zip", ".pyc"}
CREDENTIAL = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{35,}|"
                        rb"sk-[A-Za-z0-9_-]{25,}|AKIA[A-Z0-9]{16})")
PERSONAL_PATH = re.compile(rb"(?i)[A-Z]:[\\/]+Users[\\/]+(?!Public|Default|me[\\/]|"
                           rb"test[\\/]|fixture[\\/])[^\\/\s\"<>]+")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "public-source")
    args = parser.parse_args()
    output = args.output.resolve()
    # 기존 작업을 지우지 않고 비어 있는 출력 폴더에만 만든다.
    if output.exists() and any(output.iterdir()):
        raise SystemExit("출력 폴더가 비어 있지 않습니다. 다른 폴더를 지정하세요.")
    # with_suffix는 'NoseOut-0.1.0-source'의 '.0-source'를 확장자로 보고 바꿔 버린다: 이름 끝에 붙인다.
    archive = output.with_name(output.name + ".zip")
    if archive.exists():
        raise SystemExit("같은 이름의 ZIP이 있습니다. 다른 출력 폴더를 지정하세요.")
    # 스냅샷은 커밋(HEAD)의 내용과 바이트 단위로 같아야 한다(core.autocrlf가 켜진 작업 트리는 줄바꿈이 다를 수 있음).
    # 그래서 작업 트리가 아니라 HEAD의 blob을 읽고, 커밋하지 않은 변경이 있으면 만들지 않는다.
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT).strip():
        raise SystemExit("커밋하지 않은 변경이 있습니다. 커밋한 뒤 내보내세요.")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", head], cwd=ROOT).decode("utf-8").split("\0")
    included: dict[str, bytes] = {}
    excluded: list[str] = []
    for entry in filter(None, tree):
        info, name = entry.split("\t", 1)
        mode, kind, blob = info.split()
        rel = Path(name)
        if (name in EXCLUDED_TTS or name in INTERNAL_FILES
                or (rel.parts[0] not in ALLOWED_ROOTS and name not in ALLOWED_FILES)):
            excluded.append(name)
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise SystemExit(f"일반 파일이 아닙니다: {name}")
        data = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=ROOT)
        if rel.suffix.lower() in BLOCKED_SUFFIXES or ".sqlite" in rel.name or len(data) > 20_000_000:
            raise SystemExit(f"공개 대상에 부적합한 파일을 발견했습니다: {name}")
        if rel.suffix.lower() == ".wav" and name != "tests/reference/speech.wav":
            raise SystemExit(f"출처를 확인하지 않은 음성 파일입니다: {name}")
        if b"\0" not in data and (CREDENTIAL.search(data) or PERSONAL_PATH.search(data)):
            raise SystemExit(f"개인 경로나 비밀정보 후보입니다. 확인하세요: {name}")
        included[name] = data
    required = {"LICENSE", "licenses/DigUp-MIT.txt", "THIRD_PARTY_NOTICES.md", "README.md"}
    if not required.issubset(included):
        raise SystemExit("필수 라이선스 고지가 없습니다.")
    # 공개 문서가 스냅샷에서 뺀 내부 문서를 링크하면 깨진 링크가 된다.
    internal_link = re.compile(rb"\]\((?:\.\./|\./)?(?:docs/)?(?:AGENTS|progress)\.md")
    linked = [name for name, data in included.items() if name.endswith(".md") and internal_link.search(data)]
    if linked:
        raise SystemExit(f"공개 문서가 내부 문서를 링크합니다: {linked}")
    output.mkdir(parents=True, exist_ok=True)
    for name, data in included.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {"base_commit": head, "includes_working_changes": False, "includes_git_history": False,
        "status": "source snapshot; binary distribution requires docs/binary-distribution.md and corresponding sources",
        "excluded_tracked_or_untracked_files": excluded,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in included.items()}}
    (output / "PUBLIC_SOURCE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # 원본과 복사본을 비교한 뒤 이 스냅샷만 담은 ZIP을 만든다.
    for name, data in included.items():
        if (output / name).read_bytes() != data:
            raise SystemExit(f"복사본 검증에 실패했습니다: {name}")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                zf.write(path, Path(output.name) / path.relative_to(output))
    print(f"SOURCE_READY: {len(included)} files; archive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
