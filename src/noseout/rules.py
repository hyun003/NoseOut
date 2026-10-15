"""무엇을 색인하고 무엇을 건너뛸지. 원본 Sources/DigUpKit/Rules.swift와 Platform/Portable/Files+Portable.swift를
옮기고, Windows에 맞는 것(시스템 폴더, OneDrive 온디맨드 파일)을 더했다. 문서·이미지·PDF(2단계), 음성·영상(4단계)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

SKIPPED_FOLDER_NAMES = {
    "node_modules", "bower_components", "venv", ".venv", "site-packages", "__pycache__", "DerivedData", "Pods",
    "Carthage", "$RECYCLE.BIN", "System Volume Information",
}
PROJECT_MARKERS = {
    ".git", "package.json", "Package.swift", "pyproject.toml", "setup.py", "requirements.txt", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "CMakeLists.txt",
}
PROJECT_BUNDLE_EXTENSIONS = {"xcodeproj", "xcworkspace", "sln"}
DOC_EXTENSIONS = {"txt", "text", "md", "markdown", "html", "htm", "docx"}  # rtf, doc, odt: 이후 단계
CODE_EXTENSIONS = {
    "py", "ipynb", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "m", "mm", "swift", "ts", "tsx", "cts", "js", "jsx",
    "mjs", "cjs", "go", "rs", "java", "kt", "kts", "scala", "rb", "php", "cs", "fs", "sh", "bash", "zsh", "fish",
    "ps1", "lua", "pl", "r", "jl", "dart", "sql", "vue", "svelte", "css", "scss", "sass", "less", "json", "jsonl",
    "yaml", "yml", "toml", "xml", "plist", "gradle", "cmake", "mk", "lock", "log", "csv", "tsv", "ini", "cfg",
    "conf", "proto", "graphql", "tf", "nix", "zig", "hs", "ex", "exs", "erl", "clj", "elm", "ml", "vim", "el", "asm",
    "metal", "glsl", "hlsl", "wgsl", "cu", "pyi", "pyc", "bat", "cmd", "psm1",
}
IMAGE_EXTENSIONS = {
    "jpg", "jpeg", "jpe", "jfif", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "avif",
}
AUDIO_EXTENSIONS = {"mp3", "m4a", "m4b", "aac", "wav", "wave", "flac", "ogg", "oga", "opus", "aiff", "aif", "wma",
                    "amr", "mka"}
VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "3gp", "mpg", "mpeg", "ogv"}
SCREENSHOT_PREFIXES = ("Screenshot", "Screen Shot", "CleanShot", "SCR-", "스크린샷", "화면 캡처")
SECRET_EXTENSIONS = {"pem", "key", "p12", "pfx", "keystore", "jks", "ppk", "kdbx", "gpg", "asc", "crt", "cer", "der",
                     "p8", "mobileprovision"}
SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env", "secret", "credential", ".netrc", ".npmrc",
                   ".pypirc", "htpasswd", ".htpasswd")
MIN_IMAGE_SIDE = 256

# 코드 색인 (6단계, 원본 Rules.swift의 "Code" 부분). 일반 색인은 코드를 계속 건너뛴다(CODE_EXTENSIONS).
CODE_SOURCE_EXTENSIONS = {
    "py", "pyi", "pyx", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm", "swift", "ts", "tsx", "mts",
    "cts", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "kts", "scala", "groovy", "rb", "php", "cs", "fs",
    "sh", "bash", "zsh", "fish", "ps1", "lua", "pl", "pm", "r", "jl", "dart", "sql", "vue", "svelte", "astro",
    "html", "htm", "css", "scss", "sass", "less", "gradle", "cmake", "mk", "proto", "graphql", "gql", "tf", "hcl",
    "nix", "zig", "hs", "ex", "exs", "erl", "clj", "cljs", "elm", "ml", "mli", "vim", "el", "lisp", "asm", "s",
    "metal", "glsl", "hlsl", "wgsl", "cu", "cuh", "ino", "v", "sv", "tex",
    "psm1", "bat", "cmd",   # Windows 스크립트 (원본에 없음)
}
CODE_TEXT_EXTENSIONS = {"md", "markdown", "rst", "adoc", "org", "txt", "yaml", "yml", "toml"}   # 저장소의 문서·설정
CODE_FILE_NAMES = {
    "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Justfile", "justfile", "Rakefile",
    "Gemfile", "Podfile", "Brewfile", "Procfile", "Vagrantfile", "Jenkinsfile",
}
# 남의 코드(vendor 등)와 빌드·실행 산출물. git이 무시하지 않는 저장소나 git 밖 폴더를 위해 이름으로도 막는다.
CODE_SKIPPED_FOLDER_NAMES = {
    "vendor", "Vendor", "vendored", "third_party", "third-party", "thirdparty", "3rdparty", "external", "extern",
    "deps", "build", "Build", "dist", "out", "target", "obj", "coverage", "htmlcov", "generated", "__generated__",
    "wandb", "mlruns", "lightning_logs",
}
MAX_CODE_BYTES = 1 << 20        # 이보다 큰 소스는 프로그램이 만든 것으로 본다 (원본과 같음)
MAX_NOTEBOOK_BYTES = 20 << 20   # 노트북 파일은 출력까지 들어 있어 따로 상한

# Windows 파일 특성: 숨김, 시스템, OneDrive 온디맨드(읽으면 내려받기가 일어남)
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
_OFFLINE = getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0x1000)
_RECALL_ON_OPEN = 0x40000
_RECALL_ON_DATA_ACCESS = 0x400000
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""


def is_secret(name: str) -> bool:
    lower = name.lower()
    return extension(lower) in SECRET_EXTENSIONS or lower.startswith(SECRET_PREFIXES)


def is_screenshot(name: str) -> bool:
    return name.startswith(SCREENSHOT_PREFIXES)


def classify(name: str) -> tuple[str, str]:
    """("index", kind) | ("skip", 이유) | ("ignore", "")."""
    if is_secret(name):
        return "skip", "key or password file"
    ext = extension(name)
    if ext in DOC_EXTENSIONS:
        return "index", "doc"
    if ext == "pdf":
        return "index", "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "index", "screenshot" if is_screenshot(name) else "image"
    if ext in CODE_EXTENSIONS:
        return "skip", "code or data file"
    if ext in AUDIO_EXTENSIONS:
        return "index", "audio"
    if ext in VIDEO_EXTENSIONS:
        return "index", "video"
    return "ignore", ""


def classify_code(name: str) -> tuple[str, str]:
    """코드 색인의 판정 (원본 Rules.classifyCode): 소스·저장소 문서·설정·노트북만. 데이터(json, csv, 잠금 파일, 로그),
    압축된 번들, 라이선스, 비밀 파일은 읽지 않는다."""
    if is_secret(name):
        return "skip", "key or password file"
    lower = name.lower()
    if lower.endswith((".min.js", ".min.mjs", ".min.css")):
        return "skip", "minified"
    if lower.startswith(("license", "licence", "copying", "notice")):
        return "skip", "license"
    ext = extension(name)
    if ext in CODE_SOURCE_EXTENSIONS or ext in CODE_TEXT_EXTENSIONS or ext == "ipynb" or name in CODE_FILE_NAMES \
            or name.startswith("Dockerfile"):
        return "index", "code"
    if ext in CODE_EXTENSIONS:
        return "skip", "data file"
    return "ignore", ""


def code_skip_reason(path: Path) -> str | None:
    """코드 색인이 들어가지 않는 폴더: 남의 코드·빌드 산출물, 서브모듈·워크트리(.git이 파일)."""
    if path.name in CODE_SKIPPED_FOLDER_NAMES:
        return "vendored or built code"
    if (path / ".git").is_file():
        return "submodule or worktree"
    return None


def is_mpeg_ts(path: str | Path) -> bool:
    """.ts가 TypeScript가 아니라 MPEG-TS 영상인지: 188(또는 192)바이트마다 0x47 (원본과 같음). 앞 600바이트만 읽는다."""
    try:
        with open(path, "rb") as f:
            data = f.read(600)
    except OSError:
        return False
    if len(data) < 400:
        return False
    return (data[0] == data[188] == data[376] == 0x47) or (data[4] == data[196] == data[388] == 0x47)


def attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def is_hidden(name: str, st: os.stat_result) -> bool:
    return name.startswith(".") or bool(attributes(st) & (_HIDDEN | _SYSTEM))


def is_placeholder(st: os.stat_result) -> bool:
    """OneDrive 등 클라우드에만 있는 파일: 읽지 않는다(원본이 iCloud 파일을 내려받지 않는 것과 같음)."""
    return bool(attributes(st) & (_OFFLINE | _RECALL_ON_OPEN | _RECALL_ON_DATA_ACCESS))


def is_link(st: os.stat_result) -> bool:
    """심볼릭 링크·정션: 따라가지 않는다(선택한 폴더 밖으로 나갈 수 있음)."""
    return stat.S_ISLNK(st.st_mode) or bool(attributes(st) & _REPARSE)


def folder_skip_reason(path: Path) -> str | None:
    name = path.name
    if name in SKIPPED_FOLDER_NAMES:
        return "developer folder"
    if name.endswith(".noindex"):
        return ".noindex folder"
    return None


def is_project_root(folder: Path) -> bool:
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(n in PROJECT_MARKERS for n in names) or any(extension(n) in PROJECT_BUNDLE_EXTENSIONS for n in names)


def forbidden_root_reason(root: Path) -> str | None:
    """색인 대상으로 고를 수 없는 폴더: 드라이브 전체, Windows·프로그램 폴더, 사용자 폴더 전체.
    (사용자 요구: 시스템 전체를 색인하지 않는다.)"""
    resolved = root.resolve()
    if resolved.parent == resolved:
        return "drive root"
    for folder in _system_folders():
        folder = Path(folder).resolve()
        if resolved == folder or folder in resolved.parents:
            return f"system folder ({folder})"
    home = Path.home().resolve()
    if resolved == home or resolved in home.parents:
        return "whole user folder"
    return None


def _system_folders() -> list[str]:
    system = [os.environ.get(v) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                                          "LOCALAPPDATA", "APPDATA")]
    system.append(str(Path.home() / "AppData"))  # Local·Roaming의 부모. 고르면 그 아래로 내려가게 된다
    return [folder for folder in system if folder]


# 이름 검색 전용 (드라이브 전체 이름 검색, 2026-10-14). AI 색인의 금지 규칙(forbidden_root_reason)과 따로 둔다:
# - 이름 검색은 드라이브 루트·사용자 폴더 전체를 고를 수 있다(파일 이름·경로만 읽고 내용은 읽지 않는다).
# - 그래도 시스템 폴더(Windows·프로그램·ProgramData·AppData 등, 캐시 대부분이 여기 있다), 드라이브 최상위의 Windows 관리 폴더,
#   숨김·시스템 특성, 링크·정션, 개발 폴더(folder_skip_reason), 비밀 파일은 이름 목록에도 넣지 않는다.
DRIVE_SYSTEM_FOLDER_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "recovery", "perflogs", "config.msi",
    "$winreagent", "$sysreset", "$windows.~bt", "$windows.~ws", "windows.old", "$getcurrent", "onedrivetemp",
    "msocache", "boot", "efi",
}


def is_drive_root(path: str | Path) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return resolved.parent == resolved


def system_folder_reason(path: Path) -> str | None:
    """시스템 폴더(그 아래 포함)인가. forbidden_root_reason에서 드라이브 루트·사용자 폴더 전체 조건만 뺀 것."""
    resolved = path.resolve()
    for folder in _system_folders():
        folder = Path(folder).resolve()
        if resolved == folder or folder in resolved.parents:
            return f"system folder ({folder})"
    return None


def name_root_problem(root: str | Path) -> str | None:
    """이름 검색 범위로 고를 수 없는 이유. 드라이브 루트·사용자 폴더 전체는 허용한다(AI 색인에는 여전히 금지)."""
    root = Path(root)
    try:
        st = os.lstat(root)
    except OSError:
        return f"not a folder: {root}"
    if is_link(st):
        return f"refusing to list {root}: it is a link or junction"
    if not root.is_dir():
        return f"not a folder: {root}"
    reason = system_folder_reason(root)
    return f"refusing to list {root}: {reason}" if reason else None


def forbidden_names() -> set[str]:
    """forbidden_root_reason이 거부할 수 있는 폴더의 이름들(casefold). 이름 목록(names.py)이 폴더마다 경로를 resolve하는
    비용을 줄이는 사전 필터다: 고른 폴더(이미 검사를 통과) 아래를 링크를 따라가지 않고 위에서부터 걸으면, 거부될 하위 폴더는
    시스템 폴더·사용자 폴더 자체뿐이고 그 이름은 이 집합에 있다. 이름이 맞으면 forbidden_root_reason 전체 검사를 한다."""
    names = set()
    for folder in [*_system_folders(), str(Path.home())]:
        for path in (Path(folder), Path(folder).resolve()):
            names.add(path.name.casefold())
    names |= {p.name.casefold() for p in Path.home().resolve().parents if p.name}
    return names


_CAMERA = ("img", "dsc", "pxl", "mvimg", "vid", "gopr", "dji", "photo", "whatsapp", "screenshot", "kakaotalk")


def looks_machine_named(stem: str) -> bool:
    name = stem.lower()
    if name.startswith(_CAMERA):
        return False
    if re.fullmatch(r"[0-9a-f]{8,}([-_][0-9a-f]{4,})*", name):
        return True
    return re.fullmatch(r"[a-z]{0,16}[-_. ]?\d{3,}([-_.]\d+)*", name) is not None
