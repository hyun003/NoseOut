# 아키텍처

작성: 2026-10-13 · 기준 원본: ARahim3/DigUp `f9b8106` (2026-10-12)

## 1. 원본 DigUp 분석 요약

원본은 macOS용 Swift 앱(약 19,000줄)이다. 들여다볼 만한 점은 **원본 저장소에 이미 Windows·Linux 경로가 일부 있다**는 것이다.

| 구성 | 원본 위치 | 내용 |
|---|---|---|
| 임베딩 | `Sources/LlamaRuntime/LlamaEmbedder.swift` | llama.cpp(b11461, 1줄 패치)를 라이브러리로 링크. mean pooling, L2 정규화. 텍스트 `<bos> text <eos>`, 미디어 `<bos><\|image> soft tokens <image\|><eos>`. 한 입력 = 한 `llama_process` 호출 |
| 이미지 크기 | `Sources/LlamaRuntime/ImageSizing.swift` | 기준(transformers Gemma 4 processor)과 같은 픽셀을 보도록 직접 리사이즈(Pillow bicubic을 그대로 옮김). mtmd에는 `image_min_tokens=1`로 그대로 넘김 |
| 모델 파일 | `Sources/DigUpKit/ModelFiles.swift` | `ggml-org/embeddinggemma-2-GGUF` @ `bfcd298`, Q8_0 텍스트(310 MB) + mmproj(555 MB). **F16 금지**(활성값 overflow) |
| 프롬프트 | `Sources/DigUpKit/Model.swift` | 검색어 `task: search result \| query: …`, 코드 `task: code retrieval \| query: …`, 문서 `title: … \| text: …` |
| 색인 옵션 | `Model.swift` `IndexOptions` | 사진 140 토큰, 스크린샷·PDF 페이지 280, 영상 프레임 70(3초 간격, 중복 제거), 음성 30초 창/25초 hop, 텍스트 1800자 청크/200자 겹침 |
| 저장 | `Sources/DigUpKit/IndexStore.swift` | SQLite 파일 1개: `files`, `segments`(vec = 768×float16 BLOB, 페이지·초 위치), FTS5 키워드 색인. 코드는 별도 파일 |
| 검색 | `Sources/DigUpKit/Searcher.swift` | 모든 벡터와 코사인 → **모달별 평균을 빼서 척도 맞춤** → 파일별 최고 구간 → 키워드 커버리지 가산 → "in a video" 같은 종류 힌트 가산 |
| 규칙 | `Sources/DigUpKit/Rules.swift` | 숨김 폴더·키·인증서·캐시·앱 번들 건너뜀, 코드 프로젝트(`.git`, `package.json` 등) 안에서는 스크린샷만, 데이터셋처럼 보이는 폴더 건너뜀 |
| 헬퍼 | `Sources/digup/` | `search-service`, `index-service`: JSON 한 줄씩 주고받는 프로세스 |
| Windows 셸 | `shell/src-tauri`, `shell/ui` | Tauri(Rust) 트레이·단축키·검색 패널. 위 헬퍼를 실행해 JSON 프로토콜로 통신 |
| 빌드 | `scripts/build-llama.sh` | Windows에서는 MSVC + `GGML_BACKEND_DL` + CUDA(nvcc 있으면) DLL 빌드 |
| 기준 벡터 | `Tests/LlamaRuntimeTests/Reference/` | 고정 입력 9개의 벡터. llama.cpp 업데이트마다 코사인 ≥ 0.999 확인 |

원본 Windows 경로는 Swift on Windows + MSVC + CUDA Toolkit 빌드가 필요하고 README에도 아직 "on the way"이다. 이 프로젝트는
그 설계를 따르되 **Python으로 다시 구현**한다.

## 2. 이 프로젝트의 구조

```
선택한 폴더만 ─► Crawler (Rules: 건너뛸 경로·확장자·비밀 파일)
              ─► Extractors (Pillow/pillow-heif, pypdfium2, python-docx 등, ffmpeg, Windows OCR)
              ─► Engine: llama-server.exe (CUDA, 127.0.0.1만) ── EmbeddingGemma 2 Q8_0 + mmproj
              ─► Index: SQLite 1개 (files, segments + float16 벡터, FTS5)
검색어 ───────► Engine(텍스트 전용 서버, ~0.3 GB) ─► Searcher (코사인 + 모달별 정규화 + 키워드) ─► UI
```

### 2.1 Engine — 1단계에서 구현·검증함

