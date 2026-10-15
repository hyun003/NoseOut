# 제3자 소프트웨어와 자료 출처

NoseOut 자체 코드의 MIT 허가는 외부 라이브러리·모델·코덱의 라이선스를 대체하지 않습니다.
이 소스 묶음에는 Python 환경, 모델, 엔진, 코덱 DLL과 EXE가 포함되지 않습니다.
외부 구성요소를 내려받아 결합하거나 실행 파일로 배포할 때는 각각의 조건을 따로 충족해야 합니다.

## 원본 DigUp

- 프로젝트: [ARahim3/DigUp](https://github.com/ARahim3/DigUp)
- 참조 리비전: `f9b8106ffb8308b4f48b76149d270f7c2e6b4918`
- Copyright (c) 2026 Abdur Rahim
- 허가: MIT. 원문은 [licenses/DigUp-MIT.txt](licenses/DigUp-MIT.txt)에 수정 없이 보존합니다.
- NoseOut은 비공식 Windows 재구현입니다. 원작자의 공식 제품이나 보증을 뜻하지 않습니다.
- `src/noseout/`의 여러 모듈은 원본 엔진 입력, 이미지 크기, 추출, 저장, 검색 점수, 제외 규칙,
  코드 검색 동작을 이식·참조했습니다. 각 파일의 출처 주석과 `docs/architecture.md`를 함께 봅니다.
- `tests/reference/photo.jpg`, `screen.png`, `speech.wav`, `vectors.json`은 위 리비전의 합성 기준 자료입니다.
  이미지·음성 기준 자료도 원본 MIT 고지의 대상입니다.
- 자체 Windows GUI·OCR·감시·독립 이름 검색 및 NoseOut 아이콘 등의 변경을 포함합니다.

## 직접 의존성과 외부 실행 도구

아래는 현재 설치 버전의 메타데이터와 공식 안내를 확인한 요약이며, 모든 전이 의존성의 전문을 대체하지 않습니다.

| 구성요소 | 라이선스 또는 조건 | 출처 |
|---|---|---|
| NumPy | BSD-3-Clause 및 동봉 구성별 MIT/0BSD/Zlib/CC0 등 | https://github.com/numpy/numpy |
| Pillow | MIT-CMU 및 동봉 라이브러리별 조건 | https://github.com/python-pillow/Pillow |
| requests, huggingface-hub | Apache-2.0 | https://github.com/psf/requests / https://github.com/huggingface/huggingface_hub |
| pytest, python-docx, WinRT Python | MIT 계열, 실제 구성별 고지 확인 | https://github.com/pytest-dev/pytest / https://github.com/python-openxml/python-docx / https://github.com/pywinrt/pywinrt |
| pypdfium2 | BSD-3-Clause / Apache-2.0 및 PDFium 등 의존성별 조건 | https://github.com/pypdfium2-team/pypdfium2 |
| PySide6-Essentials, shiboken6 | LGPL-3.0 / GPL 선택 조건, Qt 모듈별 라이선스 | https://www.qt.io/development/open-source-lgpl-obligations |
| pillow-heif 1.8.1 | Python 소스 BSD-3-Clause. 설치 wheel의 `LICENSES_bundled.txt`는 GPLv2를 명시 | https://github.com/bigcat88/pillow_heif/blob/v1.8.1/LICENSES_bundled.txt |
| libheif, libde265, x265 | 설치 wheel 고지 기준 LGPLv3, LGPLv3, GPLv2 | 위 pillow-heif 동봉 고지 |
| llama.cpp | MIT | https://github.com/ggml-org/llama.cpp |
| EmbeddingGemma 2 | Apache-2.0. 사용한 모델·GGUF 리비전의 LICENSE/NOTICE 확인 | https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2 / https://huggingface.co/ggml-org/embeddinggemma-2-GGUF |
| FFmpeg | 사용 빌드 기준 LGPLv3. 빌드 옵션·별도 코덱·소스 제공 조건 확인 | https://www.ffmpeg.org/legal.html |
| NVIDIA CUDA DLL | 해당 CUDA 버전의 재배포 목록과 라이선스 | https://docs.nvidia.com/cuda/eula/index.html |
| PyInstaller | GPL 및 부트로더 예외. PyInstaller 사용만으로 앱 전체가 GPL이 되는 것은 아님 | https://pyinstaller.org/en/stable/license.html |

## 상표와 모델 사용 정책

Gemma is a trademark of Google LLC. NoseOut is not affiliated with or endorsed by Google.
(Gemma는 Google LLC의 상표입니다. NoseOut은 Google과 제휴하지 않았으며 Google의 보증을 받지 않았습니다.)
원본 DigUp도 같은 상표 고지를 둡니다.

EmbeddingGemma 2 모델 카드는 배포와 사용이 [Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy)를
따라야 한다고 안내합니다. 모델이 들어 있는 NoseOut을 사용하거나 다시 배포할 때도 이 정책을 확인하세요.
모델 카드 원문은 `licenses/EmbeddingGemma-2/Google-model-README.md`에 보존합니다.

## HEIC와 EXE 배포의 남은 검토

HEIC를 읽기만 해도 설치된 wheel에서 x265 등 코덱이 제외되었다고 단정할 수 없습니다.
현재 개인용 EXE 제작 도구는 설치된 pillow-heif를 수집하므로 해당 바이너리의 조건이 관련됩니다.
자체 소스에 MIT를 붙이거나 앱 소스를 공개했다는 사실만으로 전체 결합 실행물의 배포 조건이 해결되지는 않습니다.
실제 DLL 구성·연결, GPL 버전 호환과 대응 소스 제공 범위를 검토하거나 적절한 대체 빌드를 준비해야 합니다.
현재 EXE는 공개 배포 조건을 완료한 패키지가 아닙니다.

2026-10-15 추가한 `--public-candidate`는 별도 구성입니다. 이 공개용 구성에는 pillow-heif·libheif·libde265·x265와
FFmpeg 실행물을 포함하지 않습니다. Microsoft 런타임 DLL과 선택적 Qt Mesa DLL도 제외합니다.
FFmpeg는 사용자가 공식 배포처에서 해시를 확인해 직접 받으며, Microsoft 런타임은 공식 설치본을 사용합니다.
공개용에 동봉한 Python 패키지별 고지는 `licenses/packages`, Qt 원문·제3자 고지는 `licenses/Qt-6.12.0`,
GNU 전문은 `licenses/GNU`에 있습니다. NVIDIA는 공식 CUDA 13.4.1 패키지와 DLL 해시를 대조한 EULA,
모델은 고정 GGUF 리비전의 README·원모델 리비전·Apache 전문을 보존합니다.
대응 소스 제공과 개별 권리는 [공개용 배포 조건](docs/binary-distribution.md)을 따릅니다.

## 시험 자료와 아이콘

- `tests/fixtures/*.png`: `scripts/make_fixtures.py`가 도형과 글자를 합성한 자료입니다. 글꼴 파일은 포함하지 않습니다.
- Windows `System.Speech`로 생성한 `meeting_en.wav`, `weather_en.wav`, `recipe_en.wav`, `train_ko.wav`의
  출력 재배포 조건은 확정하지 못했습니다. 공개용 소스 내보내기에서는 이 네 파일을 제외합니다.
  문장·생성 스크립트는 포함하며, 필요한 시험 자료는 각자의 환경에서 생성합니다.
  생성 결과를 재배포할 권리까지 보장하는 설명은 아닙니다.
- `src/noseout/assets/noseout.svg`: NoseOut용으로 작성한 원형 탐색 벡터 심볼이며 저장소의 MIT 허가에 포함됩니다.
  다른 서비스의 로고 파일을 가져오지 않았습니다. 상표 무충돌을 보증하지 않습니다.
- Everything 설치 파일·DB·역분석 산출물은 이 소스 묶음에 포함하지 않습니다. 독립 이름 검색은
  `os.scandir`, SQLite, Windows의 공식 MFT/USN API와 디렉터리 변경 감시를 사용합니다.
