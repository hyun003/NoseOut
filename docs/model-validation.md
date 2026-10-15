# 모델·추론 엔진 검증

작성: 2026-10-13 · 측정값 원본: `results/metrics.json`, `results/benchmark-cuda.json`, `results/benchmark-cpu.json`

## 1. PC 환경 확인 결과

| 항목 | 결과 | 비고 |
|---|---|---|
| OS | Windows 11 Pro 10.0.26300 | |
| GPU | RTX 4070 SUPER, 12,282 MiB, compute capability 8.9 | 평소 약 2 GB 사용 중(다른 앱) |
| NVIDIA 드라이버 | 617.42, CUDA UMD 13.4 | CUDA 13.4 런타임 빌드 사용 가능 |
| CUDA Toolkit (nvcc) | 없음 | 공식 바이너리에 cudart·cuBLAS DLL이 들어 있어 필요 없음 |
| Python | 3.14.4 (`pythoncore-3.14-64`) | 프로젝트 `.venv`도 3.14 |
| Git | 2.55.0.windows.5 | |
| CMake, Visual Studio, uv | 없음 | 직접 빌드할 때만 필요 |
| ffmpeg | 없음 | 영상·mp3 등은 2단계 이후에 필요 (설치 전 승인 받음) |
| 디스크 C: | 211 GB 남음 | |
| 설치한 것 | `.venv`: numpy, pillow, requests, pytest, huggingface-hub · `vendor/llama`: llama.cpp b11461 CUDA · `models/`: GGUF 2개 | 모두 프로젝트 폴더 안 |

받은 파일 무결성:
- `llama-b11461-bin-win-cuda-13.4-x64.zip` `62c91b76…61cf`, `cudart-llama-bin-win-cuda-13.4-x64.zip` `738f8c25…e668`: GitHub 릴리스 digest와 일치. `llama-server --version` → build 11461, commit `4d756bc72` (원본이 고정한 커밋과 같음)
- `embeddinggemma-2-Q8_0.gguf` `2188ac1d…9135`, `mmproj-embeddinggemma-2-Q8_0.gguf` `c4a8a526…ab64f`: 원본 `ModelFiles.swift`의 SHA-256과 일치

## 2. 추론 엔진 조사

EmbeddingGemma 2(2026-10 공개, 740M, 768차원)는 텍스트·이미지·음성·영상을 한 공간에 넣는다. 지원 런타임:

| 후보 | Windows + RTX 4070 SUPER | 원본과 같은 벡터? | 설치 부담 | 판단 |
|---|---|---|---|---|
| **llama.cpp b11461 공식 CUDA 바이너리 + llama-server** | 동작 확인 | **예** (코사인 ≥ 0.9997) | 압축 해제만 (~550 MB) | **채택** |
| llama.cpp 직접 빌드 (MSVC + CUDA Toolkit) | 가능 (원본 `build-llama.sh`가 지원) | 예 | VS Build Tools + CUDA Toolkit (수 GB) | 보류. 원본의 logits 패치가 꼭 필요해지면 |
| llama.cpp Vulkan / CPU 바이너리 | CPU 동작 확인 | CPU 예 (≥ 0.9997) | 작음 | 대체 경로(GPU 문제 시) |
| transformers / sentence-transformers + PyTorch CUDA | 가능할 것 (모델 카드 공식 경로) | 기준 구현 자체 | ~3 GB | 이번엔 미승인으로 미설치. 교차 검증이 더 필요할 때 |
| llama-cpp-python | 미확인 | — | 빌드 필요할 수 있음 | 비추천: 원본이 쓰는 새 API(`llama_batch_ext`, `llama_process`)와 b11452+ 모델 지원을 따라왔는지 불확실 |
| mlx-vlm (원본 Python 워커) | 불가 (Apple 전용) | — | — | 제외 |
| ONNX Runtime / DirectML | — | — | — | 공식 저장소에 ONNX 파일 없음. 제외 |