- **llama.cpp b11461 공식 릴리스 `win-cuda-13.4-x64` + cudart** 를 `vendor/llama/`에 압축만 풀어 쓴다. 원본과 같은
  태그·커밋(`4d756bc72`)이라 기준 벡터와 직접 비교할 수 있다. CUDA Toolkit, Visual Studio 설치 불필요.
- `src/noseout/engine.py`가 `llama-server.exe`를 `--host 127.0.0.1`, 빈 포트로 띄우고 `/embedding`으로 요청한다.
  - `--embeddings --pooling mean`, `-c/-b/-ub 4096`, `-np 1`, `-ngl 999`
  - 미디어: `--mmproj … --image-min-tokens 1 --image-max-tokens 1120`, 이미지는 Python에서 미리 리사이즈해 PNG로 전달
  - `LLAMA_MEDIA_MARKER=<__media__>` 고정 (b11461 서버는 기본이 실행마다 무작위 marker)
  - ubatch를 4096으로 둔 이유: 서버가 혼합 배치에서 이미지 토큰을 ubatch/2로 제한한다(2048이면 1120 → 1024로 깎임).
  - 텍스트는 `/tokenize`로 토큰화해 2048 토큰 상한(앞 2047 + `<eos>`, 원본과 같은 규칙)을 적용한 뒤 토큰 id로 보낸다.
  - 모든 HTTP는 `trust_env=False` 세션으로 보낸다(환경 프록시를 타지 않음). 시작이 실패하면 서버를 반드시 종료한다.
- 원본은 라이브러리 링크 + 1줄 패치(임베딩 모델에서 logits 버퍼를 만들지 않게 함)를 쓴다. 공식 바이너리에는 이 패치가
  없어 요청마다 호스트 메모리를 더 쓸 수 있다. 벡터 값에는 영향이 없고(기준 일치 확인), RAM 64 GB에서는 문제가 되지 않지만
  4단계 성능 작업에서 다시 본다.

### 2.2 프로세스 나누기 (원본과 같은 방식)

| 프로세스 | 모델 | 언제 |
|---|---|---|
| 검색 서버 | 텍스트 GGUF만 (mmproj 없이) | 검색창이 열릴 때. 로드 약 2초 |
| 색인 서버 | 텍스트 + mmproj | 색인할 때만. 끝나면 종료 |
| 앱(Python) | — | 트레이, 단축키, 감시, DB, 검색 |

### 2.3 Index와 Searcher (2단계에서 구현)

- 원본 스키마(`files`, `segments`, `fts`)를 거의 그대로 쓴다. Windows에서는 `inode/device` 대신 NTFS file ID
  (`os.stat().st_ino`, `st_dev`)를 쓴다.
- 벡터는 float16으로 저장, 검색 때 numpy 행렬로 올려 한 번에 행렬곱한다. 10만 구간 × 768 = 150 MB 정도.
- 점수는 원본과 같다: 모달(text/image/audio)별 평균을 뺀 코사인을 쓰고, FTS5 키워드 커버리지를 가산한다.
  이 정규화가 필요하다는 근거는 `model-validation.md` 4절(이미지→설명 방향 오답)에 있다.

### 2.3.1 2단계에서 원본과 다르게 한 것

