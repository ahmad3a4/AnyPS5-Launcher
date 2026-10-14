@echo off
rem Starts the AnyPS5 Launcher without a console window.
where pythonw >nul 2>nul && (start "" pythonw "%~dp0launcher\AnyPS5 Launcher.pyw" %* & exit /b)
where py >nul 2>nul && (start "" py -3 "%~dp0launcher\AnyPS5 Launcher.pyw" %* & exit /b)
echo Python 3.12 or newer is needed: https://www.python.org/downloads/windows/
pause
