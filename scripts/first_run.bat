@echo off
chcp 65001 >nul
setlocal
rem NoseOut first-run helper for the public package. Saved as UTF-8 without BOM, CRLF.
rem python314.dll itself needs the Microsoft VC++ runtime, so NoseOut.exe cannot report a missing runtime.
rem This file only covers that case; NoseOut.exe checks versions and prepares the NumPy alias itself.
rem Keep comments ASCII: cmd misplaces line offsets after some long UTF-8 comment lines.
set "NOSEOUT_SYS=%SystemRoot%\System32"
set "NOSEOUT_MISSING="
for %%D in (vcruntime140.dll vcruntime140_1.dll msvcp140.dll) do if not exist "%NOSEOUT_SYS%\%%D" set "NOSEOUT_MISSING=1"
if defined NOSEOUT_MISSING goto need_runtime
start "" "%~dp0NoseOut.exe"
exit /b 0

:need_runtime
echo.
echo Microsoft Visual C++ 2015-2022 x64 런타임이 설치되어 있지 않습니다.
echo NoseOut을 실행하려면 Microsoft 공식 페이지에서 X64 버전을 설치하세요.
echo   https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist
echo 설치한 뒤 이 파일이나 NoseOut.exe를 다시 실행하세요.
echo 안내 페이지를 브라우저로 엽니다.
echo.
start "" "https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist"
pause
exit /b 1
