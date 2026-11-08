@echo off
setlocal
cd /d "%~dp0"
title Danbooru Tagger v0.7.2 - lokalna baza tagow
echo.
echo Instalacja lokalnej bazy tagow SQLite (snapshot 2026-04-08)
echo Baza jest pobierana z Hugging Face, nie z Danbooru.
echo.
if not exist ".venv\Scripts\python.exe" (
  echo BLAD: Nie ma .venv. Uruchom 1_SETUP.cmd z paczki FULL.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install "huggingface_hub>=0.23"
if errorlevel 1 goto :error
".venv\Scripts\python.exe" download_tag_db.py
if errorlevel 1 goto :error
echo.
echo Baza zainstalowana. Mozesz teraz uruchomic Taggera.
pause
exit /b 0
:error
echo.
echo Nie udalo sie pobrac bazy. Aplikacja bedzie dzialac z dotychczasowym JSON.
pause
exit /b 1