**llama.cpp CUDA 호환성 결과**: 문제 없음. 발견한 주의점 3가지는 모두 해결했다.
1. b11461 서버는 media marker를 실행마다 무작위로 만든다 → `LLAMA_MEDIA_MARKER=<__media__>` 환경변수로 고정.
2. 서버는 이미지 토큰을 ubatch/2로 제한한다 → `-ub 4096`으로 1120 예산까지 허용.
3. 로그 경고 `Flash Attention is assigned to device CPU`: 일부 레이어의 FA가 CPU로 간다. 결과 벡터는 기준과 일치하며,
   1120 예산 이미지가 0.5초 걸리는 원인 후보다. 4단계(성능)에서 `-fa off` 등을 비교한다.
4. (참고) 공식 바이너리에는 원본의 logits 패치가 없다. 벡터에는 영향 없음.

## 3. 테스트 계획

### 3.1 모달별 검증

| 모달 | 무엇을 | 입력 | 통과 기준 | 테스트 |
|---|---|---|---|---|
| 공통 | 원본 macOS 벡터와 일치 | 원본 `Reference/` 9개 (영·벵골·아랍 검색어, 짧은/1800토큰 문서, 스크린샷 280, 사진 140·70, 4초 음성) | 각 코사인 ≥ 0.999, 768차원, 단위 길이 | `test_reference_vectors.py` |
| 텍스트 | 다국어 검색어 → 문서 | 영·한 검색어 5개, 영·한 문서 5개(교차 언어 포함) | top-1 100% | `test_cross_modal.py::test_text_to_text_multilingual` |
| 이미지 | 설명 → 이미지, 이미지 → 설명 | 합성 이미지 5개 + 원본 사진·스크린샷 2개, 영·한 설명 | 검색어→이미지 top-1 ≥ 5/7, 짝 평균 > 비짝 평균 | `test_text_to_image[en/ko]` |
| 음성 | 설명 → 말소리 | Windows TTS로 만든 영어 3·한국어 1 문장(16 kHz mono) | top-1 ≥ 75%, 짝 평균 > 비짝 평균 | `test_text_to_speech[en/ko]` |
| 이미지 크기 | 원본과 같은 리사이즈 | 크기 5종 × 예산 5종 | 48px 배수, 예산 이하 | `test_image_sizing.py` |
| 텍스트 길이 | 2048 토큰 상한 (원본: 앞 2047 + `<eos>`) | 영·한 2000/2048/3000/6000 토큰 | 길이·`<eos>`·앞부분 일치, 2048 넘는 두 입력의 벡터 동일 | `test_engine.py` |
| 엔진 수명주기 | 시작 실패 시 서버 정리, 프록시 무시 | 멈춘 `/health`, 잘못된 모델, 죽은 프록시 환경변수 | 남은 프로세스 없음, 127.0.0.1 직접 통신 | `test_engine.py` |
| 성능 | 속도, VRAM | 합성 입력 | 기록만 | `scripts/benchmark.py` |

나중 단계에서 더할 것: 30초 넘는 음성 창 나누기, 영상 프레임, PDF 페이지(텍스트/렌더링), EXIF 회전, HEIC, 긴 문서 청크,
원본 `evals/*.json` 질의 세트로 순위 품질(MRR) 측정.

### 3.2 "같은 의미의 텍스트와 이미지가 가까운가" 검증 방법

1. **짝 행렬**: 이미지 N개와 그 설명 N개를 임베딩해 N×N 코사인 행렬 S를 만든다(대각선이 짝).
2. **양방향 top-1**: 행마다(설명→이미지), 열마다(이미지→설명) 최댓값이 대각선인지 센다.
3. **margin**: `S[i,i] − max(S[i,j≠i])`. 0에 가까우면 순위가 불안정하다.
4. **언어 바꾸기**: 같은 이미지를 영어·한국어 설명으로 각각 검색해 다국어 정렬을 본다.
5. **모달 간격**: 텍스트↔텍스트, 텍스트↔이미지, 텍스트↔음성 짝 코사인의 평균을 비교한다. 차이가 크면 모달을 섞은
   검색 결과에서 텍스트 파일이 늘 위로 올라오므로, 원본처럼 모달별 정규화가 필요하다.
