"""
NEXT FRAMES - CORTADOR MAESTRO
Pipeline automatizado para extraccion de material (Video -> MP3 -> Chat -> SRT)
Version 5: nombre por canal, chat en JSON, verificacion de sincronia, modo lista.

Salida en Contenido_Extraido/ (misma raiz para los 4 archivos de un directo):
    <canal>__<AAAAMMDD>_<idvod>.mp4    (para editar en DaVinci)
    <canal>__<AAAAMMDD>_<idvod>.mp3
    <canal>__<AAAAMMDD>_<idvod>.srt
    <canal>__<AAAAMMDD>_<idvod>.json   (chat de Twitch; YouTube: .txt)
    meta/<nombre>.json                 (informe de sincronia; el script de extraccion lo ignora)
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import traceback
import unicodedata
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

import yt_dlp
import faster_whisper
from faster_whisper import WhisperModel, BatchedInferencePipeline

import nf_comun as C
import nf_modelos as M

# ==========================================
# REPORTE DE ETAPAS
# ==========================================
@contextmanager
def etapa(nombre, posibles_causas=None):
    C.log(f"\n>>> {nombre}...")
    try:
        yield
        C.log(f"[OK] {nombre} completado.", "ok")
    except C.Cancelado:
        raise
    except Exception as e:
        lineas = ["!" * 64, f"FALLO EN: {nombre}", f"Error: {type(e).__name__}: {e}"]
        if posibles_causas:
            lineas.append("Posibles causas:")
            lineas += [f"  - {c}" for c in posibles_causas]
        lineas.append("!" * 64)
        C.log("\n".join(lineas), "error")
        C.log(traceback.format_exc(), "traza")
        raise

# ==========================================
# RUTAS PORTABLES Y CONFIGURACION
# (ffmpeg, ffprobe, aria2c y TwitchDownloaderCLI viven en bin/, junto al programa)
# ==========================================
def obtener_ruta_ffmpeg():
    return str(C.BIN)

def _ruta_binario(nombre):
    return C.ruta_binario(nombre)

CARPETA_SALIDA = CARPETA_META = CARPETA_TMP = None

def hay_gpu_disponible():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False

CONFIG = {
    "base_dir": None,
    "idioma": None,
    "modelo_whisper": None,
    "whisper_device": "cpu",
    "whisper_compute": "int8",
    "whisper_batch_size": 1,
    "concurrencia_twitch": 5,
    "concurrencia_youtube": 12,
    "concurrencia_generico": 5,
    # Umbrales de los avisos de sincronia (segundos)
    "tol_duracion_mp3": 1.0,
    "tol_inicio_audio_video": 0.25,
    "tol_duracion_oficial": 2.0,
    "tol_fin_chat": 30.0,
}

_MODELOS_CACHE = {}

def _fijar_dispositivo(gpu):
    CONFIG["whisper_device"] = "cuda" if gpu else "cpu"
    CONFIG["whisper_compute"] = "float16" if gpu else "int8"
    CONFIG["whisper_batch_size"] = 16 if gpu else 1

def _clave_bench():
    return f"{CONFIG['idioma']}:{CONFIG['whisper_device']}"

def configurar(carpeta_salida, idioma, modelo="auto", dispositivo="cpu"):
    """Punto de entrada para la ventana. El idioma es obligatorio (se le pasa a Whisper)."""
    global CARPETA_SALIDA, CARPETA_META, CARPETA_TMP
    if not idioma:
        raise ValueError("Debes elegir el idioma del directo antes de empezar.")
    if modelo and modelo != "auto" and not M.compatible(modelo, idioma):
        raise ValueError(f"El modelo '{modelo}' solo transcribe bien en inglés; elige otro para '{idioma}'.")
    C.preparar_entorno()
    CARPETA_SALIDA = str(carpeta_salida)
    CARPETA_META = os.path.join(CARPETA_SALIDA, "meta")
    CARPETA_TMP = os.path.join(CARPETA_SALIDA, "_tmp")
    for _c in (CARPETA_SALIDA, CARPETA_META, CARPETA_TMP):
        os.makedirs(_c, exist_ok=True)
    CONFIG["base_dir"] = CARPETA_SALIDA
    CONFIG["idioma"] = idioma
    _MODELOS_CACHE.clear()
    gpu = dispositivo == "cuda"
    if gpu and not hay_gpu_disponible():
        C.log("AVISO: no se pudo activar la GPU (CUDA no disponible). Se usa CPU.", "aviso")
        gpu = False
    _fijar_dispositivo(gpu)
    if modelo and modelo != "auto":
        CONFIG["modelo_whisper"] = modelo
    else:
        guardado = C.cargar_config().get("benchmark", {}).get(_clave_bench())
        CONFIG["modelo_whisper"] = guardado["modelo"] if guardado else None

def _detectar_plataforma_descarga(url):
    url_low = url.lower()
    if "twitch.tv" in url_low:
        return "twitch"
    if "youtube.com" in url_low or "youtu.be" in url_low:
        return "youtube"
    return "generico"

def _concurrencia_para(plataforma):
    return {
        "twitch": CONFIG["concurrencia_twitch"],
        "youtube": CONFIG["concurrencia_youtube"],
    }.get(plataforma, CONFIG["concurrencia_generico"])

# ==========================================
# NOMBRES DE ARCHIVO: <canal>__<fecha>_<idvod>
# ==========================================
def limpiar_nombre(texto, minusculas=True):
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9_-]+", "_", t)
    t = re.sub(r"_{2,}", "_", t).strip("_")     # '__' queda reservado como separador
    return t.lower() if minusculas else t

def extraer_video_id_twitch(url):
    m = re.search(r"twitch\.tv/videos/(\d+)", url)
    return m.group(1) if m else None

def obtener_info(url):
    opts = {"quiet": True, "noprogress": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def construir_nombre(info, url, nombre_manual=None):
    if nombre_manual:
        return limpiar_nombre(nombre_manual, minusculas=False)
    canal = info.get("uploader_id") or info.get("channel") or info.get("uploader")
    if not canal:
        raise RuntimeError("No se pudo obtener el nombre del canal desde la URL. Usa --nombre.")
    vid = extraer_video_id_twitch(url) or str(info.get("id") or "").lstrip("v")
    sufijo = "_".join(x for x in (info.get("upload_date") or "", limpiar_nombre(vid, False)) if x)
    canal = limpiar_nombre(canal)
    return f"{canal}__{sufijo}" if sufijo else canal

# ==========================================
# SONDEO Y VERIFICACION DE SINCRONIA
# ==========================================
def sondear(ruta):
    """Duracion de contenedor y start_time/duracion de pista de video y audio (ffprobe)."""
    cmd = [_ruta_binario("ffprobe"), "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,start_time,duration", "-of", "json", ruta]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        d = {}
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    out = {"formato": num((d.get("format") or {}).get("duration"))}
    for s in d.get("streams", []):
        tipo = s.get("codec_type")
        if tipo in ("video", "audio") and tipo not in out:
            out[tipo] = {"inicio": num(s.get("start_time")) or 0.0, "duracion": num(s.get("duration"))}
    return out

def verificar_video_valido(ruta_video):
    cmd = [
        _ruta_binario("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", ruta_video,
    ]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.stdout.strip() != "video":
        raise RuntimeError(f"El archivo descargado no tiene pista de video: {ruta_video}")

def _ultimo_offset_chat_json(ruta_json):
    try:
        with open(ruta_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        comentarios = data.get("comments") or []
        offs = [float(c["content_offset_seconds"]) for c in comentarios if "content_offset_seconds" in c]
        video = data.get("video") if isinstance(data.get("video"), dict) else {}
        return (max(offs) if offs else None), len(comentarios), video
    except Exception:
        return None, 0, {}

def verificar_sincronia(nombre_base, info, ruta_mp4, ruta_mp3, ruta_srt, ruta_chat):
    """Informe no bloqueante: todos los archivos deben compartir el mismo tiempo cero."""
    print("\n[SYNC] Verificando que MP4, MP3, SRT y chat comparten el mismo tiempo cero...")
    mp4, mp3 = sondear(ruta_mp4), sondear(ruta_mp3)
    avisos = []
    oficial = info.get("duration")
    dur_mp4, dur_mp3 = mp4.get("formato"), mp3.get("formato")

    print(f"       duracion oficial (plataforma): {oficial}")
    print(f"       duracion MP4: {dur_mp4} | MP3: {dur_mp3}")
    if dur_mp4 and dur_mp3 and abs(dur_mp4 - dur_mp3) > CONFIG["tol_duracion_mp3"]:
        avisos.append(f"MP3 y MP4 difieren {dur_mp3 - dur_mp4:+.1f} s de duracion")
    if oficial and dur_mp4 and abs(oficial - dur_mp4) > CONFIG["tol_duracion_oficial"]:
        avisos.append(f"El MP4 dura {dur_mp4 - oficial:+.1f} s respecto a la duracion oficial "
                      "(posibles segmentos omitidos: el chat podria quedar desfasado)")
    v, a = mp4.get("video"), mp4.get("audio")
    if v and a:
        print(f"       inicio pista video: {v['inicio']:.3f} s | audio: {a['inicio']:.3f} s")
        if abs(v["inicio"] - a["inicio"]) > CONFIG["tol_inicio_audio_video"]:
            avisos.append(f"Audio y video no arrancan juntos ({a['inicio'] - v['inicio']:+.2f} s)")

    ultimo_srt = None
    if ruta_srt and os.path.exists(ruta_srt):
        with open(ruta_srt, "r", encoding="utf-8") as f:
            marcas = re.findall(r"-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)", f.read())
        if marcas:
            h, m, s, ms = marcas[-1]
            ultimo_srt = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))
            print(f"       ultimo subtitulo termina en: {ultimo_srt:.1f} s")
            if dur_mp3 and ultimo_srt > dur_mp3 + 2:
                avisos.append("El SRT termina despues del audio")

    chat_info = {}
    if ruta_chat and ruta_chat.endswith(".json"):
        ultimo, n_msgs, video = _ultimo_offset_chat_json(ruta_chat)
        chat_info = {"mensajes": n_msgs, "ultimo_offset": ultimo, "video": video}
        print(f"       chat: {n_msgs} mensajes, ultimo en {ultimo} s")
        if ultimo is not None and dur_mp3 and ultimo > dur_mp3 + CONFIG["tol_fin_chat"]:
            avisos.append(f"El chat llega {ultimo - dur_mp3:.0f} s mas alla del audio")
    elif ruta_chat:
        print("       chat en TXT: sin metadatos de tiempo para verificar")

    if avisos:
        for x in avisos:
            print(f"  AVISO SYNC: {x}")
    else:
        print("       [OK] Sin desfases detectados con los umbrales configurados.")

    meta = {"nombre": nombre_base, "canal": info.get("uploader_id") or info.get("uploader"),
            "fecha": info.get("upload_date"), "id": info.get("id"), "duracion_oficial": oficial,
            "mp4": mp4, "mp3": mp3, "ultimo_srt": ultimo_srt, "chat": chat_info,
            "avisos": avisos, "generado": datetime.now(timezone.utc).isoformat()}
    with open(os.path.join(CARPETA_META, f"{nombre_base}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return avisos


# ==========================================
# MODULO 1: INGESTA DE VIDEO (MP4 Y MP3)
# ==========================================
def hook_yt_dlp(d):
    C.comprobar_cancelacion()
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        frac = None
        if total:
            frac = (d.get('downloaded_bytes') or 0) / total
        elif d.get('fragment_count'):
            frac = (d.get('fragment_index') or 0) / d['fragment_count']
        detalle = []
        if frac is not None:
            detalle.append(f"{frac * 100:.0f}%")
        if d.get('speed'):
            detalle.append(f"{d['speed'] / 1e6:.1f} MB/s")
        if d.get('eta'):
            detalle.append(f"faltan {int(d['eta'])} s")
        C.progreso("video", frac, " · ".join(detalle))
    elif d['status'] == 'finished':
        C.progreso("video", 1.0, "Listo")
        print("[1/4] Descarga completada. Procesando video...")

def descargar_media(url, nombre_base, forzar=False):
    ruta_mp4 = os.path.join(CONFIG["base_dir"], f"{nombre_base}.mp4")
    ruta_mp3 = os.path.join(CONFIG["base_dir"], f"{nombre_base}.mp3")
    ruta_ffmpeg = obtener_ruta_ffmpeg()

    if os.path.exists(ruta_mp4) and not forzar:
        print("[1/4] MP4 ya existe; se omite la descarga (usa 'Rehacer' para repetirla).")
        C.progreso("video", 1.0, "Ya descargado")
    else:
        C.progreso("video", None, "Iniciando")
        plataforma = _detectar_plataforma_descarga(url)
        concurrencia = _concurrencia_para(plataforma)
        tiene_aria2c = shutil.which("aria2c") is not None

        ydl_opts = {
            'format': 'best[height<=1080]/bestvideo[height<=1080]+bestaudio/best',
            'merge_output_format': 'mp4',
            'outtmpl': ruta_mp4,
            'ffmpeg_location': ruta_ffmpeg,
            'concurrent_fragment_downloads': concurrencia,
            'retries': 15,
            'fragment_retries': 15,
            'quiet': True,
            'noprogress': True,
            'progress_hooks': [hook_yt_dlp],
        }
        if tiene_aria2c:
            ydl_opts["external_downloader"] = "aria2c"
            ydl_opts["external_downloader_args"] = {"aria2c": ["-x", "10", "-s", "10", "-k", "1M", "--summary-interval=1"]}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except C.Cancelado:
            raise
        except Exception as e:
            raise RuntimeError(f"Fallo critico al descargar video: {str(e)}")

    C.comprobar_cancelacion()
    verificar_video_valido(ruta_mp4)

    if os.path.exists(ruta_mp3) and not forzar:
        print("[1/4] MP3 ya existe; se omite la extraccion.")
        C.progreso("audio", 1.0, "Ya extraído")
    else:
        # El MP3 sale SIEMPRE del MP4 descargado (mismo tiempo cero), nunca de una descarga aparte.
        print("[1/4] Extrayendo pista de audio (MP3)...")
        C.progreso("audio", None, "Extrayendo")
        tmp = os.path.join(CARPETA_TMP, f"{nombre_base}.mp3")
        cmd = [_ruta_binario("ffmpeg"), "-y", "-i", ruta_mp4, "-vn", "-acodec", "libmp3lame",
               "-q:a", "2", "-f", "mp3", tmp]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.replace(tmp, ruta_mp3)     # atomico: un MP3 a medias nunca queda con el nombre final
        C.progreso("audio", 1.0, "Listo")
    return ruta_mp4, ruta_mp3

# ==========================================
# MODULO 2: INGESTA DE CHAT (JSON TWITCH + FALLBACK YOUTUBE)
# ==========================================
def _vigilar_chat(proc, ruta_log):
    """Lee el porcentaje que escribe TwitchDownloaderCLI en su log y lo lleva a la barra 'Chat'."""
    patron = re.compile(r"(\d{1,3})%")
    def _hilo():
        while proc.poll() is None:
            try:
                with open(ruta_log, "r", encoding="utf-8", errors="replace") as f:
                    m = patron.findall(f.read()[-400:])
                if m:
                    C.progreso("chat", min(int(m[-1]), 100) / 100, f"{min(int(m[-1]), 100)}%")
            except OSError:
                pass
            time.sleep(1)
    threading.Thread(target=_hilo, daemon=True).start()

def iniciar_chat_twitch(url, nombre_base, forzar=False):
    """Lanza TwitchDownloaderCLI en segundo plano (se solapa con la transcripcion)."""
    ruta_json = os.path.join(CONFIG["base_dir"], f"{nombre_base}.json")
    if os.path.exists(ruta_json) and not forzar:
        print("[2/4] Chat JSON ya existe; se omite la descarga.")
        C.progreso("chat", 1.0, "Ya descargado")
        return {"proc": None, "final": ruta_json, "tmp": None, "log": None}

    video_id = extraer_video_id_twitch(url)
    if not video_id:
        print("[2/4] Advertencia: No se pudo extraer el ID del VOD de Twitch.")
        C.progreso("chat", 0.0, "Sin ID de VOD")
        return None
    cli_path = _ruta_binario("TwitchDownloaderCLI")
    if not os.path.exists(cli_path):
        print(f"[2/4] ERROR: Falta el binario {cli_path}.")
        C.progreso("chat", 0.0, "Falló: falta el programa")
        return None

    tmp = os.path.join(CARPETA_TMP, f"{nombre_base}.json")
    log = os.path.join(CARPETA_TMP, f"{nombre_base}_chat.log")
    print(f"[2/4] Descargando chat en segundo plano (TwitchDownloaderCLI, VOD {video_id})...")
    C.progreso("chat", None, "Descargando")
    # --embed-images: incluye la lista de emotes de terceros (BTTV/FFZ/7TV) para poder contarlos.
    cmd = [cli_path, "chatdownload", "--id", video_id, "--embed-images", "-o", tmp]
    fh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    _vigilar_chat(proc, log)
    return {"proc": proc, "final": ruta_json, "tmp": tmp, "log": log, "fh": fh}

def finalizar_chat_twitch(tarea):
    if not tarea:
        return None
    proc = tarea.get("proc")
    if proc is not None:
        while proc.poll() is None:          # espera cancelable (antes: proc.wait())
            C.comprobar_cancelacion()
            time.sleep(0.5)
        codigo = proc.returncode
        tarea["fh"].close()
        if codigo != 0 or not os.path.exists(tarea["tmp"]):
            print(f"[2/4] TwitchDownloaderCLI fallo (codigo {codigo}). Revisa: {tarea['log']}")
            print("      (el VOD podria ser solo para subs o no tener replay de chat)")
            C.progreso("chat", 0.0, "Falló")
            return None
        os.replace(tarea["tmp"], tarea["final"])
    _, n, _ = _ultimo_offset_chat_json(tarea["final"])
    if n == 0:
        print("[2/4] ADVERTENCIA: el JSON del chat no contiene comentarios.")
        C.progreso("chat", 0.0, "Falló: chat vacío")
        return None
    print(f"[2/4] Chat de Twitch listo: {n} comentarios ({os.path.basename(tarea['final'])}).")
    C.progreso("chat", 1.0, f"{n} comentarios")
    return tarea["final"]


def cancelar_chat_twitch(tarea):
    if tarea and tarea.get("proc") is not None and tarea["proc"].poll() is None:
        tarea["proc"].kill()
        tarea["fh"].close()

def _hms(segundos):
    s = int(max(segundos, 0))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

def descargar_chat_youtube(url, nombre_base):
    ruta_txt = os.path.join(CONFIG["base_dir"], f"{nombre_base}.txt")
    ruta_json_base = os.path.join(CONFIG["base_dir"], f"{nombre_base}_chat")

    ydl_opts_chat = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['all', 'live_chat'],
        'outtmpl': f"{ruta_json_base}.%(ext)s",
        'quiet': True,
        'noprogress': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts_chat) as ydl:
            ydl.download([url])
    except Exception:
        return None

    json_file = None
    for file in os.listdir(CONFIG["base_dir"]):
        if file.startswith(f"{nombre_base}_chat") and (file.endswith(".json") or file.endswith(".jsonl")):
            json_file = os.path.join(CONFIG["base_dir"], file)
            break
    if not json_file:
        return None

    print("      Formateando comentarios de YouTube a archivo TXT...")
    mensajes_guardados = 0
    sin_offset = 0
    try:
        with open(json_file, 'r', encoding='utf-8') as fj:
            contenido = fj.read().strip()
        with open(ruta_txt, 'w', encoding='utf-8') as ft:
            lineas = json.loads(contenido) if contenido.startswith('[') else contenido.split('\n')
            for item in lineas:
                if not item:
                    continue
                try:
                    msg = item if isinstance(item, dict) else json.loads(item)
                    autor = msg.get("author", "Desconocido")
                    if isinstance(autor, dict):
                        autor = autor.get("name", "Desconocido")
                    texto = msg.get("message", msg.get("text", msg.get("body", "")))
                    # Solo se acepta el OFFSET respecto al inicio del video. La hora de reloj
                    # ('timestamp') no sirve para sincronizar con el audio.
                    rel = msg.get("time_in_seconds")
                    if rel is None:
                        sin_offset += 1
                        continue
                    if not texto:
                        continue
                    ft.write(f"[{_hms(float(rel))}] {autor}: {texto}\n")
                    mensajes_guardados += 1
                except Exception:
                    continue
        os.remove(json_file)
        if mensajes_guardados == 0:
            print(f"[2/4] ADVERTENCIA: chat de YouTube sin offsets de video utilizables "
                  f"({sin_offset} mensajes descartados). No se genero TXT.")
            if os.path.exists(ruta_txt):
                os.remove(ruta_txt)
            return None
        print(f"[2/4] Chat de YouTube extraido. {mensajes_guardados} comentarios guardados.")
        return ruta_txt
    except Exception:
        if json_file and os.path.exists(json_file):
            os.remove(json_file)
        return None

# ==========================================
# MODULO 3: INYECCION DINAMICA DE VAD Y SRT
# ==========================================
def preparar_vad():
    """El archivo VAD viaja con el programa (dentro de faster_whisper/assets o en vad/).
    Solo intenta descargarlo si no esta en ningun lado."""
    assets_dir = os.path.join(os.path.dirname(faster_whisper.__file__), "assets")
    vad_path = os.path.join(assets_dir, "silero_vad_v6.onnx")
    if os.path.exists(vad_path):
        return
    os.makedirs(assets_dir, exist_ok=True)
    local = C.BASE / "vad" / "silero_vad_v6.onnx"
    if local.exists():
        shutil.copyfile(local, vad_path)
        return
    print("[!] AVISO: no se encontro el archivo VAD incluido; se intentara descargar (requiere internet).")
    url_primaria = "https://github.com/SYSTRAN/faster-whisper/raw/master/faster_whisper/assets/silero_vad_v6.onnx"
    try:
        urllib.request.urlretrieve(url_primaria, vad_path)
    except Exception:
        urllib.request.urlretrieve("https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/silero_vad_v6.onnx", vad_path)

def _obtener_modelo(nombre, guardar):
    clave = (nombre, CONFIG['whisper_device'])
    if clave in _MODELOS_CACHE:
        return _MODELOS_CACHE[clave]
    ruta = M.asegurar_modelo(nombre)                 # descarga con barra si hace falta
    C.progreso("modelo", None, "Cargando modelo")
    modelo = WhisperModel(str(ruta), device=CONFIG['whisper_device'], compute_type=CONFIG['whisper_compute'])
    C.progreso("modelo", 1.0, "Modelo listo")
    if guardar:                     # el benchmark no cachea los modelos candidatos
        _MODELOS_CACHE[clave] = modelo
    return modelo

def fmt_srt(t):
    """HH:MM:SS,mmm redondeando en milisegundos (evita '00:00:60,000')."""
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

_etapa_progreso = "transcripcion"     # el benchmark lo cambia a "benchmark"

def _es_error_cuda(e):
    t = f"{type(e).__name__} {e}".lower()
    return any(k in t for k in ("cuda", "cublas", "cudnn", "cudart", ".dll", "gpu", "out of memory", "driver"))

def _transcribir(ruta_audio, ruta_srt, nombre, guardar, es_cpu):
    model = _obtener_modelo(nombre, guardar)
    kw = dict(beam_size=5, vad_filter=True)
    if CONFIG["idioma"]:
        kw["language"] = CONFIG["idioma"]      # antes Whisper lo adivinaba solo
    if es_cpu:
        # condition_on_previous_text=False corta los bucles de repeticion y la deriva de
        # marcas de tiempo que arrastra el texto previo. No cambia modelo, beam ni VAD.
        segments, info = model.transcribe(ruta_audio, condition_on_previous_text=False, **kw)
    else:
        batched_model = BatchedInferencePipeline(model=model)
        segments, info = batched_model.transcribe(ruta_audio, batch_size=CONFIG['whisper_batch_size'], **kw)

    duracion_total = max(float(info.duration or 0), 1.0)
    tmp = os.path.join(CARPETA_TMP, os.path.basename(ruta_srt))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, start=1):
                C.comprobar_cancelacion()
                f.write(f"{i}\n")
                f.write(f"{fmt_srt(segment.start)} --> {fmt_srt(segment.end)}\n")
                f.write(f"{segment.text.strip()}\n\n")
                frac = min(segment.end / duracion_total, 1.0)
                C.progreso(_etapa_progreso, frac, f"{frac * 100:.0f}%")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, ruta_srt)       # atomico: un SRT a medias nunca queda con el nombre final

def generar_srt_cpu(ruta_audio, ruta_srt, modelo_forzado=None):
    preparar_vad()
    modelo_a_usar = modelo_forzado if modelo_forzado else CONFIG['modelo_whisper']
    while True:
        es_cpu = CONFIG['whisper_device'] == 'cpu'
        modo_batch = "Desactivado (Secuencial)" if es_cpu else CONFIG['whisper_batch_size']
        print(f"\n[3/4] Transcribiendo audio con Faster-Whisper '{modelo_a_usar}' "
              f"(idioma={CONFIG['idioma']}, device={CONFIG['whisper_device']}, batch_size={modo_batch})...")
        try:
            _transcribir(ruta_audio, ruta_srt, modelo_a_usar, modelo_forzado is None, es_cpu)
            return
        except C.Cancelado:
            raise
        except Exception as e:
            if es_cpu or not _es_error_cuda(e):
                raise
            # Equipo con NVIDIA pero sin librerias CUDA utilizables: se prueba y se vuelve a CPU.
            C.log(f"AVISO: la GPU fallo ({type(e).__name__}: {e}). Se continua con CPU.", "aviso")
            _MODELOS_CACHE.clear()
            _fijar_dispositivo(False)


# ==========================================
# MODULO 4: AUTOCALIBRACION (PRIMERA VEZ)
# ==========================================
def _candidatos_benchmark(idioma):
    # distil-large-v3 esta pensado para ingles: solo compite si el idioma es ingles.
    return ["base", "small", "distil-large-v3" if idioma == "en" else "large-v3-turbo"]

def auto_benchmark(ruta_mp3):
    global _etapa_progreso
    print("\n" + "=" * 60)
    print(f" CALIBRANDO HARDWARE PARA EL IDIOMA '{CONFIG['idioma']}'")
    print(" (Se hace una vez por idioma; puedes repetirlo con 'Recalibrar')")
    print("=" * 60)

    ruta_mp3_recortado = os.path.join(CONFIG["base_dir"], "benchmark_temp.mp3")
    cmd_recorte = [_ruta_binario("ffmpeg"), "-y", "-i", ruta_mp3, "-t", "120", "-c", "copy", ruta_mp3_recortado]
    subprocess.run(cmd_recorte, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    modelos_candidatos = _candidatos_benchmark(CONFIG["idioma"])
    UMBRAL_TOLERANCIA = 1.6
    tiempos_registro = {}
    _etapa_progreso = "benchmark"
    try:
        for i, modelo in enumerate(modelos_candidatos, start=1):
            C.comprobar_cancelacion()
            print(f"\nEvaluando velocidad del modelo: {modelo.upper()}")
            ruta_srt_temp = os.path.join(CONFIG["base_dir"], f"temp_{modelo}.srt")
            try:
                # La descarga NO cuenta en la medicion (antes distorsionaba la primera prueba).
                M.asegurar_modelo(modelo)
                inicio = time.time()
                generar_srt_cpu(ruta_mp3_recortado, ruta_srt_temp, modelo_forzado=modelo)
                tiempo_total = time.time() - inicio
                tiempos_registro[modelo] = tiempo_total
                print(f"Completado en {tiempo_total:.2f} segundos.")
            except C.Cancelado:
                raise
            except Exception as e:
                # Antes el error se silenciaba: ahora queda en el registro.
                C.log(f"ERROR: el modelo {modelo} fallo en el benchmark: {type(e).__name__}: {e}", "error")
                C.log(traceback.format_exc(), "traza")
                tiempos_registro[modelo] = float('inf')
            finally:
                if os.path.exists(ruta_srt_temp):
                    os.remove(ruta_srt_temp)
    finally:
        _etapa_progreso = "transcripcion"
        if os.path.exists(ruta_mp3_recortado):
            os.remove(ruta_mp3_recortado)

    tiempos_validos = {m: t for m, t in tiempos_registro.items() if t != float('inf')}

    if not tiempos_validos:
        ganador = "small"
    else:
        tiempo_minimo = min(tiempos_validos.values())
        limite_aceptable = tiempo_minimo * UMBRAL_TOLERANCIA
        ganador = None
        for modelo in reversed(modelos_candidatos):
            if modelo in tiempos_validos and tiempos_validos[modelo] <= limite_aceptable:
                ganador = modelo
                break
        if ganador is None:
            ganador = min(tiempos_validos, key=tiempos_validos.get)

    print("\n" + "=" * 60)
    print(f"CONFIGURACION GUARDADA: modelo '{ganador.upper()}' para '{CONFIG['idioma']}'")
    print("=" * 60)

    cfg = C.cargar_config()
    cfg.setdefault("benchmark", {})[_clave_bench()] = {
        "modelo": ganador, "compute": CONFIG["whisper_compute"], "batch_size": CONFIG["whisper_batch_size"]}
    C.guardar_config(cfg)
    return ganador


# ==========================================
# MODULO 5: PIPELINE FINAL
# ==========================================
def pipeline_produccion(url, nombre_manual=None, forzar=False):
    if not CONFIG["idioma"]:
        raise RuntimeError("Falta el idioma. Llama a configurar() antes de procesar.")
    C.comprobar_cancelacion()
    with etapa("Lectura de metadatos del VOD", posibles_causas=["URL invalida", "VOD privado, solo para subs o eliminado", "Sin conexion a internet"]):
        info = obtener_info(url)
        nombre_base = construir_nombre(info, url, nombre_manual)
    print(f"      Nombre base: {nombre_base}")

    with etapa("Descarga de video y audio", posibles_causas=["URL invalida o VOD eliminado", "Falta ffmpeg.exe en la carpeta bin/"]):
        ruta_mp4, ruta_mp3 = descargar_media(url, nombre_base, forzar)

    # El chat se descarga en segundo plano mientras Whisper trabaja.
    tarea_chat = None
    ruta_chat = None
    with etapa("Inicio de descarga de chat", posibles_causas=["El streamer desactivo el replay del chat", "Falta TwitchDownloaderCLI.exe en la carpeta bin/"]):
        if _detectar_plataforma_descarga(url) == "twitch":
            tarea_chat = iniciar_chat_twitch(url, nombre_base, forzar)
        else:
            ruta_chat = descargar_chat_youtube(url, nombre_base)
            C.progreso("chat", 1.0 if ruta_chat else 0.0, "Listo" if ruta_chat else "Sin chat")

    ruta_srt = os.path.join(CONFIG["base_dir"], f"{nombre_base}.srt")
    try:
        if CONFIG["modelo_whisper"] is None:
            with etapa("Auto-calibracion de hardware", posibles_causas=["Sin internet para descargar los modelos candidatos", "Poco espacio en la carpeta de modelos"]):
                CONFIG["modelo_whisper"] = auto_benchmark(ruta_mp3)
        if os.path.exists(ruta_srt) and not forzar:
            print("\n[3/4] SRT ya existe; se omite la transcripcion.")
            C.progreso("transcripcion", 1.0, "Ya existe")
        else:
            with etapa("Transcripcion (Whisper)", posibles_causas=["Archivo de audio vacio o corrupto", "Modelo no descargado y sin internet", "GPU sin librerias CUDA (se reintenta en CPU)"]):
                generar_srt_cpu(ruta_mp3, ruta_srt)
            C.progreso("transcripcion", 1.0, "Listo")
    except BaseException:
        cancelar_chat_twitch(tarea_chat)
        raise

    if tarea_chat is not None:
        try:
            with etapa("Cierre de descarga de chat"):
                ruta_chat = finalizar_chat_twitch(tarea_chat)
        except C.Cancelado:
            cancelar_chat_twitch(tarea_chat)
            raise

    with etapa("Verificacion de sincronia"):
        verificar_sincronia(nombre_base, info, ruta_mp4, ruta_mp3, ruta_srt, ruta_chat)

    print("\n[4/4] FASE DE EXTRACCION COMPLETADA.")
    print(f"Archivos en {CONFIG['base_dir']}:")
    for ext in (".mp4", ".mp3", ".srt", ".json", ".txt"):
        p = os.path.join(CONFIG["base_dir"], f"{nombre_base}{ext}")
        if os.path.exists(p):
            print(f"   OK  {nombre_base}{ext}  ({os.path.getsize(p) / 1e6:.1f} MB)")
    if not ruta_chat:
        print("   FALTA el chat: el script de extraccion marcara este directo como incompleto.")
    print("Recuerda: exporta el EDL de marcadores con EXACTAMENTE este nombre base.")
    return nombre_base

def procesar_lista(urls, nombre_manual=None, forzar=False, al_empezar=None, al_terminar=None):
    """Sustituye al antiguo main()/argparse: la ventana llama a esta funcion.
    al_empezar(i, url) y al_terminar(i, ok, mensaje) permiten pintar cada elemento."""
    ok, errores = [], []
    for i, u in enumerate(urls):
        if al_empezar:
            al_empezar(i, u)
        print("\n" + "#" * 64 + f"\n# VOD {i + 1}/{len(urls)}: {u}\n" + "#" * 64)
        try:
            ok.append(pipeline_produccion(u, nombre_manual if len(urls) == 1 else None, forzar))
        except C.Cancelado:
            C.log("AVISO: proceso cancelado por el usuario.", "aviso")
            if al_terminar:
                al_terminar(i, False, "Cancelado")
            break
        except Exception as e:
            errores.append((u, f"{type(e).__name__}: {e}"))
            C.log(f"Este VOD se detuvo: {type(e).__name__}: {e}", "error")
            if al_terminar:
                al_terminar(i, False, f"{type(e).__name__}: {e}")
            continue
        if al_terminar:
            al_terminar(i, True, "Listo")
    if len(urls) > 1 or errores:
        print("\n=== RESUMEN DEL LOTE ===")
        print(f"OK: {len(ok)} | Con error: {len(errores)}")
        for u, m in errores:
            print(f"  X {u}: {m}")
    return ok, errores
