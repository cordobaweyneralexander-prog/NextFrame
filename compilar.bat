@echo off
setlocal
echo ============================================
echo   NextFrames - compilacion (carpeta portable)
echo ============================================
echo.

if not exist "bin\ffmpeg.exe" (
    echo ERROR: falta bin\ffmpeg.exe
    echo Copia ffmpeg.exe, ffprobe.exe y TwitchDownloaderCLI.exe a la carpeta bin\ antes de compilar.
    goto :fin
)
if not exist "bin\ffprobe.exe" (
    echo ERROR: falta bin\ffprobe.exe
    goto :fin
)
if not exist "bin\TwitchDownloaderCLI.exe" (
    echo ERROR: falta bin\TwitchDownloaderCLI.exe
    goto :fin
)

REM Si quedo un NextFrames.exe de una compilacion anterior corriendo (por
REM ejemplo, si lo dejaste abierto para revisar algo), PyInstaller no puede
REM reemplazarlo y la compilacion falla con "Acceso denegado". Se cierra
REM primero, en silencio, y no pasa nada si no habia ninguno corriendo.
taskkill /F /IM NextFrames.exe /T >nul 2>&1

REM Se usa SIEMPRE el python de venv\ por su ruta exacta, en vez de "where
REM python" o del "python" que este activo en el PATH de la sesion actual.
REM Asi no importa si el cmd donde corres esto tiene el venv bien activado
REM o no: si venv\Scripts\python.exe existe, se usa ESE, sin ambiguedad.
set "PY=venv\Scripts\python.exe"

echo [1/2] Verificando entorno...
if not exist "%PY%" (
    echo ERROR: no se encontro %PY%
    echo.
    echo Crea el venv de este proyecto primero, dentro de esta misma carpeta:
    echo     python -m venv venv
    echo     venv\Scripts\activate
    echo     pip install -r requirements.txt
    goto :fin
)

echo [2/2] Compilando con PyInstaller (puede tardar varios minutos)...
"%PY%" -m PyInstaller --clean --noconfirm NextFrames.spec
if errorlevel 1 (
    echo.
    echo La compilacion fallo. Revisa el mensaje de arriba.
    goto :fin
)

echo.
echo ============================================
echo   Creando modelos\ y registros\ dentro de dist\NextFrames\
echo ============================================
REM bin\ YA quedo adentro: el propio NextFrames.spec lo empaqueta como parte
REM de la compilacion (ver bin_datas en el .spec), asi que no hace falta
REM copiarlo aqui a mano ni mantener una segunda copia dentro de dist\.
if not exist "dist\NextFrames\modelos" mkdir "dist\NextFrames\modelos"
if not exist "dist\NextFrames\registros" mkdir "dist\NextFrames\registros"

echo.
echo Verificando que bin\ haya quedado empaquetado...
if not exist "dist\NextFrames\bin\ffmpeg.exe" (
    echo AVISO: dist\NextFrames\bin\ffmpeg.exe no aparece. Revisa que tu
    echo carpeta bin\ (junto a este .bat) tenga los 3 archivos y compila otra vez.
) else (
    echo OK: bin\ empaquetado correctamente.
)

echo.
echo LISTO. La carpeta dist\NextFrames\ ya es portable.
echo Copiala completa a otro PC (con o sin Python) y abre NextFrames.exe
echo.

:fin
pause
