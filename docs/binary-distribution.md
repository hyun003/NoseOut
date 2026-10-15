# NoseOut 공개용 패키지와 배포 조건

이 문서는 `--public-candidate`로 만든 패키지에 적용한다. 기존 개인용 `outputs/NoseOut`의 배포 허가 문서가 아니다.

## 구성과 첫 실행

- HEIC/HEIF 내용 분석은 제외한다. 이름 검색은 가능하다. 다른 지원 형식은 기존대로 처리한다.
- FFmpeg는 동봉하지 않는다. 음성·영상 분석을 사용하려면 `FFmpeg 다운로드.ps1`을 직접 실행한다(선택).
  고정한 공식 BtbN 파일을 내려받고 압축 파일·실행 파일 해시를 확인한다. 받은 프로그램은 자동 실행하지 않는다.
  단기 일일 릴리스 대신 2년 보관 정책의 2026-09-30 월말 LGPL 릴리스를 고정한다. 상류 보관 종료 전에 URL·해시를 갱신해야 한다.
  인터넷에서 받은 ZIP의 스크립트는 실행 정책에 막힐 수 있다. 그때는 그 폴더에서
  `powershell -ExecutionPolicy Bypass -File ".\FFmpeg 다운로드.ps1"`로 그 한 번만 실행한다(시스템 정책은 바꾸지 않음).