6. **기준 일치를 먼저**: 1~5는 엔진이 원본과 같은 벡터를 낼 때만 의미가 있으므로, 원본 기준 벡터 테스트를 먼저 통과시킨다.

## 4. 실행 결과 (2026-10-13, RTX 4070 SUPER, CUDA)

`pytest -q`: **50 passed** (8.8초). (Codex 검토 반영 후. 처음 실행은 42개)

### 4.1 원본 기준 벡터와의 코사인

| 입력 | CUDA | CPU (`-ngl 0`) |
|---|---|---|
| query (영어) | 0.99993 | 0.99993 |
| query-bn (벵골어) | 0.99993 | 0.99991 |
| query-ar (아랍어) | 0.99994 | 0.99994 |
| document | 0.99985 | 0.99976 |
| document-long (~1800 토큰) | 0.99989 | 0.99992 |
| screen-280 | 0.99993 | 0.99995 |
| photo-140 | 0.99979 | 0.99978 |
| frame-70 | 0.99971 | 0.99968 |
| speech (4초) | 0.99975 | 0.99975 |

전부 기준 0.999 이상. 이 9개 입력(텍스트는 최대 약 1800 토큰)에서 Windows CUDA 엔진은 원본 macOS(Metal)와 같은 벡터를 만든다.
2048 토큰을 넘는 텍스트는 원본 코드와 같은 규칙(앞 2047 토큰 + `<eos>`)으로 자르도록 구현하고 경계 테스트를 했지만,
그 구간의 원본 기준 벡터는 없으므로 "규칙이 같다"까지만 확인한 것이다.

### 4.2 교차 모달

| 검사 | 질의→대상 top-1 | 대상→질의 top-1 | 짝 평균 | 비짝 평균 | 최소 margin |
|---|---|---|---|---|---|
| 텍스트→텍스트 (영·한 섞음) | 5/5 | 5/5 | 0.783 | 0.538 | 0.166 |
| 텍스트→이미지 (영어) | 7/7 | 6/7 | 0.741 | 0.569 | 0.031 |
| 텍스트→이미지 (한국어) | 7/7 | 6/7 | 0.695 | 0.554 | 0.038 |
| 텍스트→음성 (영어 질의) | 4/4 | 4/4 | 0.800 | 0.509 | 0.254 |
| 텍스트→음성 (한국어 질의) | 4/4 | 4/4 | 0.765 | 0.504 | 0.148 |

관찰:
- 검색 방향(설명으로 이미지·음성 찾기)은 영어·한국어 모두 전부 맞았다. 한국어 질의로 영어 음성, 영어 질의로 한국어 음성도 찾는다.
- 이미지→설명 방향 오답 1건: 원본의 추상 스크린샷(`screen.png`, 글자 없는 회색 막대와 빨간 경고 띠)이 "흰 배경의 빨간 원"
  설명에 더 가까웠다(0.680 vs 자기 설명 0.605). 그림 자체가 모호하고, 일부 설명이 전반적으로 높은 점수를 받는(hub) 현상이다.
  실제 검색은 검색어→파일 방향이라 영향이 작지만, 원본처럼 **질의별·모달별 정규화**를 하는 근거가 된다.
- 텍스트→이미지 margin이 0.03 수준으로 작다. 모양이 단순한 합성 이미지끼리 비교했기 때문이며, 실제 사진으로 다시 잰다(2단계).
- 모달 간격(짝 평균): 텍스트↔텍스트 0.783, 텍스트↔이미지 0.741, 텍스트↔음성 0.800. 이 작은 세트에서는 차이가 크지 않지만,
  비짝 평균도 모달마다 달라(0.50~0.57) 정규화 없이 섞으면 순위가 기울 수 있다.

### 4.3 속도와 메모리 (`scripts/benchmark.py`)

