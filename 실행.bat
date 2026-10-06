@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title MA20 전환 리포트

echo ============================================================
echo   MA20 하락 - 상승 전환 리포트
echo ============================================================
echo.

rem ---- 1. Python 찾기 ----
set "PY="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PY=py -3"
if defined PY goto check_pkg
python --version >nul 2>nul && set "PY=python"
if defined PY goto check_pkg
goto no_python

rem ---- 2. 필요한 패키지 확인 ----
:check_pkg
%PY% -c "import pandas, requests" >nul 2>nul
if not errorlevel 1 goto check_key
echo 처음 실행이라 필요한 프로그램을 설치합니다. 1-2분 걸릴 수 있습니다.
echo.
%PY% -m pip install --user --disable-pip-version-check requests pandas
if errorlevel 1 goto pip_fail
echo.

rem ---- 3. 인증키 확인 ----
:check_key
if exist ".env" goto run
echo KRX Open API 인증키가 아직 없습니다.
:ask_key
set "KEY="
set /p "KEY=인증키를 붙여 넣고 Enter: "
if "%KEY%"=="" goto ask_key
> ".env" echo KRX_API_KEY=%KEY%
echo 인증키를 저장했습니다.
echo.

rem ---- 4. 실행 - 지수 + 주식형/원자재 ETF 전체 ----
:run
echo KRX에서 데이터를 받는 중입니다. 처음에는 1-2분 걸릴 수 있습니다.
echo.
%PY% krx_ma20_turn.py
if errorlevel 1 goto run_fail
echo.
echo 완료했습니다. 브라우저에 리포트가 열립니다.
echo 결과 파일은 results 폴더에도 저장되어 있습니다.
echo.
pause
exit /b 0

:no_python
echo [오류] 이 컴퓨터에 Python이 설치되어 있지 않습니다.
echo.
echo  1. https://www.python.org/downloads/ 에서 Python을 내려받아 설치하세요.
echo  2. 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
echo  3. 설치가 끝나면 이 파일을 다시 더블클릭하세요.
echo.
pause
exit /b 1

:pip_fail
echo.
echo [오류] 필요한 프로그램 설치에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 실행하세요.
echo.
pause
exit /b 1

:run_fail
echo.
echo [오류] 실행 중 문제가 생겼습니다. 위의 [실패] 메시지를 확인하세요.
echo 인증키가 잘못된 경우 이 폴더의 .env 파일을 지우고 다시 실행하면 새로 입력할 수 있습니다.
echo.
pause
exit /b 1
