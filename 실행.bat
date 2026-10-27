@echo off
rem Double-click launcher for Windows (D100 section 8). UTF-8 without BOM, CRLF.
rem Checks (install stamp, packages, ffmpeg) live in scripts\launch_check.py, shared with the macOS launcher.
rem Messages are English + Korean: if the console shows broken Korean, the English line still reads.
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0" || goto :no_project_dir

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_PY=%CD%\.venv\Scripts\python.exe"

echo [vlogsrt] Preparing the workbench...
echo [vlogsrt] 브이로그 작업대를 준비합니다.

if exist "%VENV_PY%" goto :check_venv

set "BASE_PY="
call :find_python
if not defined BASE_PY goto :no_python
echo [vlogsrt] First run: creating .venv. This can take a few minutes.
echo [vlogsrt] 처음 실행이라 실행 환경 .venv 를 만듭니다. 몇 분 걸릴 수 있습니다.
%BASE_PY% -m venv "%CD%\.venv"
if errorlevel 1 goto :venv_failed
goto :check_install

:check_venv
"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto :old_venv

:check_install
set "PY=%VENV_PY%"
"%PY%" scripts\launch_check.py stamp-ok
if not errorlevel 1 goto :check_tools
echo [vlogsrt] Installing packages. Needs internet; can take a few minutes.
echo [vlogsrt] 필요한 꾸러미를 설치합니다. 인터넷 연결이 필요하고 몇 분 걸릴 수 있습니다.
"%PY%" -m pip install --upgrade pip
if errorlevel 1 echo [vlogsrt] pip upgrade failed - continuing with the current pip.
"%PY%" -m pip install -e ".[ui,project]"
if errorlevel 1 goto :install_failed
"%PY%" scripts\launch_check.py write-stamp
echo [vlogsrt] Install finished. / 설치를 마쳤습니다.
goto :check_tools

:install_failed
rem Offline re-install: if everything is already there, start anyway and retry next time.
"%PY%" scripts\launch_check.py importable
if errorlevel 1 goto :install_fatal
echo [vlogsrt] Install failed, but the packages are already there - starting anyway.
echo [vlogsrt] 꾸러미 설치에 실패했지만 이미 깔린 것으로 계속합니다. 다음 실행에서 다시 시도합니다.

:check_tools
"%PY%" scripts\launch_check.py tools
if errorlevel 1 goto :pause_fail

echo [vlogsrt] Starting. A browser window opens shortly. Press Ctrl+C to quit.
echo [vlogsrt] 브이로그 작업대를 시작합니다. 잠시 뒤 브라우저가 열립니다. 종료하려면 Ctrl+C.
"%PY%" -u -m ui.app %*
set "APP_EXIT=%ERRORLEVEL%"
if "%APP_EXIT%"=="0" exit /b 0
echo.
echo [vlogsrt] The program exited with error code %APP_EXIT%.
echo [vlogsrt] 프로그램이 오류 코드 %APP_EXIT% 으로 종료되었습니다.
pause
exit /b %APP_EXIT%

rem ---- Python discovery: .venv first (above), then py -3.13/3.12/3.11, then python on PATH.
:find_python
for %%V in (3.13 3.12 3.11) do (
  if not defined BASE_PY (
    py -%%V -c "import sys" >nul 2>&1 && set "BASE_PY=py -%%V"
  )
)
if defined BASE_PY exit /b 0
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "BASE_PY=python"
exit /b 0

:no_project_dir
echo [vlogsrt] Could not open the project folder.
echo [vlogsrt] 프로젝트 폴더로 이동하지 못했습니다.
goto :pause_fail

:no_python
echo.
echo [vlogsrt] Python 3.11 or newer was not found. Install it with:
echo [vlogsrt] Python 3.11 이상이 필요합니다. 설치 명령:
echo     winget install Python.Python.3.12
echo [vlogsrt] Then close this window and run again from a new one.
echo [vlogsrt] 설치한 뒤 이 창을 닫고 다시 실행하세요.
goto :pause_fail

:venv_failed
echo [vlogsrt] Could not create .venv.
echo [vlogsrt] 실행 환경 .venv 를 만들지 못했습니다.
goto :pause_fail

:old_venv
echo [vlogsrt] The Python inside .venv is older than 3.11. Delete the .venv folder and run again.
echo [vlogsrt] .venv 의 Python이 3.11보다 오래됐습니다. .venv 폴더를 지운 뒤 다시 실행하세요.
goto :pause_fail

:install_fatal
echo [vlogsrt] Package install failed. Check the error above and your internet connection.
echo [vlogsrt] 꾸러미 설치에 실패했습니다. 위 오류와 인터넷 연결을 확인하세요.
goto :pause_fail

:pause_fail
echo.
pause
exit /b 1
