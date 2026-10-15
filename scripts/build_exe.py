"""현재 PC용 폴더형 EXE 제작. 모델·엔진을 옆에 두고 사용자 설정은 복사하지 않는다.

실행: .venv/Scripts/python scripts/build_exe.py --output <결과 폴더>
PyInstaller는 .venv에 별도로 설치한다. --public-candidate는 HEIC 코덱을 제외하고 고지를 동봉한다.
후보 제작은 배포 조건 검증 완료를 뜻하지 않는다.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "exe-dist")
    parser.add_argument("--public-candidate", action="store_true",
                        help="HEIC를 제외하고 라이선스 고지를 동봉한 공개 후보를 별도 제작")
    args = parser.parse_args()
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("PyInstaller를 프로젝트 .venv에 먼저 설치하세요.")
    output = args.output.resolve()
    # PyInstaller는 기존 onedir를 먼저 지우므로 실행 중이거나 보관 중인 패키지를 덮어쓰지 않는다.
    if (output / "NoseOut").exists():
        raise SystemExit("이미 NoseOut 폴더가 있는 출력 경로입니다. 비어 있는 새 경로를 지정하세요.")
    output.mkdir(parents=True, exist_ok=True)
    profile = "public" if args.public_candidate else "native"
    work = ROOT / ".tmp" / f"exe-build-{profile}"
    specs = ROOT / ".tmp" / f"exe-spec-{profile}"
    specs.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    assets = ROOT / "src" / "noseout" / "assets"
    # 창·트레이와 동일한 SVG를 ICO의 여러 크기로 렌더링한다.
    icon = QImage(256, 256, QImage.Format.Format_ARGB32)
    icon.fill(Qt.GlobalColor.transparent)
    painter = QPainter(icon)
    QSvgRenderer(str(assets / "noseout.svg")).render(painter)
    painter.end()
    png_path = specs / "noseout.png"
    icon.save(str(png_path))
    icon_path = specs / "noseout.ico"
    with Image.open(png_path) as source:
        source.save(icon_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
               "--noupx", "--name", "NoseOut", "--icon", str(icon_path),
               "--paths", str(ROOT / "src"), "--add-data", f"{assets};noseout/assets", "--distpath", str(output),
               "--workpath", str(work), "--specpath", str(specs),
               "--collect-all", "winrt", "--collect-all", "pypdfium2",
               "--exclude-module", "pytest", "--exclude-module", "PySide6.QtQml",
               "--exclude-module", "PySide6.QtQuick", str(ROOT / "scripts" / "run_app.py")]
    if args.public_candidate:
        # 선택 import는 ImportError를 처리한다. GPL 코덱이 달린 wheel을 수집하지 않는다.
        command[3:3] = ["--exclude-module", "pillow_heif", "--exclude-module", "_pillow_heif"]
    else:
        command[3:3] = ["--collect-all", "pillow_heif"]
    # 제작 도구를 띄운 앱의 PATH에 Poppler 등 다른 ICU DLL이 있어도 섞이지 않게 한다.
    # Windows 11의 Qt는 System32/icuuc.dll을 사용한다.
    build_env = os.environ.copy()
    build_env["PATH"] = os.pathsep.join([
        str(Path(sys.executable).parent),
        str(Path(os.environ["SystemRoot"]) / "System32"),
        os.environ["SystemRoot"],
        str(ROOT / ".venv" / "Lib" / "site-packages" / "PySide6"),
        str(ROOT / ".venv" / "Lib" / "site-packages" / "shiboken6"),
    ])
    subprocess.run(command, cwd=ROOT, check=True, env=build_env)
    bundle = output / "NoseOut"
    # onedir의 _internal/noseout/engine.py 기준 ROOT는 EXE가 놓인 폴더다.
    models = bundle / "models"
    models.mkdir(exist_ok=True)
    for name in ("embeddinggemma-2-Q8_0.gguf", "mmproj-embeddinggemma-2-Q8_0.gguf"):
        shutil.copy2(ROOT / "models" / name, models / name)
    llama = bundle / "vendor" / "llama"
    llama.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / "vendor" / "llama").iterdir():
        if path.is_file() and (path.suffix == ".dll" or path.name == "llama-server.exe"
                               or path.name.startswith("LICENSE")):
            shutil.copy2(path, llama / path.name)
    for source_bin in (() if args.public_candidate else (ROOT / "vendor" / "ffmpeg").glob("*/bin")):
        target = bundle / "vendor" / "ffmpeg" / source_bin.parent.name
        (target / "bin").mkdir(parents=True, exist_ok=True)
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            shutil.copy2(source_bin / name, target / "bin" / name)
        for path in source_bin.parent.glob("LICENSE*"):
            shutil.copy2(path, target / path.name)
        break
    # 소스 저장소의 고지도 실제 배포 폴더로 복사해야 수신자에게 전달된다.
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, bundle / name)
    shutil.copytree(ROOT / "licenses", bundle / "licenses")
    if args.public_candidate:
        # Qt가 선택적으로 쓰는 별도 Mesa DLL은 이 후보에서 배포하지 않는다.
        # Windows 플랫폼 플러그인과 시스템 그래픽 드라이버를 사용한다.
        software_gl = bundle / "_internal" / "PySide6" / "opengl32sw.dll"
        software_gl.unlink(missing_ok=True)
        aliases = []
        for path in bundle.rglob("*.dll"):
            lower = path.name.lower()
            if lower.startswith(("msvcp140", "vcruntime140", "concrt140", "vcomp140")):
                if lower.startswith("msvcp140-"):
                    aliases.append({"system_name": "msvcp140.dll",
                                    "relative_path": path.relative_to(bundle).as_posix()})
                # 재배포 사용권을 전제하지 않고 사용자가 설치한 Microsoft 런타임을 사용한다.
                path.unlink()
        (bundle / "runtime-setup.json").write_text(json.dumps(
            {"requires": "Microsoft Visual C++ 2015-2022 x64 (14.44 or newer)",
             "local_aliases": aliases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        forbidden = ("libx265", "libheif", "libde265", "_pillow_heif", "pillow_heif")
        if any(any(part in path.name.lower() for part in forbidden) for path in bundle.rglob("*")):
            raise SystemExit("공개 후보에 제외한 HEIC 구성요소가 남아 있습니다.")
        shutil.copy2(ROOT / "docs" / "binary-distribution.md", bundle / "배포조건.md")
        shutil.copy2(ROOT / "scripts" / "download_ffmpeg.ps1", bundle / "FFmpeg 다운로드.ps1")
        shutil.copy2(ROOT / "licenses" / "ffmpeg-download.json", bundle / "ffmpeg-download.json")
        # 수동 대안. NoseOut.exe가 시작할 때 같은 규칙으로 별칭을 자동 준비한다(runtime_setup.py).
        shutil.copy2(ROOT / "scripts" / "prepare_runtime.ps1", bundle / "런타임 준비.ps1")
        # 런타임이 아예 없으면 python314.dll부터 로드되지 않아 앱이 안내할 수 없으므로 bat로 안내한다.
        # cmd는 BOM을 명령으로 읽고 LF만 있는 줄에서 레이블을 놓치므로 BOM 없는 UTF-8·CRLF를 보장한다.
        first_run = (ROOT / "scripts" / "first_run.bat").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        if first_run.startswith(b"\xef\xbb\xbf") or b"chcp 65001" not in first_run:
            raise SystemExit("처음 실행.bat은 BOM 없는 UTF-8이고 chcp 65001을 써야 합니다.")
        first_run.decode("utf-8")
        (bundle / "처음 실행.bat").write_bytes(first_run)
    (bundle / "사용법.txt").write_text(
        ("NoseOut.exe를 더블클릭하면 실행됩니다.\n" if not args.public_candidate else
         "1. Microsoft Visual C++ 2015-2022 x64 런타임(14.44 이상)을 공식 페이지에서 설치하세요.\n"
         "   https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist\n"
         "2. NoseOut.exe(또는 처음 실행.bat)를 실행하세요. 필요한 앱 내부 준비는 자동으로 합니다.\n"
         "   런타임이 없으면 처음 실행.bat이 안내 페이지를 엽니다.\n"
         "   자동 준비가 안 될 때만 수동 대안으로 런타임 준비.ps1을 쓸 수 있습니다\n"
         "   (powershell -ExecutionPolicy Bypass -File \".\\런타임 준비.ps1\").\n")
        + "EXE만 옮기지 말고 _internal, models, vendor를 포함한 폴더 전체를 유지하세요.\n"
        "설정·색인은 %LOCALAPPDATA%\\NoseOut에 저장됩니다(예전 DigUpWin 폴더가 있으면 첫 실행 때 복사하고 원본은 남깁니다).\n"
        "검색: Ctrl+Shift+Space / 숨기기: Esc / 종료: 트레이 메뉴의 종료\n"
        + ("AI 내용 검색은 NVIDIA GPU(Turing 이상)와 CUDA 13 지원 드라이버(R580 이상, 권장 R615 이상)에서 빠르게 동작합니다.\n"
           "   GPU가 없으면 CPU로 동작하지만 문서·사진·영상 분석이 매우 느립니다. 이름 검색은 GPU와 무관합니다.\n"
           "   NVIDIA 드라이버가 없는 환경(Windows 샌드박스)에서 CPU로 동작을 확인했습니다.\n"
           "   측정은 고성능 데스크톱 CPU 기준이며 노트북에서는 더 느릴 수 있습니다.\n"
           "HEIC/HEIF 사진의 내용 분석은 지원하지 않습니다. 이름 검색은 가능합니다.\n"
           "음성·영상 분석(선택): FFmpeg 다운로드.ps1로 공식 파일을 별도로 받으세요. 실행 정책에 막히면\n"
           "   powershell -ExecutionPolicy Bypass -File \".\\FFmpeg 다운로드.ps1\"\n"
           "서명하지 않은 EXE라 Windows SmartScreen 경고가 뜰 수 있습니다.\n"
           "배포조건.md와 licenses의 고지 및 별도 대응 소스 묶음을 함께 확인하세요.\n"
           "배포 조건은 배포조건.md를 참고하세요.\n"
           if args.public_candidate else
           "이 패키지는 현재 PC 개인용이며, 공개 배포 라이선스 검토를 완료한 설치 파일이 아닙니다.\n"),
        encoding="utf-8")
    label = "Public candidate created" if args.public_candidate else "EXE ready"
    print(f"{label}: {bundle / 'NoseOut.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