| 항목 | 원본 | 여기 | 이유 |
|---|---|---|---|
| 한국어 키워드 | 벵골어·아랍어만 접두어 검색 | 한글도 접두어 검색, 한국어 불용어·종류 단어 | 조사("청구서를")가 붙어도 찾도록 |
| 금지 폴더 | 홈 폴더 선택 가능 | 드라이브 루트·사용자 폴더 전체·시스템 폴더 거부 | 사용자 요구: 시스템 전체 색인 금지 |
| 클라우드 파일 | iCloud 전용 파일 건너뜀 | OneDrive 온디맨드 파일 특성으로 건너뜀 | Windows |
| 링크 | (macOS 열거기 처리) | 심볼릭 링크·정션 따라가지 않음 | 고른 폴더 밖으로 나가지 않게 |
| 폴더 키워드 | 홈 기준 상대 경로 | 부모 폴더 3단계 | 단순화 |
| 이미지 전처리 | 긴 변 1024/1536px로 줄인 뒤 예산 리사이즈 | 원본 파일에서 바로 예산 리사이즈 | 결과 크기는 같고 단계 하나 줄임. 벡터 차이는 3단계에서 측정 |
| 긴 파일 | 30쪽·40청크씩 나눠 읽음 | 같음 (3단계), setsScale 포함 | — |
| 한국어 검색어 조사 | — | 검색어 끝 조사를 뗌("비용은" → "비용") | 3단계, 조사 붙은 검색어가 문서 단어를 못 찾던 문제 |
| FTS 후보 | 구간 1,000행에서 자른 뒤 파일별 | 파일별 최고 구간으로 순위, 종류 필터를 SQL에서 | 3단계 Codex 검토: 페이지 많은 PDF가 다른 파일을 밀어냄 |
| 벡터 점수 계산 | 블록 단위 | 블록 단위 (3단계, 2단계는 전체 사본) | 메모리 |
| file ID | inode | NTFS file ID를 `os.stat`으로, 64비트 부호 있는 값으로 저장 | `DirEntry.stat()`은 Windows에서 0을 준다 |
| 스크린샷·스캔 OCR | Apple Vision (Windows판엔 없음) | Windows.Media.Ocr (ko → en-US), 키워드용 | 4단계 |
| ffmpeg 위치 | 헬퍼 옆, $DIGUP_FFMPEG, PATH | vendor/ffmpeg, $NOSEOUT_FFMPEG, PATH | 시스템 설치 없이 |

### 2.4 안전 원칙 (사용자 요구사항)

- 사용자가 고른 폴더만 읽는다. 드라이브 전체·시스템 폴더는 기본으로 막는다.
- 파일은 읽기만 한다. 쓰기는 앱 데이터 폴더(`%LOCALAPPDATA%\NoseOut`, 옛 `DigUpWin`에서 첫 실행 때 복사)의 색인·임시 파일뿐이다.
- 네트워크는 모델·바이너리를 처음 받을 때만 쓴다. llama-server는 127.0.0.1에만 바인딩한다.
- 원본의 비밀 파일 규칙(키, 인증서, `.ssh`, 비밀번호 파일 등)을 그대로 옮긴다.

### 2.4.1 빠른 이름 검색 (원본에 없음, 2026-10-14 추가)

Everything처럼 AI 분석 전부터 이름으로 찾기 위한 기능. Everything은 볼륨 전체 MFT·USN을 관리자 서비스로 읽지만, 여기서는
**고른 폴더만 `os.scandir`로 걷고 기존 감시(ReadDirectoryChangesW)를 재사용**한다(관리자 권한·서비스·추가 설치 없음, 2.4의 원칙과
맞음). Everything 분석 보고서(REA-Lab)는 구조 참고로만 읽었고 코드·DB 형식은 옮기지 않았다.

```
폴더 추가·앱 시작·'색인 시작' ─► NameService 스레드(엔진 없음) ─► names.sqlite ◄─ NameSearchService 스레드(읽기 전용 연결)
감시 이벤트 ─┬► 이름: 추가·삭제·이름 변경을 0.15초 모아 바로 (AI 작업의 busy·중지·색인 유무와 무관)
            └► AI: 기존 규칙 그대로
검색창 ─► 이름 검색(늘) + AI 검색(엔진 준비 뒤) ─► "이름 일치 / 내용" 두 그룹
```

