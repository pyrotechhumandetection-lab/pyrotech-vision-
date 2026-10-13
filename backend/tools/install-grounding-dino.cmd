@echo off
rem ======================================================================================
rem  OPTIONAL - Object Monitoring Layer 3 (Grounding DINO name suggestions).
rem  The model (IDEA-Research/grounding-dino-tiny, Apache-2.0, ~690 MB) is NOT shipped with this
rem  folder. To enable Layer 3, give this script a folder that contains the Hugging Face cache folder
rem  "models--IDEA-Research--grounding-dino-tiny" (for example the "hf" folder of a previous install).
rem  It is copied INTO backend\models\object_monitor\hf, and the "transformers" package is installed.
rem  After that the backend uses the copy only - the source folder is not needed any more.
rem
rem    backend\tools\install-grounding-dino.cmd "D:\some\folder\hf"
rem ======================================================================================
setlocal
cd /d "%~dp0..\.."
if "%~1"=="" (
  echo Usage: backend\tools\install-grounding-dino.cmd "folder containing models--IDEA-Research--grounding-dino-tiny"
  exit /b 1
)
set "SRC=%~1"
if not exist "%SRC%\models--IDEA-Research--grounding-dino-tiny\snapshots" (
  echo "%SRC%" does not contain models--IDEA-Research--grounding-dino-tiny\snapshots
  exit /b 1
)
robocopy "%SRC%\models--IDEA-Research--grounding-dino-tiny" "backend\models\object_monitor\hf\models--IDEA-Research--grounding-dino-tiny" /E /NFL /NDL /NJH /NJS /NP
if errorlevel 8 (echo Copy failed. & exit /b 1)
"backend\.venv\Scripts\python.exe" -m pip install --disable-pip-version-check transformers==5.17.0 || (echo Installing transformers failed. & exit /b 1)
echo.
echo Grounding DINO installed. Restart the backend; Object Monitoring shows "Layer 3: On".
