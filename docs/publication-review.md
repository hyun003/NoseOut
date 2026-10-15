# NoseOut GitHub 공개 준비 점검

검토일: 2026-10-14. 기준: `1b28171`과 현재 작업 폴더의 EXE·이름·아이콘 변경.
대상은 소스 공개 준비이며, 공개 업로드나 EXE 배포 허가 완료를 뜻하지 않습니다.

## 이번에 보완한 내용

1. `LICENSE`에 MIT 전문과 원권리자 `Copyright (c) 2026 Abdur Rahim` 및 NoseOut 기여자 고지 추가.
   `licenses/DigUp-MIT.txt`에는 실제 사용한 리비전의 원문을 수정 없이 보존.
2. `THIRD_PARTY_NOTICES.md`에 이식·참조한 원본과 기준 시험 자료, 외부 라이브러리·모델·코덱의
   출처와 자체 MIT 허가가 적용되지 않는 범위를 명시.
3. README를 현재 GUI·빠른 이름 검색·드라이브 이름 범위·선택 범위 AI 분석·코드 검색과 한계에 맞게 갱신.
   비공식 재구현, 모델·엔진 별도 준비, 개인용 EXE의 공개 배포 조건 미완료도 명시.
4. 설치 목록에 실제 사용하는 `PySide6-Essentials==6.12.0`을 추가.
   PyInstaller는 별도 `requirements-build.txt`로 기록. 패키지를 새로 설치하거나 기존 환경을 변경하지 않음.
5. `scripts/export_source.py`로 Git 이력, 환경, 엔진·모델·색인, TTS WAV 네 개를 제외한 소스 묶음 준비.
   소스 파일별 해시를 기록하고 복사본을 비교함. 공개할 저장소는 기존 `.git`을 옮기지 않고 새로 구성할 수 있음.

## Git 기록·개인정보 검사

모든 현재 Git 참조에서 도달 가능한 커밋 **13개**, 고유 파일 blob **214개**를 검사했습니다.
삭제된 미참조 객체, 타 저장소, GitHub Releases/Actions 등 외부 산출물은 검사 범위가 아닙니다.

- 현재 추적 파일 내용에서는 실제 사용자 경로 패턴을 발견하지 않음.
- 과거 `AGENTS.md`의 두 버전과 `docs/model-validation.md`의 한 버전에서 개인 경로 확인.
  기존 저장소를 그대로 push하면 이 경로도 공개될 수 있음. 기존 이력은 수정하지 않았음.
- 주요 GitHub·OpenAI·AWS 키 모양 검사에서 실제 자격증명 후보를 발견하지 않음.
  이 제한된 패턴 검사는 비밀정보가 전혀 없다는 보증은 아님.
- 개발 시험에서 의도적으로 넣은 private-key 헤더와 `.env` 파일명은 실제 비밀키 발견으로 판정하지 않음.
- 모든 검사 대상 이력에서 `.venv`, `vendor`, `models`, `.tmp`, `.index`, 실제 테스트 데이터 경로,
  REA-Lab, EXE/DLL/GGUF/SQLite 파일을 발견하지 않음.

소스 묶음에는 Git 이력이 없으므로 과거 경로 문제를 우회할 수 있습니다. 기존 이력을 없앤 것은 아닙니다.
내보내기는 이름과 알려진 경로·토큰 패턴을 검사하고, 바이너리 제외 정책과 파일별 해시를 적용합니다.
코드에 포함된 모든 긴 문자열의 출처와 모든 제3자 권리까지 자동 감사하는 도구는 아닙니다.

## 미확정 1: 현재 EXE의 HEIC 코덱

현재 설치 `pillow-heif 1.8.1`은 Python 소스가 BSD-3-Clause지만, wheel의 `LICENSES_bundled.txt`는
바이너리 wheel이 GPLv2라고 명시합니다. libheif·libde265는 LGPLv3, x265는 GPLv2로 표시합니다.
현재 EXE 제작은 `--collect-all pillow_heif`를 사용하므로 이 문제는 실제 개인용 EXE 구성과 관련됩니다.

**확인된 것:** GPL 구성요소의 사용 고지를 BSD 하나로 처리하면 안 됩니다.
**아직 확정하지 못한 것:** 실제 배포 DLL의 구성, 결합 실행물의 GPL 적용 범위, Qt 등과의 정확한 버전 호환,
대응 소스 제공 범위. 단순히 전체 앱을 GPL로 선언해 모두 해결됐다고 볼 수 없습니다.

대응 선택지는 필요한 코덱만 포함한 적법한 빌드, 대체 라이브러리, 공개 EXE에서 HEIC 기능을 제외하는 구성,
또는 현 구성에 맞는 전체 배포 조건 이행입니다. 이번에는 기능 제거·재설치·바이너리 교체를 하지 않았습니다.