| 항목 | CUDA | CPU (8 threads) |
|---|---|---|
| 서버 준비 (텍스트+mmproj) | 1.8 s | 1.6 s |
| 검색어 1개 | 14 ms | 25 ms |
| 문서 (~250 토큰) | 136 개/s | 26 개/s |
| 이미지 70 / 280 / 1120 토큰 | 51 / 123 / 501 ms | 0.48 / 2.2 / 14.3 s |
| 음성 4초 | 28 ms | 218 ms |
| 추가 VRAM | 약 1.7 GB (작업 후 2.2 GB) | — |

이미지 시간에는 Python 쪽 디코드·리사이즈·PNG 인코딩이 들어 있다. 같은 사진의 70과 1120 예산 벡터 코사인은 0.956으로,
예산에 따라 벡터가 꽤 달라진다. 그래서 원본처럼 종류별 예산(사진 140, 스크린샷·PDF 280, 영상 70)을 고정하고 색인에 기록한다.

## 5. 재현 방법

```powershell
cd $env:USERPROFILE\Projects\DigUp-Windows
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# llama.cpp b11461 (SHA-256은 1절)
#   https://github.com/ggml-org/llama.cpp/releases/download/b11461/llama-b11461-bin-win-cuda-13.4-x64.zip
#   https://github.com/ggml-org/llama.cpp/releases/download/b11461/cudart-llama-bin-win-cuda-13.4-x64.zip
#   → vendor\llama\ 에 둘 다 압축 해제
.venv\Scripts\python -c "from huggingface_hub import hf_hub_download as d; [d('ggml-org/embeddinggemma-2-GGUF', f, revision='bfcd298762cc34d0357ece5ebdd31791a3a374d8', local_dir='models') for f in ['embeddinggemma-2-Q8_0.gguf','mmproj-embeddinggemma-2-Q8_0.gguf']]"
.venv\Scripts\python scripts\make_fixtures.py
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\benchmark.py
.venv\Scripts\python scripts\benchmark.py --cpu
```

## 6. Codex 검토 (2026-10-13)

`adversarial-review` 결과 **needs-attention, 3건** → 모두 수정하고 테스트를 추가했다.

| 등급 | 지적 | 조치 |
|---|---|---|
| high | `requests`가 `HTTP_PROXY` 등 환경 프록시를 따르면 127.0.0.1 요청(문서 내용·이미지)이 프록시로 갈 수 있음 | `requests.Session(trust_env=False)`로 모든 서버 통신. 죽은 프록시를 지정한 테스트 추가 |
| medium | `/health`가 응답을 미루면 `requests.Timeout`이 잡히지 않아 생성자가 실패하고 서버 프로세스가 남음 | 서버 실행 후 초기화 전체를 `try/except → close()`로 감쌈, `RequestException` 전체를 재시도, `kill()` 뒤 `wait()`, 로그 파일 핸들 닫기. 멈춘 health·잘못된 모델 테스트 추가 |
| medium | `MAX_TOKENS=2048`을 선언만 하고 적용하지 않음 | `/tokenize`로 토큰화해 2048 초과 시 앞 2047 + `<eos>`로 자르고 토큰 id로 전송. 영·한 경계 테스트 추가, 4.1절 주장 범위를 좁힘 |

수정 후 기준 벡터 코사인은 수정 전과 같았다(텍스트를 문자열 대신 토큰 id로 보내도 동일). Codex 실행 환경에서는 git 확인과
테스트 재실행이 되지 않아, Codex의 판단은 파일 내용만 근거로 했다.

## 7. 남은 위험

- 합성 데이터만으로 측정했다. 실제 사진·문서 품질은 2단계에서 사용자가 고른 **테스트 폴더 하나**로 확인한다.
- `Flash Attention → CPU` 경고의 성능 영향은 아직 재지 않았다.
- Python 3.14에서 이후 필요한 패키지(pypdfium2, pillow-heif, winrt OCR, PySide6)의 휠 제공 여부는 각 단계 시작 때 확인한다.
