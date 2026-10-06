@echo off
setlocal EnableExtensions EnableDelayedExpansion

title NEXUS AI - Full Test & Validation Suite
chcp 65001 >nul 2>&1
color 0A

rem ============================================================================
rem Resolve project root from this .bat file location
rem ============================================================================
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

pushd "%PROJECT_ROOT%"
if errorlevel 1 (
    echo [ERROR] Could not enter project root: "%PROJECT_ROOT%"
    pause
    exit /b 1
)

rem ============================================================================
rem Paths
rem ============================================================================
set "PYTHON=%PROJECT_ROOT%\venv\Scripts\python.exe"
set "STREAMLIT_APP=dashboard\streamlit_app.py"

rem ============================================================================
rem Default behavior
rem ============================================================================
set "PAUSE_BETWEEN=0"
set "PAUSE_AT_END=1"
set "STREAMLIT_MODE=ask"

rem ============================================================================
rem Parse arguments
rem ============================================================================
:parse_args
if "%~1"=="" goto args_done

if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h" goto usage

if /i "%~1"=="--ci" (
    set "PAUSE_BETWEEN=0"
    set "PAUSE_AT_END=0"
    set "STREAMLIT_MODE=skip"
)

if /i "%~1"=="--no-pause" set "PAUSE_AT_END=0"
if /i "%~1"=="--pause-between" set "PAUSE_BETWEEN=1"
if /i "%~1"=="--launch-streamlit" set "STREAMLIT_MODE=auto"
if /i "%~1"=="--no-streamlit" set "STREAMLIT_MODE=skip"
if /i "%~1"=="--ask-streamlit" set "STREAMLIT_MODE=ask"

shift
goto parse_args

:args_done

rem ============================================================================
rem Validate virtual environment Python
rem ============================================================================
if not exist "%PYTHON%" (
    echo [ERROR] venv Python not found:
    echo         "%PYTHON%"
    echo.
    echo Create it first:
    echo   python -m venv venv
    echo   venv\Scripts\python.exe -m pip install -r requirements.txt
    if "%PAUSE_AT_END%"=="1" pause
    popd
    exit /b 1
)

rem ============================================================================
rem Python environment optimizations
rem ============================================================================
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

set "BASE_PYTHONPATH=%PROJECT_ROOT%;%PROJECT_ROOT%\models;%PROJECT_ROOT%\backend"
if defined PYTHONPATH (
    set "PYTHONPATH=%BASE_PYTHONPATH%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%BASE_PYTHONPATH%"
)

rem ============================================================================
rem Test counters
rem ============================================================================
set /a TOTAL=0
set /a PASSED=0
set /a FAILED=0
set "FAILED_TESTS="

rem ============================================================================
rem Run tests
rem ============================================================================
call :run_test "Basic Model Test" "models\test_model.py"
call :run_test "Integration Test" "models\test_integration.py"
call :run_test "Final Test" "models\test_final.py"

rem ============================================================================
rem Summary
rem ============================================================================
echo.
echo ============================================================
echo   NEXUS AI Test Summary
echo ============================================================
echo Total   : %TOTAL%
echo Passed  : %PASSED%
echo Failed  : %FAILED%
if not "%FAILED_TESTS%"=="" echo Failed:   %FAILED_TESTS:~1%
echo ============================================================

rem ============================================================================
rem Streamlit launch logic
rem ============================================================================
set "CHOICE=N"

if "%STREAMLIT_MODE%"=="auto" set "CHOICE=Y"
if "%STREAMLIT_MODE%"=="skip" set "CHOICE=N"

if "%STREAMLIT_MODE%"=="ask" (
    echo.
    echo Start Streamlit Dashboard in a separate window?
    set /p CHOICE="Start Streamlit? (Y/N): "
)

if /i "%CHOICE%"=="Y" (
    if not exist "%PROJECT_ROOT%\%STREAMLIT_APP%" (
        echo [WARN] Streamlit app not found: "%PROJECT_ROOT%\%STREAMLIT_APP%"
    ) else (
        echo [STREAMLIT] Launching dashboard in a new window...
        start "NEXUS Streamlit Dashboard" /D "%PROJECT_ROOT%" cmd /k "%PYTHON%" -m streamlit run "%STREAMLIT_APP%"
        echo [OK] Streamlit is starting in a new window.
    )
) else (
    echo [INFO] Streamlit launch skipped.
)

rem ============================================================================
rem Finish
rem ============================================================================
echo.
echo [DONE] All tasks completed.

if "%PAUSE_AT_END%"=="1" pause

set "FINAL_RC=0"
if %FAILED% GTR 0 set "FINAL_RC=1"

popd
endlocal & exit /b %FINAL_RC%

rem ============================================================================
rem Help
rem ============================================================================
:usage
echo.
echo NEXUS AI Full Test Suite
echo.
echo Usage:
echo   run_tests_optimized.bat [options]
echo.
echo Options:
echo   --help, -h             Show this help.
echo   --ci                   Non-interactive: no pauses, no Streamlit prompt.
echo   --no-pause             Do not pause at the end.
echo   --pause-between        Pause between each test.
echo   --launch-streamlit     Automatically launch Streamlit after tests.
echo   --no-streamlit         Never launch or ask for Streamlit.
echo   --ask-streamlit        Ask whether to launch Streamlit, default behavior.
echo.
echo Examples:
echo   run_tests_optimized.bat
echo   run_tests_optimized.bat --ci
echo   run_tests_optimized.bat --pause-between --launch-streamlit
echo.
popd
endlocal & exit /b 0

rem ============================================================================
rem Subroutine: run one test
rem ============================================================================
:run_test
set /a TOTAL+=1
set "TEST_NAME=%~1"
set "TEST_FILE=%~2"

echo.
echo ============================================================
echo   [%TOTAL%/3] %TEST_NAME%
echo   File: %TEST_FILE%
echo ============================================================

if not exist "%TEST_FILE%" (
    echo [FAIL] Missing test file: "%TEST_FILE%"
    set /a FAILED+=1
    set "FAILED_TESTS=!FAILED_TESTS! %TEST_NAME%"

    if "%PAUSE_BETWEEN%"=="1" (
        echo.
        echo Press any key to continue...
        pause >nul
    )

    exit /b 1
)

"%PYTHON%" "%TEST_FILE%"
set "RC=!ERRORLEVEL!"

if "!RC!"=="0" (
    echo [PASS] %TEST_NAME%
    set /a PASSED+=1
) else (
    echo [FAIL] %TEST_NAME% exited with code !RC!
    set /a FAILED+=1
    set "FAILED_TESTS=!FAILED_TESTS! %TEST_NAME%"
)

if "%PAUSE_BETWEEN%"=="1" (
    echo.
    echo Press any key to continue...
    pause >nul
)

exit /b !RC!