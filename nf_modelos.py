"""
nf_modelos.py - Modelos de Whisper en una carpeta que elige el usuario (por defecto modelos/).
Cada modelo vive en su propia subcarpeta: <carpeta_modelos>/<nombre>/model.bin ...
La descarga muestra progreso midiendo los bytes que van llegando a disco.
"""
import os
import shutil
import threading
import time
from collections import namedtuple
from pathlib import Path

import nf_comun as C

Modelo = namedtuple("Modelo", "nombre repo bytes solo_ingles")

# Tamanos aproximados: solo sirven para calcular la barra de progreso.
CATALOGO = [
    Modelo("tiny", "Systran/faster-whisper-tiny", 75_000_000, False),
    Modelo("base", "Systran/faster-whisper-base", 145_000_000, False),
    Modelo("small", "Systran/faster-whisper-small", 484_000_000, False),
    Modelo("medium", "Systran/faster-whisper-medium", 1_530_000_000, False),
    Modelo("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1_620_000_000, False),
    Modelo("large-v3", "Systran/faster-whisper-large-v3", 3_090_000_000, False),
    Modelo("distil-large-v3", "Systran/faster-distil-whisper-large-v3", 1_510_000_000, True),
]
PATRONES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def buscar(nombre):
    for m in CATALOGO:
        if m.nombre == nombre:
            return m
    raise KeyError(f"Modelo desconocido: {nombre}")


def carpeta_modelos():
    return Path(C.cargar_config()["carpeta_modelos"])


def ruta_modelo(nombre):
    return carpeta_modelos() / nombre


def instalado(nombre):
    return (ruta_modelo(nombre) / "model.bin").is_file()


def compatible(nombre, idioma):
    """distil-large-v3 solo transcribe bien en ingles."""
    return (not buscar(nombre).solo_ingles) or idioma == "en"


def tam_texto(m):
    b = m.bytes
    return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def _bytes_en(carpeta):
    total = 0
    for raiz, _, archivos in os.walk(carpeta):
        for a in archivos:
            try:
                total += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return total


def descargar(nombre, cb=None):
    """Descarga el modelo a <carpeta_modelos>/<nombre>. cb(fraccion) se llama cada medio segundo."""
    from huggingface_hub import snapshot_download   # se importa aqui para no frenar el arranque

    m = buscar(nombre)
    destino = ruta_modelo(nombre)
    destino.mkdir(parents=True, exist_ok=True)
    parar = threading.Event()

    def vigilar():
        while not parar.is_set():
            if cb:
                cb(min(_bytes_en(destino) / m.bytes, 0.99))
            time.sleep(0.5)

    hilo = threading.Thread(target=vigilar, daemon=True)
    hilo.start()
    C.log(f"Descargando modelo '{nombre}' ({tam_texto(m)}) en {destino}...", "info")
    try:
        snapshot_download(m.repo, local_dir=str(destino), allow_patterns=PATRONES)
    except Exception as e:
        raise RuntimeError(f"No se pudo descargar el modelo '{nombre}': {type(e).__name__}: {e}")
    finally:
        parar.set()
    if not instalado(nombre):
        raise RuntimeError(f"La descarga de '{nombre}' terminó pero falta model.bin en {destino}.")
    if cb:
        cb(1.0)
    C.log(f"[OK] Modelo '{nombre}' listo.", "ok")


def asegurar_modelo(nombre):
    """Devuelve la ruta del modelo; lo descarga (con progreso en la barra 'Modelo') si no esta."""
    if not instalado(nombre):
        C.progreso("modelo", 0.0, f"Descargando {nombre}")
        descargar(nombre, lambda f: (C.comprobar_cancelacion(),
                                     C.progreso("modelo", f, f"Descargando {nombre}")))
    return ruta_modelo(nombre)


def eliminar(nombre):
    shutil.rmtree(ruta_modelo(nombre), ignore_errors=True)
    C.log(f"Modelo '{nombre}' eliminado.", "info")
