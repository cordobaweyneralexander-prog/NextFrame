# -*- mode: python ; coding: utf-8 -*-
# Genera una CARPETA portable (no un solo .exe): NextFrames/ se copia entera a otro PC.
# Un .exe unico se descomprime en una carpeta temporal en cada arranque, y ahi es donde
# el VAD (que vive dentro de esa carpeta) se perdia al cerrar el programa.
import glob
import customtkinter
from pathlib import Path

ctk_dir = Path(customtkinter.__file__).parent

# bin\ (ffmpeg, ffprobe, TwitchDownloaderCLI y cualquier .dll que pongas al lado)
# se empaqueta AQUI, como parte de la propia compilacion. dist\NextFrames\ se
# borra y se rearma entero cada vez que compilas -eso es normal de PyInstaller-
# pero como estos archivos se copian DESDE bin\ (la carpeta fuente, junto a
# este .spec, que nunca se toca) en cada build, siempre terminan adentro sin
# necesidad de un paso aparte despues de compilar.
bin_datas = [(f, 'bin') for f in glob.glob('bin/*.*')]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        (str(ctk_dir), 'customtkinter'),   # temas/assets de CustomTkinter
        ('vad/silero_vad_v6.onnx', 'vad'), # copia de respaldo del VAD (ver preparar_vad())
    ] + bin_datas,
    hiddenimports=[
        'yt_dlp', 'faster_whisper', 'ctranslate2', 'onnxruntime',
        'huggingface_hub', 'customtkinter',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NextFrames',
    debug=False,
    strip=False,
    upx=False,
    console=False,        # sin consola: nf_comun.SalidaALog redirige print() al panel
    icon=None,
    contents_directory='.',  # SIN esto, PyInstaller 6+ mete todo (incluido
    # bin/ y vad/) dentro de una subcarpeta _internal/, y entonces
    # nf_comun.py (que busca bin/ justo al lado del .exe) no lo encuentra.
    # Con '.' se vuelve al esquema plano de siempre: todo junto al .exe.
    # (Va aqui, en EXE, no en COLLECT: COLLECT no tiene este parametro y
    # lo ignoraba en silencio, sin avisar ni fallar la compilacion.)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='NextFrames',    # carpeta final: dist/NextFrames/
)
