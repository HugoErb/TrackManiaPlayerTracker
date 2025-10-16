@echo off
setlocal ENABLEEXTENSIONS

REM ————— Se placer à la racine du script —————
cd /d "%~dp0"

echo.
echo === Setup du projet Python ===

REM ————— Détection de Python —————
set "PYTHON_EXE="
where python >nul 2>&1 && set "PYTHON_EXE=python"
if not defined PYTHON_EXE (
    where py >nul 2>&1 && (
        REM Essaie le launcher py -3 de Windows
        py -3 -V >nul 2>&1 && set "PYTHON_EXE=py -3"
    )
)

if not defined PYTHON_EXE (
    echo [ERREUR] Python introuvable dans le PATH.
    echo Installez Python ^(https://www.python.org/downloads/^)^ OU ajoutez-le au PATH, puis relancez.
    exit /b 1
)

echo Python detecte :
%PYTHON_EXE% -V
if errorlevel 1 (
    echo [ERREUR] Impossible d'executer Python.
    exit /b 1
)

REM ————— Création du venv s’il n’existe pas —————
set "VENV_DIR=.venv"
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo Environnement virtuel deja present: %VENV_DIR%
) else (
    echo Creation de l'environnement virtuel: %VENV_DIR%
    %PYTHON_EXE% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERREUR] Echec de creation du venv.
        exit /b 1
    )
)

REM ————— Mise à jour de pip —————
echo Mise a jour de pip...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [AVERTISSEMENT] Echec de la mise a jour de pip. On continue.
)

REM ————— Installation des dependances —————
if exist "requirements.txt" (
    echo Installation des dependances depuis requirements.txt...
    "%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERREUR] Echec de l'installation des dependances.
        exit /b 1
    )
) else (
    echo [INFO] Aucun requirements.txt trouve a la racine. Etape ignoree.
)

REM ————— (Optionnel) Installer les navigateurs Playwright si la lib est presente —————
REM Decommentez la ligne ci-dessous si vous utilisez Playwright :
REM "%VENV_DIR%\Scripts\python.exe" -m playwright install chromium

echo.
echo === Terminé ===
echo Pour activer le venv :
echo   PowerShell :   .\%VENV_DIR%\Scripts\Activate.ps1
echo   CMD        :   %VENV_DIR%\Scripts\activate.bat
echo.

endlocal
exit /b 0