[사용 버전의 동봉 고지](https://github.com/bigcat88/pillow_heif/blob/v1.8.1/LICENSES_bundled.txt)

## 미확정 2: 다른 동봉 바이너리의 배포 준비

- Qt/PySide6 LGPL: 사용·라이선스 고지, 해당 라이브러리 소스 제공, 라이브러리 교체·재연결 권리 등 확인 필요.
- FFmpeg: 설치본에 LGPLv3 전문이 있지만, 공개할 정확한 빌드 옵션·소스·전이 코덱과 대응 소스 제공을 확인해야 함.
- PDFium·Pillow·NumPy·Python·WinRT 등: 설치 wheel마다 동봉 고지와 별도 라이브러리가 있으므로 실제 패키지 전체 고지 필요.
- CUDA DLL: 버전별 재배포 허용 파일 목록·조건 확인 필요.
- EmbeddingGemma **2**는 공식 모델 카드가 Apache-2.0으로 표시함. 이전 Gemma 모델의 조건을 그대로 적용하지 않고,
  사용한 GGUF 리비전과 동봉 모델의 LICENSE/NOTICE를 보존해야 함. 모델 파일은 소스 묶음에 넣지 않음.
- PyInstaller에는 부트로더 예외가 있으므로 PyInstaller 사용 자체가 앱 전체 GPL 공개 의무라는 판단은 하지 않음.

현재 EXE를 GitHub Releases에 올리기 전에는 위 검토가 남아 있습니다.
소스에 MIT와 제3자 고지를 추가한 것을 EXE의 모든 조건 완료로 설명하지 않습니다.

[Qt 공식 조건](https://www.qt.io/development/open-source-lgpl-obligations),
[FFmpeg 공식 안내](https://www.ffmpeg.org/legal.html),
[CUDA EULA](https://docs.nvidia.com/cuda/eula/index.html),
[EmbeddingGemma 2 공식 카드](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2),
[PyInstaller 예외](https://pyinstaller.org/en/stable/license.html)

## 미확정 3: Windows TTS 출력

`tests/fixtures/meeting_en.wav`, `weather_en.wav`, `recipe_en.wav`, `train_ko.wav`의 출력 재배포 허가를
공식 조건으로 확정하지 못했습니다. 합성 파일이라는 사실만으로 위법 또는 자유 재배포 가능을 단정하지 않습니다.
공개 소스 묶음에서는 네 파일을 제외하고 문장과 생성 스크립트를 포함합니다.
원본 `tests/reference/speech.wav`는 DigUp의 MIT 합성 기준 자료이며 고지와 함께 포함합니다.

원래 작업 폴더의 WAV와 과거 Git 기록은 보존합니다. `.gitignore` 추가는 이미 추적된 파일을 제거하지 않으므로
공개할 때는 내보낸 스냅샷을 쓰거나 별도의 이력 정리 작업이 필요합니다.

## 미확정 4: NoseOut 이름과 아이콘

이전 제품명 Locate의 이름 검색에서 기존 [Locate32 파일 검색 프로그램](https://sourceforge.net/p/locate32/wiki/Home/)과
[GNU locate](https://www.gnu.org/software/findutils/manual/html_node/find_html/Overview.html)를 확인했습니다.
이 사실은 이름이 유사한 프로그램의 존재를 뜻하며, Locate라는 이름의 사용이 곧 상표 침해라는 결론은 아닙니다.
국가·상품/서비스별 상표 등록과 혼동 가능성의 법적 검토는 수행하지 않았습니다.
이전 이름 Glean에 관해서는 다음 사실을 확인했습니다. [Glean](https://www.glean.com/)이라는 기업용 AI 검색 서비스가 이미 존재하는 것을 공식 사이트에서 확인했습니다. 분야도 검색·AI로 겹치므로 이름 구별성을 추가 검토할 필요가 있습니다. 상표 등록·혼동 가능성의 법적 판단까지 확정한 것은 아닙니다. 현재 이름은 사용자 요청으로 NoseOut으로 변경했습니다. NoseOut 이름의 상표 등록·충돌 여부는 아직 조사하지 않았습니다.

새 아이콘은 자체 작성 원형 탐색 SVG입니다. ChatGPT·Claude·Gemini의 로고 파일을 복제하지 않았습니다.
다만 전 세계 유사 로고와 상표의 무충돌을 완전히 조사한 것은 아닙니다.

## Everything 조사 결과의 이용

이번 Git 검사 범위에서 Everything 바이너리·실제 DB·REA 디컴파일 산출물은 발견하지 못했습니다.
현재 독립 이름 검색은 `os.scandir`, SQLite와 Windows 공식 MFT/USN API·디렉터리 변경 감시를 사용합니다.
초기 검토 뒤 MFT 관리자 도우미가 추가됐으며 아래 2026-10-15 갱신 기록에 반영했습니다.
기능 아이디어 참조만으로 이름 검색 기능을 삭제해야 한다는 근거는 발견하지 않았습니다.
모든 원본/디컴파일 코드와 행 단위로 비교한 법적 감정까지 수행한 것은 아닙니다.

## 결론과 다음 단계

소스 공개 준비에서 확인한 MIT 고지·문서·설치 목록 문제는 보완했습니다. 공개용 소스 묶음은
과거 개인 경로와 네 개의 TTS WAV를 포함하지 않도록 만들며, 외부 실행물·모델을 포함하지 않습니다.
GitHub 저장소 생성·업로드·push와 Git 기록 수정은 하지 않았습니다.

현재 EXE를 함께 공개하려면 HEIC GPL 구성과 다른 동봉 바이너리의 배포 조건을 먼저 정리해야 합니다.
이 문서는 실제 파일·공식 안내의 사전 검토 기록이며, 모든 국가의 법률·상표·특허에 대한 무침해 보증은 아닙니다.

2026-10-15 실제 배포 폴더를 대상으로 추가 검토했습니다. 최신 판정과 파일 근거는
[EXE 배포 라이선스 검토](exe-license-review-20261015.md)에 있습니다. HEIC의 x265 DLL 연결과 여러 고지 누락을
확인했으며, x265 4.3은 GPLv2 이후 버전 선택 문구가 있어 GPLv2-only 호환 불가로 단정하지 않습니다.
검토 기록을 보완한 것이며, 실행물 변경이나 배포 조건 이행을 완료한 것은 아닙니다.

## 2026-10-15 소스 스냅샷 갱신

설정창·색인 현황 추가 뒤 `scripts/export_source.py`로 소스 묶음을 다시 만들었다(`outputs/NoseOut-source.zip`, 저장소 안·Git 제외).
제외 정책(Git 이력·TTS WAV 네 개·모델·엔진·EXE·색인)과 개인 경로·비밀정보 패턴 검사는 그대로다. 개인용 EXE의 공개 배포 라이선스 검토
(HEIC·동봉 바이너리 등 위 미확정 항목)는 **여전히 완료하지 않았다**.

## 2026-10-15 소스 스냅샷 갱신 (이름 검색 MFT/USN)

이름 검색 MFT/USN 추가 뒤 소스 묶음을 다시 만들었다(`outputs/NoseOut-source-ntfs.zip`). 새 제3자 라이브러리는 없다(ctypes·표준 라이브러리,
Windows 공식 API). 관리자 권한 도우미(`ntfs_helper.py`)를 포함한다. 권한 요청 범위(볼륨 하나의 MFT 열거, 파일 내용·쓰기 없음)는 `ntfs_helper.py` 머리말에,
사용자가 누를 때만 UAC를 요청한다는 점은 README에 적었다. 개인용 EXE의 공개 배포 라이선스 검토 미확정 사항은 **그대로 남아 있다**.

## 2026-10-15 GitHub 준비 문서 갱신

README·제3자 고지의 NTFS 설명과 테스트 기록을 현재 개발 기록에 맞췄습니다. 공개 절차는
`docs/github-publication.md`에 정리했습니다. 기존 이력의 개인 경로를 포함하지 않는 소스 스냅샷을 사용하며,
현재 EXE의 공개 배포 조건 미확정 사항은 해소됐다고 주장하지 않습니다. 새 비밀정보 제외 규칙을 추가했지만
이 규칙만으로 기존 Git 이력이나 이미 추적된 자료가 제거되는 것은 아닙니다. 업로드·커밋은 하지 않았습니다.

## 2026-10-15 이력 정리와 별도 공개용 구성 보완

위 항목은 당시의 개인용 EXE와 정리 전 이력에 대한 기록입니다. 이후 사용자 요청으로 13개 커밋의 개인 경로와
시험 WAV 추적 이력을 정리하고, HEIC·FFmpeg·Microsoft 런타임 DLL을 동봉하지 않는 별도 공개용 구성을 만들었습니다.
앱·Qt·Python·모델·CUDA 등의 고지와 정확한 Qt 대응 소스 자료를 추가했습니다. 기존 개인용 EXE는 보존했습니다.
최신 결과와 검증 한계는 [이력·라이선스 보완 결과](history-and-license-cleanup-20261015.md),
실제 배포 구성은 [공개용 배포 조건](binary-distribution.md)을 따릅니다.
