# NoseOut

Windows 11용 로컬 파일 검색 프로그램입니다. 파일 이름·경로는 AI 분석과 별도로 빠르게 검색하고,
문서·사진·PDF·음성·영상은 선택한 범위에서 분석해 내용으로 찾습니다.

[DigUp](https://github.com/ARahim3/DigUp)을 참조·이식한 **비공식 Windows 재구현**입니다.
원작자 및 Glean의 기업용 AI 서비스·Everything·Locate32 등 다른 검색 서비스와 무관합니다.

## 요구사항

- Windows 11 x64
- 공개용 EXE: [Microsoft Visual C++ 2015–2022 재배포 가능 패키지 X64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist) 14.44 이상
- **AI 내용 검색은 NVIDIA GPU와 CUDA 13을 지원하는 드라이버에서 빠르게 동작합니다.**
  - GPU: Turing(GeForce RTX 20·GTX 16 시리즈) 이상. CUDA 13은 Maxwell·Pascal·Volta 지원을 제거했습니다.
  - 드라이버: R580(580.xx) 이상 필수. 동봉한 CUDA 런타임 13.4·cuBLAS 13.7은 NVIDIA의 CUDA 13.x 소수 버전
    호환 규칙상 R580 이상에서 동작합니다. CUDA 13.4의 기준 드라이버 분기는 R615이므로 R615 이상(최신 드라이버)을 권장합니다.
    실제로 확인한 드라이버는 개발 PC의 617.42 하나입니다.
  - NVIDIA GPU가 없으면 엔진이 CPU로 동작합니다. 검색어 처리는 빠르지만 문서·사진·영상 분석이 매우 느립니다
    (개발 PC 측정: 사진 한 장 약 2초, 큰 이미지 약 14초). NVIDIA 드라이버가 없는 환경(Windows 샌드박스)에서
    CPU로 동작을 확인했습니다. 측정은 고성능 데스크톱 CPU 기준이며 노트북에서는 더 느릴 수 있습니다.
- 파일 이름·경로 검색은 GPU와 무관하게 동작합니다.

## 기능

- 폴더·드라이브에서 파일 이름과 경로 등록, 등록 중 검색, 일시정지·이어하기·중지
- AI 분석은 고른 폴더와 개별 파일에 한정, 이름 검색과 내용 검색을 함께 표시
- 문서·PDF·그림·스크린샷 OCR·음성·영상 검색, `code:` 접두사의 별도 코드 검색
- 트레이와 전역 단축키, 폴더 변경 감시, 기존 색인 버전 검사
- 설정창(일반·검색 범위·화면·색인 현황): 단축키 변경, 로그인 시 실행(EXE), 테마, 창 위치 복원, 색인·작업 상태
- 파일 내용은 로컬 엔진의 `127.0.0.1`로만 전달. 외부 검색 서비스로 업로드하지 않음

이름 목록도 색인을 사용합니다. 로컬 NTFS 드라이브는 처음 등록 뒤 USN 변경 기록(일반 권한)으로 바뀐 폴더만 다시 읽고,
'빠른 NTFS'를 고르면 처음 등록을 관리자 도우미의 MFT 목록으로 합니다(사용자가 누를 때만 UAC). 그 밖에는 폴더 순회입니다.
Everything 수준의 속도는 측정하지 않았고 주장하지 않습니다.
내용 분석은 이름 등록보다 오래 걸릴 수 있습니다.

## Releases에서 받아 실행하기

소스를 빌드하지 않고 쓰려면 GitHub Releases의 공개용 패키지를 받습니다.

1. 같은 릴리스에서 `NoseOut-<버전>-win-x64.zip`(실행 파일, 예: `NoseOut-0.1.0-win-x64.zip`)을 받습니다.
   함께 올린 `NoseOut-<버전>-corresponding-sources.zip`은 Qt(LGPL) 등의 대응 소스이며, 실행에는 필요하지 않습니다.
2. [Microsoft Visual C++ 2015–2022 재배포 가능 패키지 X64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)
   (14.44 이상)를 Microsoft 공식 페이지에서 설치합니다.
3. ZIP을 **폴더 전체** 압축 해제합니다. `NoseOut.exe`만 꺼내지 말고 `_internal`, `models`, `vendor`를 같은 폴더에 둡니다.
4. `NoseOut.exe`를 실행합니다. 런타임을 설치했는지 확실하지 않으면 `처음 실행.bat`을 실행합니다.
   런타임이 없으면 안내 페이지를 열고, 있으면 NoseOut을 시작합니다.
   앱 내부에 필요한 런타임 파일 준비는 첫 실행 때 자동으로 합니다.
5. 서명하지 않은 EXE라서 Windows SmartScreen 경고(“Windows의 PC 보호”)가 뜰 수 있습니다.
   받은 위치와 해시를 확인한 뒤 '추가 정보' → '실행'으로 시작합니다.

선택 사항과 제한:

- 음성·영상 분석을 쓰려면 폴더의 `FFmpeg 다운로드.ps1`로 공식 FFmpeg를 받습니다. 인터넷에서 받은 스크립트라
  실행 정책에 막히면 그 폴더에서 다음처럼 실행합니다(시스템 실행 정책은 바꾸지 않음).
  ```powershell
  powershell -ExecutionPolicy Bypass -File ".\FFmpeg 다운로드.ps1"
  ```
- HEIC/HEIF 사진의 내용 분석은 공개용 패키지에서 지원하지 않습니다. 이름 검색은 됩니다.
- `런타임 준비.ps1`은 자동 준비가 실패할 때 쓰는 수동 대안입니다(위와 같은 방식으로 실행).
- 배포 조건과 고지는 패키지 안의 `배포조건.md`, `THIRD_PARTY_NOTICES.md`, `licenses/`에 있습니다.

## 실행 환경과 설치

확인한 환경은 Windows 11, Python 3.14, RTX 4070 SUPER입니다. 다른 PC·차가운 디스크·네트워크와
외장 드라이브의 속도·호환성은 보장하지 않습니다. 모델과 엔진 바이너리는 이 소스 묶음에 없습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

- 모델: EmbeddingGemma 2 Q8_0와 mmproj, `models/`에 준비합니다.
- 엔진: llama.cpp b11461의 Windows 바이너리를 `vendor/llama/`에 준비합니다.
- 음성·영상: FFmpeg와 FFprobe를 `vendor/ffmpeg/<버전>/bin/`에 준비합니다.
- 리비전·다운로드 경로·해시는 [모델 검증 기록](docs/model-validation.md)의 재현 방법을 참고합니다.
- 설치된 HEIC 라이브러리 wheel에는 GPL 구성요소가 있습니다. [제3자 고지](THIRD_PARTY_NOTICES.md)를 읽어 주세요.

## 앱 실행

저장소 폴더에서 실행합니다.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m noseout app
```

검색 창은 `Ctrl+Shift+Space`, 숨기기는 `Esc`, 종료는 트레이 또는 검색 창 메뉴의 '종료'입니다.
단축키·시작 방식·테마는 검색 창의 '설정…'에서 바꿉니다(적용·취소). 검색 범위 탭의 변경은 누르는 즉시 저장됩니다.
Windows 로그인 시 실행은 `NoseOut.exe`로 실행했을 때만 켤 수 있고, 현재 사용자 계정의 시작 항목만 씁니다.
처음 실행할 때 이름 검색 범위와 AI 분석 범위를 구분해 선택합니다.
설정·색인은 `%LOCALAPPDATA%\NoseOut`에 저장합니다(환경 변수 `NOSEOUT_HOME`으로 바꿀 수 있음).
이전 이름의 `%LOCALAPPDATA%\DigUpWin`만 있으면 첫 실행 때 앱이 꺼져 있는 것을 확인하고 새 폴더로 복사·검증해 옮깁니다.
옛 폴더는 지우지 않고 남기며, 복사에 실패하면 옛 폴더를 계속 씁니다. 옛 환경 변수 `DIGUPWIN_HOME`도 경고와 함께 받습니다.

## 개인용 EXE 제작

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_exe.py
```

결과는 `.tmp/exe-dist/NoseOut/NoseOut.exe`입니다(`--output outputs`면 `outputs/NoseOut/NoseOut.exe`). Python 런타임을 포함하므로 EXE 실행 시 Python 명령이
필요하지 않습니다. `_internal`, `models`, `vendor`를 포함한 폴더 전체를 보관해야 합니다.
**위 기본 명령(`--public-candidate` 없이)으로 만든 개인용 빌드는 배포용이 아닙니다.** HEIC 코덱·FFmpeg·Microsoft
런타임 DLL을 함께 수집하며, 그 구성의 공개 배포 조건은 검토를 완료하지 않았습니다. 공개 배포에는 아래 공개용 패키지를 씁니다.

## 공개용 패키지

개인용과 다른 새 출력 경로에 `--public-candidate`로 만든다. 기존 NoseOut 폴더를 덮어쓰지 않는다.

```powershell
.\.venv\Scripts\python.exe scripts\build_exe.py --public-candidate --output outputs\public-build
```

공개용에서는 HEIC/HEIF 내용 분석과 선택적 Mesa 소프트웨어 OpenGL DLL을 제외한다. FFmpeg와 Microsoft 런타임 DLL은
동봉하지 않는다. 사용 순서는 "VC++ 런타임 설치 → `NoseOut.exe`(또는 `처음 실행.bat`)"이다. NumPy가 찾는 앱 내부 런타임
별칭은 EXE가 시작할 때 설치된 런타임에서 자동으로 준비하고(앱 폴더에 쓸 수 없으면 사용자 설정 폴더의 `runtime\`),
`런타임 준비.ps1`은 수동 대안으로 남긴다. 음성·영상 분석은 `FFmpeg 다운로드.ps1`로 공식 파일을 별도로 받으면 사용할 수 있다.
실행 파일과 함께 Qt 등의 **정확한 대응 소스 ZIP도 같은 배포 위치에 제공해야 한다.**
고지·교체 권리·외부 구성요소 조건은 [공개용 배포 조건](docs/binary-distribution.md)에 있다.
제작 명령의 성공만으로 소스 제공까지 완료되었다고 보지 않는다.

## 테스트

```powershell
.\.venv\Scripts\python.exe scripts\make_fixtures.py
.\.venv\Scripts\python.exe scripts\make_test_data.py
.\.venv\Scripts\python.exe -m pytest -q
```

공개용 소스에는 Windows TTS로 만든 시험 WAV 네 개를 넣지 않습니다. 시험 음성은 각자의 환경에서
생성하며, 출력 재배포 허가까지 보장하지 않습니다. 원본 MIT 합성 기준 자료는 고지와 함께 포함합니다.
모델·엔진이 없으면 관련 테스트는 건너뛸 수 있습니다. 간헐적으로 실패하던 동시 색인 시험(이름 검색 3번이 끝나기 전에
AI 색인이 먼저 끝나던 고정 대기)은 2026-10-15에 테스트가 직접 여닫는 문(threading.Event)으로 바꿨습니다.
공개 준비 수정 후 개발 환경 전체 실행은 429개 통과였습니다(최신 수치는 아래 공개 안내 문서).
자세한 결과와 검증 범위는 [보완 기록](docs/history-and-license-cleanup-20261015.md)에 있습니다.

## 알려진 한계

- rtf/doc/odt 내용 분석 미지원. HEIC는 외부 코덱의 지원과 라이선스 조건에 의존합니다.
- 짧은 한국어 음성을 영어·다른 표현으로 찾는 품질에는 한계가 있고, 별도 ASR 적용은 보류했습니다.
- NTFS는 재실행 때 저장된 목록으로 먼저 검색하고 USN 변경을 뒤에서 반영합니다. 최초 등록·저널 유실 후
  재등록이나 NTFS가 아닌 드라이브의 폴더 순회는 범위 크기에 따라 시간이 걸립니다.
- MFT 목록에는 하드링크의 다른 이름이 빠질 수 있습니다. 설정의 '폴더 순회로 전체 확인'으로 보완합니다.
- 사용자 권한으로 열 수 없는 폴더의 내부는 등록하지 않습니다. 해당 폴더는 상태 미확인으로 표시될 수 있습니다.
- 일반 폴더 감시의 알림 유실 후 전체 확인은 최대 10분 늦을 수 있습니다. NTFS USN 경로는 별도로 변경을 반영합니다.
- 외장 드라이브를 감시 중이면 안전한 제거가 거부될 수 있어 감시를 먼저 꺼야 합니다.
- 지원 편집기가 없으면 코드 줄 이동 대신 메모장과 줄 번호 안내를 사용합니다.

## 라이선스와 공개 준비

자체 코드·아이콘은 [MIT](LICENSE), 원본의 고지는 [DigUp MIT 전문](licenses/DigUp-MIT.txt)에 보존합니다.
외부 라이브러리·모델·코덱은 각자의 조건을 따릅니다. MIT가 모든 결합 실행물에 적용된다는 뜻은 아닙니다.

Gemma is a trademark of Google LLC. NoseOut is not affiliated with or endorsed by Google.
EmbeddingGemma 2 모델의 사용과 재배포는 모델 카드가 안내하는
[Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy)를 따릅니다.

[공개 준비 점검](docs/publication-review.md), [제3자 고지](THIRD_PARTY_NOTICES.md),
[구조](docs/architecture.md)를 참고하세요.
Git 기록과 시험 음성을 제외한 소스 묶음은 `scripts/export_source.py`로 만들 수 있습니다.
GitHub에 올릴 범위와 공개 전 절차는 [GitHub 공개 안내](docs/github-publication.md)를 참고하세요.
