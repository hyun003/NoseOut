# NoseOut EXE 배포 라이선스 검토

검토일: 2026-10-15. 대상: 현재 실사용 배포 폴더 `outputs/NoseOut` 전체.

**결론: 현재 폴더 그대로의 공개 배포는 준비되지 않았다.** 실제 고지 누락이 있고, HEIC의 GPL 결합 조건과 LGPL 대응 소스 제공 방식이 미완료다. 소스 저장소에 MIT 파일을 넣는 것만으로 해결되지 않는다. 조건을 충족한 배포 구성을 만들 수 없다는 뜻은 아니다.

## 검토 대상과 근거

- 파일 457개, 그중 EXE/DLL/PYD 153개. 모델 두 개와 별도 엔진·FFmpeg도 포함한다.
- `NoseOut.exe` SHA256: `f64a02b9a7f8ee041bd14c5cf5c354620f48ea6a934b15f6f1de0500ac32e8bf`.
- 파일별 크기·해시: `results/exe-license-inventory-20261015.json`.
- 주요 DLL의 PE import 관계·해시: `results/exe-license-pe-20261015.json`.
- 실제 FFmpeg의 `-version`, `-L` 출력과 EXE 내부 Python 모듈 목록: `results/exe-license-runtime-20261015.json`.
- 배포 폴더의 고지, 현재 빌드 스크립트, 설치 패키지 메타데이터를 공식 프로젝트의 라이선스와 대조했다. 설치 환경의 버전 정보는 보조 근거이며, 모든 파일의 재현 가능한 빌드 출처를 대신하지 않는다.
- 앱 코드·실사용 EXE·설정은 변경하지 않았다. 재빌드·앱 테스트·업로드·커밋은 하지 않았다.

## 주요 판정

| 구성요소 | 실제 확인 | 배포 전에 남은 일 |
| --- | --- | --- |
| NoseOut·원본 DigUp | 소스에는 MIT 고지가 있으나 배포 폴더에는 없음 | 원권리자를 포함한 MIT 전문과 제3자 고지 동봉 |
| HEIC: pillow-heif 1.8.1 | 같은 프로세스에서 libheif가 GPL x265 DLL을 직접 불러옴 | GPL 없는 빌드로 교체하거나 현 결합 구성의 GPL 조건 이행 |
| PySide6/Qt 6.12.0 | Qt DLL을 동적 로딩. 필요한 LGPL/GPL 전문·소스 안내를 배포 폴더에서 찾지 못함 | 선택한 라이선스에 맞는 고지·대응 소스 제공·사용자 교체 권리 확보 |
| FFmpeg | 실제 바이너리는 LGPLv3 이상, x264·x265 비활성 | 정확한 빌드의 대응 소스와 빌드 정보·포함 라이브러리 조건 정리 |
| llama.cpp b11461 | MIT, LLVM OpenMP 고지만 동봉 | llama.cpp 자체 MIT 고지와 다른 동봉 구성요소 고지 보완 |
| NVIDIA CUDA DLL 3개 | 재배포 대상 계열이나 적용 EULA·고지 미동봉 | 정확한 취득 패키지·버전과 허용 목록·조건 대조 및 필요한 고지 보존 |
| EmbeddingGemma 2 GGUF 2개 | 공식 원모델은 Apache-2.0, 배포 폴더에는 모델 고지 없음 | 변환본의 출처·리비전·변경 사항 확인, LICENSE와 해당 NOTICE 보존 |
| Python·기타 패키지 | 여러 MIT/BSD/Apache/MPL 구성요소가 EXE 안에 포함됨 | 실제 포함 목록을 기준으로 고지·라이선스·필요 소스 접근 안내 완성 |
| PyInstaller | 부트로더 예외 존재 | 사용 자체가 앱 전체 GPL 의무를 만들지는 않음 |

## 1. 가장 먼저 결정할 사항: HEIC의 GPL 구성

