@echo off
REM ============================================================
REM  UM6P Sync - Planning d'Occupation
REM  Double-cliquez sur ce fichier pour demarrer le site.
REM  Puis ouvrez :  http://127.0.0.1:8000
REM ============================================================
cd /d "%~dp0"
title UM6P Sync - Serveur
echo Demarrage du serveur UM6P Sync...
echo Ouvrez votre navigateur sur :  http://127.0.0.1:8000
echo (Laissez cette fenetre ouverte tant que vous utilisez le site.)
python run_server.py
pause
