@echo off
setlocal
cd /d "%~dp0"
title Danbooru Tagger v0.5.0 - pierwsza konfiguracja

echo.
echo ==============================================
echo   Danbooru Tagger v0.5.0 - pierwsza konfiguracja
echo ==============================================
echo.
echo Ten krok tworzy lokalne srodowisko, instaluje PyTorch CUDA
echo i pobiera PixAI Tagger v1.0. Niczego nie musisz wpisywac.
echo Jesli aktualizujesz starsza wersje, NIE musisz uruchamiac tego setupu.
echo.

set "PY_CMD="
py -3.14 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.14"
if not defined PY_CMD (
    python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1 && set "PY_CMD=python"
)

if not defined PY_CMD (
    echo BLAD: Nie znaleziono Pythona 3.10 lub nowszego.
    echo Na tym komputerze oczekiwany jest Python 3.14.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Tworze lokalne srodowisko .venv...
    %PY_CMD% -m venv .venv
    if errorlevel 1 goto :error
) else (
    echo [1/4] Srodowisko .venv juz istnieje.
)

set "PY=.venv\Scripts\python.exe"

echo [2/4] Aktualizuje pip...
"%PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :error

echo [3/4] Instaluje PyTorch z CUDA 13.0 oraz biblioteki taggera...
"%PY%" -m pip install --upgrade torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cu130
if errorlevel 1 goto :error
"%PY%" -m pip install --upgrade transformers timm Pillow huggingface_hub safetensors numpy
if errorlevel 1 goto :error

echo [4/4] Pobieram model PixAI Tagger v1.0...
"%PY%" download_model.py
if errorlevel 1 goto :error

echo.
echo Sprawdzam GPU...
"%PY%" -c "import torch; print('PyTorch:', torch.__version__); print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU fallback')"

echo.
echo GOTOWE. Od teraz uruchamiaj plik DanbooruTagger.vbs.
echo Za chwile otworze program.
echo.
start "" ".venv\Scripts\pythonw.exe" "app.pyw"
timeout /t 3 /nobreak >nul
exit /b 0

:error
echo.
echo ==============================================
echo BLAD KONFIGURACJI
echo Nie zamykaj tego okna, jesli chcesz skopiowac komunikat bledu.
echo Mozesz tez wyslac mi zrzut tego okna.
echo ==============================================
pause
exit /b 1