실제 의존 관계는 `_pillow_heif.cp314-win_amd64.pyd → libheif-…dll → libx265-217-…dll`이다. libheif는 libde265도 불러온다. 추출 코드가 pillow-heif를 등록하므로 단순히 폴더에 우연히 남은 파일로 보기도 어렵다.

동봉 `LICENSES_bundled.txt`는 Python 소스와 바이너리 wheel의 조건을 구별하며, wheel을 GPLv2, libheif·libde265를 LGPLv3, x265를 GPLv2로 표시한다. 따라서 Python 소스의 BSD 고지만으로 해당 wheel을 배포할 수 있다고 판단하면 안 된다. [사용 버전의 공식 동봉 고지](https://github.com/bigcat88/pillow_heif/blob/v1.8.1/LICENSES_bundled.txt)

x265 4.3의 공식 헤더에는 GPLv2 **또는 이후 버전** 선택이 명시되어 있다. 그러므로 GPLv2-only와 LGPLv3의 조합이라는 이유로 무조건 배포 불가능하다고 단정할 수 없다. 다만 헤더 확인만으로 배포된 전체 wheel의 모든 구성에 대한 GPLv3 경로가 확정되는 것은 아니다. 정확한 소스·패치·빌드 구성의 조건을 함께 맞춰야 한다. [x265 4.3 원문](https://raw.githubusercontent.com/Multicorewareinc/x265/4.3/source/x265.h)

현 상태를 유지하려면 x265를 포함하는 결합물의 GPL 적용 범위, 배포 라이선스와 완전한 대응 소스·빌드 절차를 정리해야 한다. 같은 프로세스에서 연결되는 구성이라 별도 프로그램을 단순 동봉한 경우와 동일하게 취급할 근거가 부족하다. HEIC를 읽기만 하거나 사용자가 기능을 쓰지 않아도, 포함한 바이너리의 배포 조건은 없어지지 않는다. MIT로 작성한 자체 소스의 기존 허가와 결합 실행물의 배포 조건은 구별해야 한다.

실무적으로는 **공개용 빌드에서 GPL x265 의존성을 없애는 경로**를 우선 권한다. HEIC를 유지하려면 인코더 없는 libheif·pillow-heif 빌드 등으로 실제 의존 관계를 바꾸고, 남는 LGPL 조건은 이행해야 한다. 또는 공개용 EXE에서 HEIC 구성 자체를 제외할 수 있다. 현재 libheif의 import 대상인 x265 DLL만 지우는 방법은 정상적인 해결이 아니다. 이번 검토에서는 어떤 DLL도 제거하지 않았다.

## 2. Qt/PySide6: 동적 DLL만으로 끝나지 않음

PySide6-Essentials·shiboken6 6.12.0과 Qt Core/Gui/Widgets/Svg 등의 DLL이 포함된다. 현재 메타데이터는 LGPL-3.0-only/GPL 대안을 표시한다. LGPL 경로를 택하면 적절한 사용 고지, LGPL 및 참조 GPL 전문, 해당 Qt 구성의 대응 소스 제공 방식, 사용자의 라이브러리 교체·수정본 실행 및 그 디버깅을 위한 역공학 권리를 확보해야 한다. 현재 onedir DLL 구조는 이를 구현하는 데 유리하지만, 그 자체가 이행 완료의 증거는 아니다. [Qt 공식 의무 안내](https://www.qt.io/development/open-source-lgpl-obligations), [Qt for Python 라이선스](https://doc.qt.io/qtforpython-6/licenses.html)

배포 폴더에서 이 고지와 Qt 소스 제공 안내를 찾지 못했다. 단순한 최신 Qt 홈페이지 링크가 정확한 배포 버전의 소스 제공 의무를 모두 대신한다고 볼 수 없다. 선택한 라이선스가 허용하는 방식으로 해당 소스·변경분·필요 빌드 정보를 제공하고 교체 가능한 패키지인지 확인해야 한다. LGPL 조건을 충족할 수 있다면 이 사실만으로 유료 Qt 구매가 필수인 것은 아니다.

## 3. FFmpeg: HEIC 쪽 x265와 별개

실제 버전은 `n8.1.3-18-g46d899d7a0-20261012`이며, `-L`은 LGPLv3 이상을 출력했다. configure에는 `--enable-version3`, `--disable-libx264`, `--disable-libx265`가 있고 `--enable-gpl`·`--enable-nonfree`는 없다. **현재 확인한 FFmpeg를 x265 GPL 빌드라고 분류할 근거는 없다.** [공식 빌드 프로젝트의 변형 설명](https://github.com/BtbN/FFmpeg-Builds)

현재 묶음에는 ffmpeg·ffprobe와 LGPL 전문만 있고 정확한 대응 소스·빌드 자료 또는 적합한 소스 제공 약정이 없다. 정적으로 포함된 외부 라이브러리도 있어 FFmpeg 본체 소스만 확인하면 끝나는 것은 아니다. 날짜·커밋·구성 옵션·빌드 레시피와 각 의존 소스를 보존하고, 배포 방식에 맞는 소스 접근을 제공해야 한다. 매번 내용이 바뀌는 latest 다운로드 링크만 남기지 않는 편이 적절하다. [FFmpeg 공식 법적 안내](https://ffmpeg.org/legal.html)

NoseOut은 FFmpeg를 별도 프로세스로 호출한다. 이 구성만으로 앱 전체가 FFmpeg의 LGPL로 바뀐다고 판단하지 않는다. 다만 FFmpeg 바이너리를 재배포하는 책임은 별도로 남는다.

## 4. llama.cpp·CUDA·모델

llama.cpp b11461은 MIT지만 배포 폴더에는 `LICENSE-LLVM-OpenMP`만 있고 llama.cpp 자체 MIT 고지가 없다. 원권리자 고지를 포함한 전문을 보완해야 한다. [해당 태그의 MIT 원문](https://raw.githubusercontent.com/ggml-org/llama.cpp/b11461/LICENSE)

`cudart64_13.dll`, `cublas64_13.dll`, `cublasLt64_13.dll`이 들어 있다. NVIDIA EULA는 CUDA runtime·cuBLAS 계열에 조건부 재배포 경로를 제공하므로 CUDA가 들어 있다는 이유로 배포 금지라고 결론내리지 않는다. 반대로 DLL 이름만으로 조건 충족도 확정할 수 없다. 취득한 정확한 CUDA 패키지 버전의 EULA, 재배포 허용 파일 목록, 저작권·제3자 고지와 배포 요건을 맞춰야 한다. 현재 묶음에는 해당 EULA·고지가 없다. 확인한 최신 문서는 2026-01-26 갱신본이며, DLL별 적용 버전과의 최종 대응은 미확정이다. CUDA 부분까지 일괄 GPL이라고 표기해서도 안 된다. [NVIDIA 공식 EULA](https://docs.nvidia.com/cuda/eula/index.html)

모델은 `embeddinggemma-2-Q8_0.gguf`, `mmproj-embeddinggemma-2-Q8_0.gguf` 두 개다. 공식 **EmbeddingGemma 2** 모델 카드는 Apache-2.0을 표시한다. 이전 Gemma의 별도 약관을 그대로 적용할 근거는 없다. 다만 현재 모델 폴더에 LICENSE/NOTICE가 없으므로 Apache 전문, 관련 저작권·NOTICE(존재하는 경우), 변환·양자화 변경 고지를 보존해야 한다. [공식 모델 카드](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2), [Apache-2.0 조건](https://www.apache.org/licenses/LICENSE-2.0)

기존 모델 검증 기록의 변환본 출처는 `ggml-org/embeddinggemma-2-GGUF`, 리비전 `bfcd298762cc34d0357ece5ebdd31791a3a374d8`이다. 이번 웹 확인에서는 해당 리비전의 변환본 고지 전체를 직접 대조하지 못했다. 원모델의 Apache 판정과 정확한 GGUF 배포 고지 확인을 구별한다. 개발 모델 폴더에 있는 Whisper 모델은 현재 EXE 폴더에 없어 이 EXE의 재배포 대상으로 세지 않았다.

## 5. 나머지 런타임과 고지 누락

Python 3.14.4, Pillow, requests, urllib3, charset-normalizer, idna, lxml, PyYAML, packaging, setuptools, WinRT 등이 실제 파일 또는 EXE의 Python 아카이브에 들어 있다. 현재 묶음에는 Python PSF와 여러 허용적 라이선스의 전문·고지가 빠져 있다. Python의 OpenSSL·SQLite·압축 모듈, Pillow·lxml의 내장 라이브러리, MSVC 런타임도 정확한 파일별 출처를 포함해 목록을 완성해야 한다. 버전명만으로 모든 하위 라이선스를 확인했다고 볼 수 없다. [Python 3.14.4 라이선스](https://raw.githubusercontent.com/python/cpython/v3.14.4/LICENSE), [Microsoft 재배포 안내](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)

certifi와 CA 인증서 묶음은 MPL-2.0 검토 대상이다. 파일 단위 고지와 해당 소스 접근 의무를 정리하면 되며 certifi 포함만으로 앱 전체를 MPL로 공개해야 한다는 뜻은 아니다. [certifi 라이선스](https://raw.githubusercontent.com/certifi/python-certifi/master/LICENSE)

이미 들어 있는 고지도 있다. NumPy의 다수 라이선스와 pypdfium2의 라이선스 파일 18개, pillow-heif의 BSD 전문·구성 안내, FFmpeg의 LGPL 전문, LLVM OpenMP 고지는 보존되어 있다. 이를 전부 누락으로 세지 않았다. 다만 일부 고지가 있다는 사실은 전체 묶음의 조건 충족을 입증하지 않는다. [pypdfium2 공식 라이선스 안내](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing)

PyInstaller는 부트로더 예외를 제공한다. PyInstaller로 EXE를 만들었다는 이유만으로 NoseOut 전체를 GPL로 바꿔야 한다는 결론은 잘못이다. 이 예외가 다른 동봉 라이브러리의 조건까지 없애지는 않는다. [PyInstaller 공식 라이선스](https://pyinstaller.org/en/stable/license.html)

## 배포 준비를 마치려면

1. HEIC의 GPL 의존성을 없앤 공개용 구성 또는 GPL 조건을 충족하는 현 구성 유지 중 하나를 정한다. 권고는 전자다.
2. 실제 동봉 파일별 버전·출처·라이선스·고지 목록을 완성하고, 앱/원본 MIT와 모든 필요한 제3자 전문을 배포 폴더에 포함한다.
3. LGPL/GPL 대상의 정확한 소스·패치·빌드 자료를 확보하고 배포 방식에 맞는 소스 제공을 마련한다. Qt 교체와 수정본 실행 권리도 보장한다.
4. CUDA의 정확한 재배포 조건과 GGUF 변환본 고지를 확정한다.
5. 이후 별도 요청으로 공개용 패키지를 만들 때만 새 결과물을 확인한다. 현재 개인용 EXE와 준비된 소스 ZIP은 이번 검토에서 바꾸지 않았다.

이 문서는 확인된 누락과 아직 충족 여부를 확정하지 못한 항목을 구별한 배포 검토다. 모든 정적 내장 코드의 소스 일치성, 모델의 전체 권리 관계, 코덱 특허·상표와 국가별 법률까지 무침해 보증한 것은 아니다. **라이선스 검토는 수행했지만, 배포 조건 이행은 완료하지 않았다.**