| 항목 | 결정 | 이유 |
|---|---|---|
| 저장 | `index.sqlite`와 다른 파일 `names.sqlite` (entries + 좁은 검색 표 `search`, 트리거로 동기화) | AI 색인 스키마·U2 버전 검사·엔진 불일치와 무관하게 열림. 파생 데이터라 형식이 다르면 다시 만든다 |
| 대상 | 고른 검색 폴더와 코드 폴더 안 모든 확장자의 파일과 폴더 이름 (코드 폴더만인 부분은 vendor·build·dist·서브모듈도 제외) | 사용자 결정. 금지·숨김·시스템·링크·정션·개발 폴더·비밀 파일 규칙은 AI와 같다. 내용 분석 규칙(코드 프로젝트 안 스크린샷만, 데이터셋)은 쓰지 않는다 |
| 비교 | NFKC + casefold (`search_text.normalized`) 부분 문자열, 단어 AND, 이름 또는 고른 폴더 아래 상대 경로 | NFD 한글·전각·대소문자. 2글자 한국어도 찾는다(trigram은 3글자 미만 불가) |
| 순위 | 이름 완전 일치(확장자 제외 포함) > 앞부분 > 이름 안 > 폴더 경로에서만, 다음 이름 길이, 등록 순서 | 좁은 표만으로 순위를 매겨 상위 후보만 읽는다(넓은 표 결합보다 30% 빠름) |
| 증분 | 폴더 단위 diff(직접 하위 목록 ↔ 저장된 행). 폴더 이름 변경·대소문자 변경은 옛 경로 아래를 지우고 새 폴더를 다시 걷는다 | 경로 문자열을 고쳐 쓰지 않아 파일 시스템과 어긋날 여지가 없다 |
| 접근 실패 | 열 수 없는 폴더·항목은 행을 지우지 않고 `trouble`에 적어 결과에 "상태 미확인", 고른 폴더 자체가 없으면 "접근 불가" | 접근 실패 ≠ 삭제 (AI 색인과 같은 원칙) |
| 결과 | 이름 일치(내용 결과가 있으면 8개 + 더 보기) / 내용(AI 결과 순서·쪽·시점·줄 그대로). 같은 파일은 내용 칸에 "이름·내용" | AI 점수·setsScale은 건드리지 않는다 |
| 열기 | 실행 파일·스크립트·바로 가기(exe, msi, bat, ps1, py, lnk 등 + PATHEXT)는 Enter로 실행하지 않고 위치만, 폴더는 탐색기 | 이름 결과는 모든 파일을 내므로 |
| 분석 중 | AI 색인 진행 중 몇 초마다 검색 벡터를 다시 읽음(별도 연결 = 커밋된 구간만). 간격 = max(3초, 다시 읽기 시간 × 10) | 끝낸 파일부터 의미 검색. 다시 읽는 동안 막히는 시간을 10% 이하로 |

### 2.4.2 드라이브 전체 이름 검색 + 선택 범위 AI 분석 (2026-10-14)

이름·경로 검색은 드라이브 전체까지 넓히고, AI 내용 분석은 고른 폴더와 이름 결과에서 하나씩 고른 파일로 한정한다. MFT·USN·관리자
서비스 없이 폴더 순회(`names.py`)와 기존 감시를 그대로 쓴다. **Everything 수준의 속도는 보장하지 않는다**(측정은 `results/names-bench.json`, `results/ntfs-bench.json`).

| 정책 | 이름 검색 (`names.py`, `rules.name_root_problem`) | AI 분석 (`crawler.py`, `rules.forbidden_root_reason`) |
|---|---|---|
| 고를 수 있는 범위 | 드라이브 루트, 사용자 폴더 전체, 일반 폴더 | 일반 폴더만 (드라이브 루트·사용자 폴더 전체 금지 그대로) + '내용 분석'으로 고른 파일 하나씩 |
| 시스템 폴더 | Windows·Program Files·ProgramData·AppData(Local·Roaming, 캐시 대부분) 제외, 드라이브 최상위의 Recovery·PerfLogs·Windows.old·$WinREAgent 등 제외 | 같은 폴더 금지 (그대로) |
| 숨김·시스템 특성 | 제외 | 제외 |
| 링크·정션 | 따라가지 않음, 범위로도 못 고름 | 같음 |
| 개발 산출물·캐시 | node_modules·.venv·__pycache__ 등 제외, 코드 폴더만인 부분은 vendor·build·dist·서브모듈도 | 같음 + 코드 프로젝트 안은 스크린샷만(내용 분석 규칙) |
| 비밀 파일 | 이름도 넣지 않음 | 읽지 않음 |
| 읽는 것 | 이름·크기·수정 시각만 | 내용(추출·임베딩) |
| 접근 거부 | 지우지 않고 "상태 미확인" | 지우지 않음 (기존) |

- 등록 순서: 검색·코드 폴더를 먼저 걷고 드라이브를 나중에 걷는다. 이미 걸은 폴더는 다시 들어가지 않는다. 일시정지(폴더 경계에서
  대기, 자리 유지)·중지(커밋한 이름은 남음, 다음 등록이 나머지를 맞춤)는 폴더 창 '이름 검색 범위' 탭.
- 감시: 감시 핸들은 바깥 폴더(드라이브)에 하나만 연다. 시스템 폴더 이벤트는 이벤트 단계에서 버린다. 이름 범위의 변경은 이름 목록에만,
  AI 갱신은 고른 폴더와 고른 파일만. 드라이브 감시 넘침 때 드라이브 다시 걷기는 10분에 한 번까지(그 안의 고른 폴더는 AI가 바로 다시 확인).
