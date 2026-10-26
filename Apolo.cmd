@echo off
rem Inicia o Apolo em background, sem janela de console.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m apolo