- Microsoft VC++ 런타임 DLL은 동봉하지 않는다. 사용 순서는 **VC++ 설치 → `NoseOut.exe`(또는 `처음 실행.bat`)**이다.
  1. Microsoft 공식 Visual C++ 2015–2022 x64 런타임(14.44 이상)을 설치한다.
  2. `NoseOut.exe`를 실행한다. EXE는 NumPy를 불러오기 전에(`runtime_setup.py`) System32의 런타임 다섯 개와 버전을 확인하고,
     `runtime-setup.json`에 적힌 NumPy 별칭(`_internal/numpy.libs/msvcp140-*.dll`, 원본은 `msvcp140.dll`만)을 정식
     설치본에서 복사한다. 앱 폴더 밖 경로나 다른 이름은 거부하고, 이미 있는 별칭의 내용이 달라도 덮어쓰지 않는다.
     앱 폴더에 쓸 수 없으면(Program Files 등) `%LOCALAPPDATA%\NoseOut\runtime\`에 복사하고 그 폴더를 DLL 검색 경로에
     추가한다. 런타임이 없거나 오래되면 Microsoft 안내 페이지를 담은 메시지를 보이고 종료하며 이유를
     `startup-error.log`에 남긴다. 시스템 DLL은 수정하지 않는다.
  3. `처음 실행.bat`은 런타임이 아예 없어 `python314.dll`부터 로드되지 않는 경우(앱 코드가 알릴 수 없음)를 위한 안내용이다.
     System32에 `vcruntime140.dll`·`vcruntime140_1.dll`·`msvcp140.dll`이 없으면 안내 페이지를 열고, 있으면 `NoseOut.exe`를 실행한다.
  4. `런타임 준비.ps1`은 같은 규칙(추가로 Authenticode 서명 확인)의 수동 대안으로 남긴다.
  한 번 실행한 로컬 폴더에는 Microsoft DLL이 추가되므로 그 폴더를 다시 공개 배포하지 않는다.
- 설정과 색인은 `%LOCALAPPDATA%\NoseOut`에 저장된다. 이전 이름의 `%LOCALAPPDATA%\DigUpWin`만 있으면 첫 실행 때
  복사·검증해 옮기고 옛 폴더는 남긴다.
- 서명하지 않은 EXE이므로 Windows SmartScreen 경고가 뜰 수 있다.
- Qt의 선택적 Mesa 소프트웨어 OpenGL DLL은 제외한다. Windows 플랫폼 플러그인과 시스템 그래픽 드라이버를 사용한다.

Microsoft 공식 설치 안내: <https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist>.
배포한 PowerShell 도구는 시스템 전체의 실행 정책이나 PATH를 바꾸지 않는다.

## 적용 라이선스

자체 코드와 원본 DigUp의 MIT 고지는 `LICENSE`, `licenses/DigUp-MIT.txt`에 있다. 이 허가는 모든 동봉 파일을 MIT로 바꾸지 않는다.
개별 원문과 저작권 고지는 `licenses/`, 패키지별 버전은 `licenses/runtime-packages.json`, 출처와 해시는 소스 목록에 보존한다.

- Qt/PySide6/shiboken6: 해당 공개 구성은 LGPLv3 경로로 사용한다. 라이브러리는 동적으로 연결한다.
- Python·llama.cpp·Pillow·NumPy·PDFium·WinRT 및 기타 라이브러리: 각각의 동봉 라이선스·고지를 따른다.
- certifi: MPL-2.0. 해당 Python 소스와 인증서 묶음을 대응 소스 ZIP에 함께 제공한다.
- EmbeddingGemma 2: Apache-2.0. 고정 GGUF 리비전의 README·원모델 리비전·파일 해시와 Apache 전문을 보존한다.
  모델 파일은 원 배포처의 양자화/변환본을 수정하지 않고 복사했다. `licenses/EmbeddingGemma-2`를 확인한다.
- CUDA 런타임 13.4.49와 cuBLAS 13.7.0.27: NVIDIA CUDA 13.4.1 공식 재배포 패키지의 DLL과 해시가 일치한다.
  EULA 원문은 `licenses/NVIDIA-CUDA-13.4.1`에 있다. CUDA는 MIT/GPL로 재허가하지 않는다.
- PyInstaller: 부트로더 예외를 포함하는 고지를 보존한다.

NVIDIA 구성요소는 NoseOut의 별도 llama.cpp 엔진이 NVIDIA GPU에서 추론하는 용도로 사용한다. NVIDIA 파일의
독립 상품 재배포, 고지 제거, 라이선스에서 금지한 역공학·변형·재허가는 허용되지 않는다(법률상 강행 권리는 별도).
NVIDIA의 보증·후원을 주장하지 않으며 수출 및 사용 제한을 포함한 동봉 EULA가 해당 구성요소에 적용된다.
이 제한은 NoseOut MIT 소스나 Qt 등 오픈소스의 수정·역공학 권리를 제한하는 조항이 아니다.

Gemma is a trademark of Google LLC. NoseOut is not affiliated with or endorsed by Google.
EmbeddingGemma 2 모델 카드는 배포·사용이 [Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy)를
따라야 한다고 안내한다. 원문은 `licenses/EmbeddingGemma-2/Google-model-README.md`에 있다.

## Qt 수정·교체와 대응 소스

사용자는 LGPL 라이브러리를 수정·교체하고 수정본을 디버깅하기 위해 필요한 역공학을 할 수 있다.
NoseOut은 Qt 교체를 막는 서명·무결성 검사를 두지 않는다. 앱을 종료하고 `_internal/PySide6`, `_internal/shiboken6`의
해당 DLL/PYD를 호환되는 x64·Qt 6.12 ABI 빌드로 교체할 수 있다. 다른 ABI로 변경하면 공개된 앱 소스로 다시 패키징할 수 있다.

실행 파일 ZIP과 **같은 공개 배포 위치에서** 같은 버전의 `NoseOut-<버전>-corresponding-sources.zip`도 제공해야 한다
(현재 묶음 이름은 `licenses/corresponding-sources.json`의 `archive`).
그 묶음에는 사용한 Qt 6.12.0의 qtbase·qtsvg·qtimageformats·qttranslations 및 pyside-setup 공식 소스, certifi 소스·인증서,
NoseOut 소스·빌드 도구·고정 의존성 목록과 파일별 해시가 들어간다. Qt 소스는 수정하지 않았다.
Qt 공식 소스의 Windows 빌드 안내와 pyside-setup의 빌드 안내에 따라 MSVC 2022·CMake/Ninja로 호환 라이브러리를 만들 수 있다.
Qt와 바인딩 소스의 내부 빌드 도구 및 제3자 원문은 원 소스 묶음에 보존되어 있다.

실행 파일 ZIP만 올리고 대응 소스를 생략하거나 최신 홈페이지 링크로 대체하지 않는다. 릴리스를 다시 묶거나
구성요소 버전을 바꾸면 대응 소스·해시·고지를 함께 갱신한다. 개인 데이터·환경·기존 Git 백업은 배포하지 않는다.

## 배포 확인

공개용 폴더에는 `LICENSE`, `THIRD_PARTY_NOTICES.md`, `licenses`, `배포조건.md`, `runtime-setup.json`, `처음 실행.bat`,
준비 도구와 파일 목록이 있어야 한다.
HEIC/GPL 코덱·FFmpeg 실행물·독립 Microsoft 런타임 DLL(`msvcp140*`·`vcruntime140*`·`concrt140*`·`vcomp140*`, 자동 준비로
생긴 별칭 포함)이 들어 있으면 이 공개 구성과 다른 패키지다.
`NoseOut.exe`를 한 번이라도 실행했거나 다운로드 도구로 파일을 추가한 폴더를 공개 ZIP 제작에 다시 사용하지 않는다.
`licenses/Qt-6.12.0`은 `scripts/collect_qt_notices.py`로 공식 소스 묶음에서 고지 원문만 꺼낸 것이다(예제·도구·설정 제외).

이 문서는 확인한 배포 구성의 라이선스 이행 자료이며 모든 국가의 상표·코덱 특허 또는 모델의 전체 권리에 대한 보증은 아니다.
실행 파일과 대응 소스를 실제로 공개하는 행위는 이번 준비 작업에 포함되지 않는다.
