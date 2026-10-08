"""
nf_comun.py - Base compartida de NextFrames.
Rutas portables (todo cuelga de la carpeta del programa), configuracion en config.json,
registro en registros/, y el puente motor -> ventana (log, progreso, cancelacion).
"""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


def _base():
    # Congelado (.exe): carpeta del ejecutable. En desarrollo: carpeta de este archivo.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE = _base()
BIN = BASE / "bin"
CARPETA_CUDA = BIN / "cuda"
CARPETA_REGISTROS = BASE / "registros"
ARCHIVO_CONFIG = BASE / "config.json"
BINARIOS_OBLIGATORIOS = ("ffmpeg", "ffprobe", "TwitchDownloaderCLI")

CONFIG_DEFECTO = {
    "carpeta_salida": str(BASE / "Contenido_Extraido"),
    "carpeta_modelos": str(BASE / "modelos"),
    "idioma": "",
    "modelo": "auto",
    "dispositivo": "cpu",
    "benchmark": {},          # "<idioma>:<cpu|cuda>" -> {"modelo": ..., "compute": ...}
}


# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
def cargar_config():
    cfg = copy.deepcopy(CONFIG_DEFECTO)
    try:
        cfg.update(json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def guardar_config(cfg):
    try:
        ARCHIVO_CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log(f"AVISO: no se pudo guardar config.json ({e}).", "aviso")


def actualizar_config(**cambios):
    cfg = cargar_config()
    cfg.update(cambios)
    guardar_config(cfg)
    return cfg


# ----------------------------------------------------------------------------
# Binarios y entorno
# ----------------------------------------------------------------------------
def ruta_binario(nombre):
    exe = nombre + (".exe" if os.name == "nt" else "")
    p = BIN / exe
    if p.exists():
        return str(p)
    return shutil.which(nombre) or str(p)


def binarios_faltantes():
    return [n for n in BINARIOS_OBLIGATORIOS if not os.path.exists(ruta_binario(n))]


_dll_dirs = []


def activar_dll(carpeta):
    """Hace visibles las DLL de una carpeta (CUDA) para ctranslate2."""
    carpeta = str(carpeta)
    os.environ["PATH"] = carpeta + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory") and os.path.isdir(carpeta):
        try:
            _dll_dirs.append(os.add_dll_directory(carpeta))
        except OSError:
            pass


def preparar_entorno():
    """ffmpeg, ffprobe, aria2c y TwitchDownloaderCLI se buscan SOLO en bin/ (y luego en el PATH)."""
    if str(BIN) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if CARPETA_CUDA.is_dir() and any(CARPETA_CUDA.glob("*.dll")):
        activar_dll(CARPETA_CUDA)


def _sin_ventanas_negras():
    """Sin consola no hay stdin/stdout/stderr validos: los hijos (ffmpeg, aria2c...) fallan o abren
    ventanitas negras. Se fuerzan valores seguros en todo subprocess.Popen del programa."""
    if getattr(subprocess.Popen, "_nf_parcheado", False):
        return
    original = subprocess.Popen.__init__

    def init(self, *a, **k):
        if os.name == "nt":
            k["creationflags"] = k.get("creationflags", 0) | 0x08000000   # CREATE_NO_WINDOW
        k.setdefault("stdin", subprocess.DEVNULL)
        k.setdefault("stdout", subprocess.DEVNULL)
        k.setdefault("stderr", subprocess.DEVNULL)
        original(self, *a, **k)

    subprocess.Popen.__init__ = init
    subprocess.Popen._nf_parcheado = True


_sin_ventanas_negras()


# ----------------------------------------------------------------------------
# Puente motor -> ventana: log, progreso, cancelacion
# ----------------------------------------------------------------------------
class Cancelado(Exception):
    """El usuario pidio cancelar; no es un error."""


_cancel = threading.Event()
_cb_log = None
_cb_progreso = None


def set_callbacks(log=None, progreso=None):
    global _cb_log, _cb_progreso
    _cb_log, _cb_progreso = log, progreso


def cancelar():
    _cancel.set()


def reiniciar_cancelacion():
    _cancel.clear()


def comprobar_cancelacion():
    if _cancel.is_set():
        raise Cancelado("Proceso cancelado por el usuario.")


def _registro(texto, nivel):
    try:
        CARPETA_REGISTROS.mkdir(parents=True, exist_ok=True)
        ahora = datetime.now()
        with open(CARPETA_REGISTROS / f"{ahora:%Y-%m-%d}.log", "a", encoding="utf-8") as f:
            f.write(f"{ahora:%H:%M:%S} [{nivel}] {texto}\n")
    except OSError:
        pass


def log(texto, nivel="info"):
    """nivel: info | ok | aviso | error | traza"""
    _registro(texto, nivel)
    if _cb_log:
        _cb_log(texto, nivel)
    elif sys.__stdout__ is not None:
        sys.__stdout__.write(texto + "\n")


def progreso(etapa, valor, texto=""):
    """etapa: video | audio | chat | modelo | transcripcion | benchmark.
    valor: 0..1, o None si es indeterminado."""
    if _cb_progreso:
        _cb_progreso(etapa, valor, texto)


def clasificar(linea, defecto="info"):
    u = linea.upper().lstrip()
    if "FALLO" in u or u.startswith("ERROR") or "ERROR:" in u or "ERROR INESPERADO" in u:
        return "error"
    if "AVISO" in u or "ADVERTENCIA" in u or u.startswith("[!]"):
        return "aviso"
    if "[OK]" in u or u.startswith("OK "):
        return "ok"
    return defecto


class SalidaALog(io.TextIOBase):
    """Sustituye a sys.stdout / sys.stderr: cada print del motor termina en el panel de registro."""
    encoding = "utf-8"

    def __init__(self, nivel_defecto="info"):
        self._buf = ""
        self._nivel = nivel_defecto

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, s):
        if not s:
            return 0
        self._buf += s.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._buf:
            linea, self._buf = self._buf.split("\n", 1)
            if linea.strip():
                log(linea.rstrip(), clasificar(linea, self._nivel))
        return len(s)

    def flush(self):
        pass
