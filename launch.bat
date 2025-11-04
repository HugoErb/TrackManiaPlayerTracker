@echo off
setlocal

rem — Se placer à la racine du script
cd /d "%~dp0"

rem — Chemin de l'interpréteur du venv (adapte si ton venv a un autre nom)
set "VENV_PY=venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERREUR] Environnement virtuel introuvable : %VENV_PY%
    echo Cree-le d'abord avec ton script d'installation, puis relance.
    exit /b 1
)

rem — Lancer main.py avec le Python du venv, en transmettant les arguments
"%VENV_PY%" "main.py" %*
set "RC=%ERRORLEVEL%"

echo.
echo Appuyez sur n'importe quel bouton pour fermer la fenetre...
pause >nul

endlocal & exit /b %RC%
