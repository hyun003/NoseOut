# Git 이력 정리와 공개용 라이선스 보완 결과

사용자 요청: 기존 저장소 이력 정리, EXE·DLL·모델 공개 배포 조건 보완. 실제 업로드는 요청 범위가 아니다.

## Git 이력

- 기존 브랜치 `master`의 커밋 13개를 유지하며 문서 blob 3개의 개인 경로를 `%USERPROFILE%`로 치환했다.
- Windows TTS 시험 WAV 4개를 모든 커밋의 추적 대상에서 뺐다. 실제 작업 폴더의 WAV는 보존했다.
- 커밋 순서·메시지·작성 정보는 유지했다. HEAD는 `1b281717…`에서 `875302db…`로 바뀌었다.
- 새 Git 디렉터리는 원 이력 객체를 그대로 물려받지 않고 정리된 객체로 다시 만들었다. 모든 도달 가능 객체와 남은 객체를 검사했다.
- 개인 경로·주요 자격증명 패턴의 남은 일치 0건. 제한된 패턴 검사이며 모든 형태의 비밀정보에 대한 보증은 아니다.
- 적용 전후 작업 파일 해시가 모두 일치했다. 새 개발 커밋을 만들지 않았고 기존 미커밋 변경은 그대로 남아 있다.
- 원 Git 디렉터리·bundle·작업 파일 ZIP은 `outputs/history-backup-20261015`에 보존했다. **이 백업에는 정리 전 정보가 있으므로 공개하지 않는다.**
- 증거: `results/history-cleanup-20261015.json`. 정리한 브랜치의 현재 변경을 검토·커밋한 다음 push할 수 있다.

## 공개용 구성에서 해결한 사항

| 항목 | 적용 |
| --- | --- |
| HEIC의 GPL x265 의존성 | 사용자 선택으로 공개용에서 pillow-heif·HEIC/HEIF 내용 분석 제외. 이름 검색 유지 |
| FFmpeg 대응 소스·내장 라이브러리 | 사용자 선택으로 실행물 동봉 제외. 공식 고정 릴리스 다운로드 도구와 이중 SHA256 검사 제공 |
| Microsoft 런타임 재배포 사용권 미확인 | DLL 동봉 제외. 사용자가 공식 VC++ x64 런타임 설치 후 준비 도구 실행 |
| Qt Mesa 소프트웨어 렌더러의 별도 구성 | 선택적 opengl32sw.dll 제외 |
| 앱·원본·Python·패키지 고지 | MIT/PSF 및 24개 패키지 메타데이터·라이선스 원문 수집. Qt 고지·GNU 전문 동봉 |
| Qt LGPL 대응 소스 | qtbase·qtsvg·qtimageformats·qttranslations·pyside-setup 6.12.0의 공식 소스와 교체·재빌드 안내 보존 |
| certifi MPL | Python 소스와 CA 인증서 묶음을 대응 소스에 포함 |
| NVIDIA CUDA | 공식 CUDA 13.4.1의 cudart 13.4.49·cuBLAS 13.7.0.27과 DLL 3개 해시 일치, 해당 EULA 보존 |
| 모델 Apache 고지 | 정확한 Google·GGUF 리비전의 README, Apache 전문, 변환 출처와 모델 해시 보존 |

버전별 고지는 `licenses/`, 정확한 소스 URL·SHA256은 `licenses/corresponding-sources.json`, NVIDIA 파일 근거는
`results/cuda-license-provenance-20261015.json`에 있다. 세부 사용 조건은 `docs/binary-distribution.md`를 따른다.
기존 개인용 `outputs/NoseOut`은 배포용으로 바꾼 것이 아니다. 원 파일 457개는 검토 전 해시와 모두 같음을 확인했다.

## 제작·확인

- `scripts/build_exe.py --public-candidate`로 새 폴더에 제작했다. 기존 NoseOut 폴더를 덮어쓰는 요청은 거부한다.
- 테스트의 기존 `--basetemp=.tmp`가 빌드와 준비 도구의 임시 자료를 함께 지워 첫 시도가 실패했다. 해당 실행은 중단했다.
  기본 임시 경로를 `.tmp/pytest`로 좁히고 별도 시험 경로에서 전체 테스트를 다시 실행했다. 실사용 EXE·설정은 대상이 아니었다.
- 분리 후 전체 Python 테스트: **378개 통과, 종료 코드 0**.
- 중단 후 이어서 실행한 최신 전체 재검증은 **377개 통과·1개 실패, 종료 코드 1**이었다.
  `test_name_search_works_while_ai_indexing_runs_and_with_a_failed_engine`의 이름 검색 3개는 모두 성공했지만,
  마지막 확인 때 AI 색인이 이미 완료돼 `indexing.running` 검사가 실패했다. 시간 의존 가능성이 있으며 확정 원인 분석은 하지 않았다.
  같은 테스트를 단독으로 3회 재실행하면 모두 통과했다. 테스트나 앱 코드를 이 결과에 맞춰 변경하지 않았다.
  실패 원본은 비공개 `outputs/release-materials-20261015/private`, 사용자 경로만 가린 결과는
  `results/license-full-tests-20261015.json`에 보존한다. 라이선스 보완 완료를 전체 기능 무결함 확인으로 설명하지 않는다.
- PowerShell 준비 도구는 시험 폴더에서 확인했다. Windows PowerShell 5.1에서도 동작하도록 자기 버전의 서명 검사 모듈을 명시했다.
- FFmpeg는 2년 보관 정책의 2026-09-30 월말 LGPL 빌드로 고정했다. 상류 보관 만료 전에 URL·해시를 갱신해야 한다.
- 공개용 복사본의 준비 도구 실행, EXE 내부 Python 코드와 동봉 DLL만 사용한 핵심 모듈 import, Qt offscreen 이벤트 루프,
  HEIC 모듈 없음, 실제 EXE의 잘못된 도우미 인자 종료 코드 2를 확인했다.
- 원 사용자 설정으로 새 공개용 GUI를 실행하거나 다른 깨끗한 Windows PC에서 확인한 것은 아니다.
- 공개 ZIP에는 준비 후 생긴 Microsoft DLL이나 다운로드한 FFmpeg를 넣지 않는다. 실행 파일 ZIP과 정확한 대응 소스 ZIP을 함께 제공한다.

## 공개할 때

소스는 정리된 저장소의 검토한 최신 커밋 또는 새 소스 ZIP을 사용한다. 실행물은 별도 공개용 ZIP을 사용하고,
`NoseOut-corresponding-sources-20261015.zip`을 같은 Releases 위치에 함께 게시한다. 원 이력 백업·개인용 EXE·시험 복사본은 제외한다.
이후 개인적으로 FFmpeg·Microsoft DLL을 추가한 폴더를 다시 ZIP으로 묶어 공개하면 이 구성과 달라진다.

라이선스 의무에 맞춰 배포 자료를 보완한 결과이며 국가별 상표·코덱 특허·모델의 전체 권리나 모든 PC의 실행을 보증한 것은 아니다.
이번 작업에서는 GitHub 저장소 생성·push·Releases 게시·새 개발 커밋을 하지 않았다.
