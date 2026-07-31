@echo off
REM ==============================================================
REM  DEPRECATED: prefer `codebot setup` from an elevated shell.
REM  This script is kept as a manual fallback for users who can't
REM  (or don't want to) run the Python installer. It does the same
REM  thing — registers codebotd as a real Windows Service via NSSM
REM  with SERVICE_AUTO_START and no console window.
REM
REM  Use this script only if you need to bypass `codebot setup`.
REM ==============================================================
REM Install codebotd as a Windows service using NSSM.
REM Run as Administrator.

setlocal EnableDelayedExpansion

REM --- Configuration ----------------------------------------------------------
set SERVICE_NAME=codebotd
set SERVICE_DESC=Code Bot USB info display daemon (CH32X033F8P6)
set NSSM_PATH=C:\Tools\nssm\win64\nssm.exe

REM Auto-detect python.exe: prefer python.org installer over Store stub.
where python.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set PYTHON_EXE=python.exe
) else (
    py.exe -3 -c "import sys; print(sys.executable)" > %TEMP%\pyexe.txt 2>nul
    set /p PYTHON_EXE=<%TEMP%\pyexe.txt
    del %TEMP%\pyexe.txt >nul 2>&1
)

if not defined PYTHON_EXE (
    echo [ERROR] python.exe / py.exe not found in PATH
    echo         Install Python 3.10+ from python.org first.
    exit /b 1
)

echo Using python: %PYTHON_EXE%
echo Using NSSM:   %NSSM_PATH%

REM --- Verify NSSM installed ---------------------------------------------------
if not exist "%NSSM_PATH%" (
    echo [ERROR] NSSM not found at %NSSM_PATH%
    echo         Install NSSM: choco install nssm
    echo         or download from https://nssm.cc/download
    exit /b 1
)

REM --- Driver step is intentionally skipped --------------------------------
REM Code Bot is driver-free on Windows: the firmware exposes MS OS 2.0
REM Descriptors that bind Interface 0 (Vendor Bulk) to inbox winusb.sys
REM on first plug. No INF, no pnputil, no admin shell required.
echo.
echo === Step 1: Driver — driver-free (MS OS 2.0 → inbox winusb.sys) ===
echo   Nothing to install. Plug in the device once after this script
echo   finishes, and Windows will auto-bind WinUSB on first enumeration.

REM --- Install service via NSSM -----------------------------------------------
echo.
echo === Step 2: Install codebotd service ===
"%NSSM_PATH%" install %SERVICE_NAME% "%PYTHON_EXE%" "-m codebot start"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] nssm install failed (exit=%ERRORLEVEL%)
    exit /b 1
)

"%NSSM_PATH%" set %SERVICE_NAME% DisplayName    "%SERVICE_DESC%"
"%NSSM_PATH%" set %SERVICE_NAME% Start           SERVICE_AUTO_START
"%NSSM_PATH%" set %SERVICE_NAME% AppStdout       %PROGRAMDATA%\codebot\codebotd.out.log
"%NSSM_PATH%" set %SERVICE_NAME% AppStderr       %PROGRAMDATA%\codebot\codebotd.err.log
"%NSSM_PATH%" set %SERVICE_NAME% AppStdoutCreationTime 0
"%NSSM_PATH%" set %SERVICE_NAME% AppStderrCreationTime 0
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateFiles  1
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateBytes  1048576
"%NSSM_PATH%" set %SERVICE_NAME% RestartOnFailureDelay 5000

REM --- Start service ----------------------------------------------------------
echo.
echo === Step 3: Start codebotd service ===
"%NSSM_PATH%" start %SERVICE_NAME%
if %ERRORLEVEL% neq 0 (
    echo [ERROR] nssm start failed (exit=%ERRORLEVEL%)
    exit /b 1
)

echo.
echo === Done ===
echo   Service "%SERVICE_NAME%" installed and started.
echo   Verify with: codebotd status
echo   Or in Services console (services.msc).
echo.
echo   Logs: %PROGRAMDATA%\codebot\codebotd.{out,err}.log

endlocal
exit /b 0
