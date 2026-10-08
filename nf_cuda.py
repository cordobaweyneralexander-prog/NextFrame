"""
nf_cuda.py - Soporte opcional de GPU NVIDIA.
Las librerias CUDA (cuBLAS + cuDNN, ~1 GB) NO viajan con el programa: solo se descargan
cuando el usuario elige GPU. Se bajan de PyPI (paquetes oficiales de NVIDIA) a bin/cuda/.
Si algo falla, el motor vuelve a CPU automaticamente.
"""
import io
import json
import os
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import nf_comun as C

# AJUSTABLE: ctranslate2 4.x usa CUDA 12 + cuDNN 9. Si tu version de ctranslate2 pide otra, se cambia aqui.
PAQUETES_CUDA = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
DLLS_REQUERIDAS = ["cublas64_12.dll", "cudnn64_9.dll"]


def hay_nvidia():
    """True si hay una GPU NVIDIA con driver (no exige las librerias CUDA)."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and "GPU" in r.stdout
    except Exception:
        return False


def cuda_instalado():
    return all((C.CARPETA_CUDA / d).is_file() for d in DLLS_REQUERIDAS)


def _rueda_windows(paquete):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{paquete}/json", timeout=30) as r:
        datos = json.load(r)
    for f in datos["urls"]:
        if f["filename"].endswith("win_amd64.whl"):
            return f["url"], int(f.get("size") or 0), f["filename"]
    raise RuntimeError(f"PyPI no tiene una rueda de Windows para {paquete}.")


def instalar_cuda(cb=None):
    """Descarga y extrae las DLL en bin/cuda/. cb(fraccion, texto)."""
    if os.name != "nt":
        raise RuntimeError("La descarga automática de CUDA solo está preparada para Windows.")
    ruedas = [_rueda_windows(p) for p in PAQUETES_CUDA]
    total = sum(r[1] for r in ruedas) or 1
    hecho = 0
    C.CARPETA_CUDA.mkdir(parents=True, exist_ok=True)
    for url, tam, nombre in ruedas:
        C.log(f"Descargando {nombre} ({tam / 1e6:.0f} MB)...", "info")
        with tempfile.NamedTemporaryFile(suffix=".whl", delete=False) as tmp:
            ruta_tmp = tmp.name
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(ruta_tmp, "wb") as out:
                while True:
                    bloque = r.read(1 << 20)
                    if not bloque:
                        break
                    out.write(bloque)
                    hecho += len(bloque)
                    if cb:
                        cb(min(hecho / total, 0.99), f"Descargando {nombre[:28]}...")
            with zipfile.ZipFile(ruta_tmp) as z:
                for m in z.namelist():
                    if m.lower().endswith(".dll") and "/bin/" in m:
                        with z.open(m) as src, open(C.CARPETA_CUDA / Path(m).name, "wb") as dst:
                            dst.write(src.read())
        finally:
            try:
                os.remove(ruta_tmp)
            except OSError:
                pass
    faltan = [d for d in DLLS_REQUERIDAS if not (C.CARPETA_CUDA / d).is_file()]
    if faltan:
        raise RuntimeError(f"Tras la descarga faltan: {', '.join(faltan)}. "
                           "Revisa PAQUETES_CUDA / DLLS_REQUERIDAS en nf_cuda.py.")
    C.activar_dll(C.CARPETA_CUDA)
    if cb:
        cb(1.0, "Listo")
    C.log("[OK] Librerías CUDA instaladas en bin/cuda.", "ok")
