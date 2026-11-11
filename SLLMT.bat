@echo off
rem Start SLLMT from the top of the repository: runs SLLMT_LLM\SLLMT.bat
if not exist "%~dp0SLLMT_LLM\SLLMT.bat" (
  echo SLLMT_LLM folder not found next to this file. Keep the downloaded folders together.
  pause
  exit /b 1
)
call "%~dp0SLLMT_LLM\SLLMT.bat"