- AI 분석 순서: 파일마다 분석 비용을 대략 추정하고(종류·크기, `indexer.estimated_seconds`) 최근 7일·90일 안에 바뀐 파일은 비용을 4·2로
  나눠 작은 것부터 읽는다. 시작할 때 "분석할 파일 N개 · 예상 약 M"을 알린다. 이후 갱신은 기존처럼 바뀐 파일만.
- 첫 실행: 문서·바탕 화면·다운로드·사진(SHGetKnownFolderPath)과 사용자 폴더 바로 아래 작업 폴더(Projects, source\repos 등)를 경로만
  확인해 체크 목록으로 보인다. 고르기 전에는 폴더 안을 열지 않는다. 시스템 드라이브 전체 이름 검색은 기본 체크(이름·경로만).

### 2.4.3 설정창과 색인 현황 (2026-10-15)

| 탭 | 저장 | 내용 |
|---|---|---|
| 일반 | 적용·확인·취소 | 전역 단축키, 로그인 시 실행(HKCU Run, EXE만), 시작 시 검색 창 / 트레이 |
| 검색 범위 | 누르는 즉시 (기존 방식) | 기존 `FoldersDialog` 그대로: AI 분석 폴더·코드 폴더·제외(공통)·이름 검색 범위·AI 분석 개별 파일, 감시, 색인 시작 |
| 화면 | 적용·확인·취소 | 테마, 큰 검색창 기본 보기, 검색창 크기·위치(닫을 때 자동 저장) |
| 색인 현황 | 읽기만 | `stats.collect`를 `StatsService` 스레드에서. 지금 범위 기준, 없는 색인은 만들지 않음 |

- 적용(`App.apply_preferences`): 단축키(새 조합을 먼저 등록, 성공하면 옛 것 해제) → 시작 항목 → `settings.json` 안전 저장(`prefs.atomic_write`).
  실패하면 거꾸로 되돌리고 이전 설정을 유지한다. 외부 상태와 저장된 설정이 어긋나지 않게 하는 순서다.
- 시작 항목은 `"<NoseOut.exe>" --startup` 형식만 이 앱 것으로 본다. 끌 때 그 형식이 아니면 지우지 않는다. 테스트는 `MemoryRun`만 쓴다.
- 작업 상태: 이름 등록(`NameService` 진행·일시정지·완료)과 내용 분석(`IndexService` 진행·계획·일시정지·완료·실패)을 따로 들고,
  검색 가능·남은 수는 색인을 센 값만 쓴다(계획 전에는 '확인 중'). 결과 없음 안내(`content_note`)도 이 값으로만 말한다.

### 2.4.4 이름 목록 MFT/USN (2026-10-15)

| 단계 | 권한 | 방법 |
|---|---|---|
| 처음 등록 (빠른 NTFS, 사용자가 누를 때) | 관리자 도우미 1회 (UAC) | `ntfs_helper.py`가 MFT 열거 버퍼만 인증 파이프로 전송 → 앱이 `NameIndex.import_mft`로 고른 범위만 넣음 |
| 처음 등록 (자동·권한 거부·자동 시작) | 일반 | 폴더 순회(`NameIndex.scan(only=…)`) |
| 재실행 따라잡기, 실행 중 반영 | 일반 | 비권한 USN 읽기 → 부모 폴더 참조 번호로 경로 → `refresh_folders`(그 폴더의 직접 목록만) |
| 네트워크·NTFS 아님·폴더에 연결한 볼륨·폴더 순회 선택 | 일반 | 기존 폴더 순회 + 폴더 감시 |

- 볼륨 상태(`volumes` 표): GUID·일련번호·저널 ID·처리한 USN·완료 여부·등록한 범위. 맞지 않으면 그 볼륨 범위만 다시 등록. 체크포인트는 커밋 뒤에만.
- 감시(`WatchService.set_usn_roots`): USN이 맡은 이름 범위에는 감시 핸들·이름 변경·전체 다시 걷기를 보내지 않는다. AI 분석 폴더 감시는 그대로.
- MFT의 이름도 폴더 순회와 같은 규칙과 사용자 권한(목록을 열 수 있는 폴더만)을 거친다. 하드링크의 다른 이름은 폴더 순회로 확인.

### 2.5 아직 정하지 않은 것

- ~~UI 기술~~ → **PySide6**로 결정(2026-10-13). 5단계에서 구현.
- 스크린샷 OCR: Windows 내장 OCR(`Windows.Media.Ocr`, winrt 패키지) 검토. 한국어 언어팩 필요 여부 확인.
