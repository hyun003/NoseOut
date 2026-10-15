"""개인용 EXE 진입점. 명령줄과 PYTHONPATH 설정 없이 기존 앱을 연다.

`--ntfs-helper ...`: 같은 EXE를 관리자 권한 MFT 도우미로 실행한다(ntfs_helper.py, 앱이 UAC로 띄울 때만). 이 경로에서는 Qt·앱을
불러오지 않고 도우미의 인자 검사만 거친다.
"""

import multiprocessing
import sys
import traceback

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if sys.argv[1:2] == ["--ntfs-helper"]:
        from noseout import ntfs_helper

        raise SystemExit(ntfs_helper.main(sys.argv[2:]))
    # 설정 폴더(NOSEOUT_HOME 또는 %LOCALAPPDATA%\NoseOut)를 정한다. 옛 DigUpWin 폴더만 있으면 여기서 한 번 옮긴다.
    from noseout import paths

    base = paths.home()
    try:
        # 공개용 EXE: NumPy를 불러오는 어떤 import보다 먼저 VC++ 런타임을 확인하고 NumPy 별칭을 준비한다.
        # 개인용 EXE·소스 실행에서는 아무것도 하지 않는다(runtime-setup.json 없음).
        from noseout import runtime_setup

        runtime_setup.ensure_for_frozen_app(base)
        from noseout.app import main

        raise SystemExit(main())
    except Exception:
        # 콘솔 없는 EXE도 시작 오류의 근거를 설정 폴더에 남긴다.
        base.mkdir(parents=True, exist_ok=True)
        (base / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
