@echo off
REM ==============================================================
REM  DEPRECATED: prefer `codebotd setup` from a regular cmd / PowerShell.
REM  This script is kept as a manual fallback for users who can't
REM  (or don't want to) run the Python installer. It does the same
REM  thing — registers a Task Scheduler task that runs `codebotd start`
REM  at every user logon.
REM
REM  Pure schtasks.exe, no extra software, no admin shell required.
REM
REM  Use this script only if you need to bypass `codebotd setup`.
REM ==============================================================
REM Install codebotd as an onlogon Task Scheduler task.
REM Run from a regular (non-elevated) cmd / PowerShell.

setlocal EnableDelayedExpansion

set TASK_NAME=CodeBot

REM --- Auto-detect codebotd.exe ------------------------------------------------
where codebotd.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set CODEBOTD_EXE=codebotd.exe
) else (
    where codebotd >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        set CODEBOTD_EXE=codebotd
    ) else (
        echo [ERROR] codebotd not found in PATH
        echo         Install the package first: pip install codebot
        exit /b 1
    )
)

echo Using codebotd: %CODEBOTD_EXE%

REM --- Driver step is intentionally skipped --------------------------------
REM Code Bot is driver-free on Windows: the firmware exposes MS OS 2.0
REM Descriptors that bind Interface 0 (Vendor Bulk) to inbox winusb.sys
REM on first plug. No INF, no pnputil, no admin shell required.
echo.
echo === Step 1: Driver — driver-free (MS OS 2.0 ^> inbox winusb.sys) ===
echo   Nothing to install. Plug in the device once after this script
echo   finishes, and Windows will auto-bind WinUSB on first enumeration.

REM --- Register onlogon task via schtasks -----------------------------------
REM /RL HIGHEST runs the task with the highest available privileges for
REM the user, so the daemon can talk to the USB device. /F overwrites an
REM existing task with the same name (idempotent re-runs are safe).
echo.
echo === Step 2: Register onlogon task '%TASK_NAME%' ===
schtasks /Create /TN %TASK_NAME% /TR "\"%CODEBOTD_EXE%\" start" /SC ONLOGON /RL HIGHEST /F
if %ERRORLEVEL% neq 0 (
    echo [ERROR] schtasks /Create failed (exit=%ERRORLEVEL%)
    exit /b 1
)

REM --- Best-effort query for immediate confirmation -------------------------
echo.
echo === Step 3: Verify task registration ===
schtasks /Query /TN %TASK_NAME% /V /FO LIST
if %ERRORLEVEL% neq 0 (
    echo [WARN] schtasks /Query failed; verify with `schtasks /Query /TN %TASK_NAME%`
)

echo.
echo === Done ===
echo   Task "%TASK_NAME%" registered. It fires at every user logon.
echo   Verify with: codebotd status
echo   Or in Task Scheduler UI (taskschd.msc).
echo.
echo   Trigger now without waiting for next logon:
echo     schtasks /Run /TN %TASK_NAME%

endlocal
exit /b 0
