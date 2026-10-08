#!/usr/bin/env python3
"""
motor_extraer.py - Extraccion de dataset, convertida en funciones que la ventana puede llamar.
La logica de calculo (audio, SRT, chat, EDL, CSV) es identica a extraer_dataset.py: no se toco.
Lo que cambia es la entrada/salida: en vez de argparse + sys.exit, hay listar_directos() para
mostrar la tabla de la pantalla 2 antes de ejecutar, y procesar_lote() para la ventana.
"""
import csv, json, math, re, subprocess, sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

import nf_comun as C

# ----------------------------------------------------------------------------
# CONFIGURACION FIJA
# ----------------------------------------------------------------------------
CARPETA_DEFECTO = r"C:\Kamiguiri\dist\Contenido_Extraido"
FFMPEG_DEFECTO = r"C:\ffmpeg-8.1.1-essentials_build\bin"
EXT_OBLIGATORIAS = [".mp3", ".srt", ".edl"]
EXT_CHAT = [".json", ".txt"]              # .json tiene prioridad
EXTENSIONES = EXT_OBLIGATORIAS + EXT_CHAT
FPS = 60
COLOR_A_NIVEL = {
    "Pink": 0,    # housekeeping: inicio/fin, sponsors, donaciones, sorteos
    "Cyan": 1,    # charla aburrida / lobby
    "Blue": 2,    # charla entretenida
    "Green": 3,   # inicio de momentos tensos
    "Lemon": 4,   # momento entretenido por explosion emocional / bizarro
    "Yellow": 5,  # highlight
    "Red": 6,     # explosion emocional tras tension construida
}
SR = 16000            # frecuencia de muestreo para medir audio
DB_PISO = -90.0       # piso para silencio digital
MAX_PALABRAS_SEG = 10  # cue SRT por encima de esto = alucinacion de Whisper (se descarta)

RE_SISTEMA = re.compile(
    r"^(subscribed|resubscribed|watched \d+ consecutive|is continuing|"
    r"is gifting|gifted|converted|is paying forward)", re.I)
RE_CHAT = re.compile(r"^\[(\d+):(\d\d):(\d\d)\] ([^:]+): (.*)$")
RE_SRT_T = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")
RE_EDL_EVENTO = re.compile(
    r"^\d+\s+\S+\s+\S+\s+\S+\s+(\d\d:\d\d:\d\d:\d\d)\s+(\d\d:\d\d:\d\d:\d\d)\s+"
    r"(\d\d:\d\d:\d\d:\d\d)\s+(\d\d:\d\d:\d\d:\d\d)")
RE_EDL_MARCA = re.compile(r"\|C:ResolveColor(\w+)\s+\|M:(.*?)\s+\|D:(\d+)")
RE_EMOTE_CAND = re.compile(r"^(?=.*[a-z0-9][A-Z])[A-Za-z0-9]{4,}$")

class ErrorDirecto(Exception):
    """Error que invalida un directo pero no detiene el lote."""


def fail(msg):
    raise ErrorDirecto(msg)

def tc_a_seg(tc):
    h, m, s, f = (int(x) for x in tc.split(":"))
    if f >= FPS:
        fail(f"Timecode {tc} tiene frame {f} >= {FPS}. El proyecto no es de {FPS} fps.")
    return h * 3600 + m * 60 + s + f / FPS


# ----------------------------------------------------------------------------
# 1. EDL -> segmentos
# ----------------------------------------------------------------------------
def leer_marcadores(edl_path):
    """Devuelve marcadores ordenados, con tiempos crudos (sin restar inicio de timeline)."""
    marcadores, ultimo_tc = [], None
    for linea in Path(edl_path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = RE_EDL_EVENTO.match(linea.strip())
        if m:
            ultimo_tc = m.group(3)  # record-in
            continue
        m = RE_EDL_MARCA.search(linea)
        if m and ultimo_tc:
            color, nombre, dur = m.group(1), m.group(2), int(m.group(3))
            if dur > 1:
                print(f"  AVISO: marcador '{nombre}' tiene duracion {dur} frames; "
                      "se usa solo su punto de inicio.")
            marcadores.append({"t": tc_a_seg(ultimo_tc), "color": color,
                               "nombre": nombre, "tc": ultimo_tc})
    if not marcadores:
        fail("El EDL no contiene marcadores (lineas '|C:ResolveColor... |M:... |D:'). "
             "Exporta con Timelines > Export > Timeline Markers to .EDL.")
    desconocidos = {m["color"] for m in marcadores} - set(COLOR_A_NIVEL)
    if desconocidos:
        fail(f"Colores sin nivel asignado: {sorted(desconocidos)}. "
             f"Validos: {sorted(COLOR_A_NIVEL)}.")
    return sorted(marcadores, key=lambda x: x["t"])


def emparejar(marcadores):
    """Empareja marcadores del mismo color en orden cronologico: (1,2), (3,4)..."""
    por_color = defaultdict(list)
    for m in marcadores:
        por_color[m["color"]].append(m)
    segmentos = []
    for color, lista in por_color.items():
        if len(lista) % 2:
            ult = lista[-1]
            fail(f"El color {color} tiene {len(lista)} marcadores (numero impar). "
                 f"Falta el par de '{ult['nombre']}' en {ult['tc']}.")
        for a, b in zip(lista[0::2], lista[1::2]):
            if b["t"] <= a["t"]:
                fail(f"Par de {color} con inicio >= fin ({a['tc']} / {b['tc']}).")
            segmentos.append({"inicio": round(a["t"], 4), "fin": round(b["t"], 4),
                              "color": color, "nivel": COLOR_A_NIVEL[color],
                              "marcador_inicio": a["nombre"], "marcador_fin": b["nombre"]})
    segmentos.sort(key=lambda s: s["inicio"])
    for s1, s2 in zip(segmentos, segmentos[1:]):
        if s2["inicio"] < s1["fin"] - 1e-6:
            fail(f"Segmentos solapados: {s1['color']} ({s1['inicio']}-{s1['fin']} s) y "
                 f"{s2['color']} ({s2['inicio']}-{s2['fin']} s). Probable par mal colocado.")
    return segmentos


# ----------------------------------------------------------------------------
# 2. Audio (ffmpeg -> numpy, un segundo a la vez)
# ----------------------------------------------------------------------------
def extraer_audio(mp3, ffmpeg):
    cmd = [ffmpeg, "-v", "error", "-i", str(mp3), "-vn", "-ac", "1",
           "-ar", str(SR), "-f", "f32le", "-"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    except FileNotFoundError:
        fail(f"No se encontro ffmpeg en: {ffmpeg}")
    rms_db, pico_db = [], []
    n_bytes = SR * 4
    while True:
        buf = p.stdout.read(n_bytes)
        if not buf:
            break
        x = np.frombuffer(buf[: len(buf) // 4 * 4], dtype=np.float32)
        if x.size == 0:
            break
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        pico = float(np.max(np.abs(x)))
        rms_db.append(max(20 * math.log10(rms), DB_PISO) if rms > 0 else DB_PISO)
        pico_db.append(max(20 * math.log10(pico), DB_PISO) if pico > 0 else DB_PISO)
    p.wait()
    if p.returncode != 0 or not rms_db:
        fail("ffmpeg no pudo decodificar el audio.")
    rms_db = np.array(rms_db)
    delta = np.concatenate([[0.0], np.diff(rms_db)])
    return rms_db, np.array(pico_db), delta


# ----------------------------------------------------------------------------
# 3. SRT -> palabras por segundo
# ----------------------------------------------------------------------------
def extraer_srt(path, n, offset):
    palabras = np.zeros(n)
    descartados = []
    txt = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    for bloque in re.split(r"\n\s*\n", txt.replace("\r\n", "\n")):
        lineas = bloque.strip().split("\n")
        for i, l in enumerate(lineas):
            m = RE_SRT_T.search(l)
            if m:
                g = m.groups()
                a = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")) / 1000
                b = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000
                a, b = a + offset, b + offset
                texto = re.sub(r"<[^>]+>", "", " ".join(lineas[i + 1:]))
                w = len(re.findall(r"\w+", texto, re.UNICODE))
                dur = max(b - a, 1e-3)
                if w / dur > MAX_PALABRAS_SEG:
                    descartados.append((round(a), w, texto.strip()[:30]))
                    break
                for s in range(max(int(a), 0), min(int(b) + 1, n)):
                    ov = min(b, s + 1) - max(a, s)
                    if ov > 0:
                        palabras[s] += w * ov / dur
                    elif b - a <= 1e-3 and s == int(a):
                        palabras[s] += w
                break
    return palabras, descartados


# ----------------------------------------------------------------------------
# 4. Chat TXT -> metricas por segundo
# ----------------------------------------------------------------------------
def extraer_chat(path, n, offset, emotes):
    msgs = np.zeros(n); caps = np.zeros(n); letras = np.zeros(n)
    subs = np.zeros(n); emo = np.zeros(n)
    usuarios = [set() for _ in range(n)]
    cand = Counter()
    fuera = malas = 0
    for linea in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not linea.strip():
            continue
        m = RE_CHAT.match(linea)
        if not m:
            malas += 1
            continue
        t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(offset)
        user, texto = m[4], m[5]
        if t < 0 or t >= n:
            fuera += 1
            continue
        if texto.startswith(user + " ") and RE_SISTEMA.match(texto[len(user) + 1:]):
            subs[t] += 1
            continue
        msgs[t] += 1
        usuarios[t].add(user)
        letras_msg = [c for c in texto if c.isalpha()]
        letras[t] += len(letras_msg)
        caps[t] += sum(c.isupper() for c in letras_msg)
        for tok in texto.split():
            if emotes is not None and tok in emotes:
                emo[t] += 1
            if RE_EMOTE_CAND.match(tok):
                cand[tok] += 1
    ratio = np.divide(caps, letras, out=np.zeros(n), where=letras > 0)
    unicos = np.array([len(u) for u in usuarios], dtype=float)
    if fuera:
        print(f"  AVISO: {fuera} mensajes de chat fuera del rango del audio (descartados).")
    if malas:
        print(f"  AVISO: {malas} lineas del chat sin formato '[H:MM:SS] usuario: mensaje'.")
    return msgs, unicos, ratio, subs, (emo if emotes is not None else None), cand


def extraer_chat_json(path, n, offset, emotes_extra):
    """Chat en JSON de TwitchDownloaderCLI (chatdownload --embed-images -o x.json)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"No se pudo leer el JSON del chat: {type(e).__name__}: {e}")
    if not isinstance(data, dict) or "comments" not in data:
        fail(f"El JSON no tiene la clave 'comments'. Claves de primer nivel: "
             f"{list(data)[:10] if isinstance(data, dict) else type(data).__name__}")
    comentarios = data["comments"]
    if not comentarios:
        fail("El JSON del chat no tiene comentarios.")
    # emotes de terceros (BTTV/FFZ/7TV): solo existen con --embed-images
    emb = data.get("embeddedData") or {}
    terceros = {e.get("name") for e in (emb.get("thirdParty") or []) if e.get("name")}
    terceros |= emotes_extra or set()
    if not emb.get("thirdParty"):
        print("  AVISO: el JSON no trae emotes de terceros (BTTV/FFZ/7TV). Descargalo con "
              "--embed-images para contarlos; solo se cuentan los emotes nativos de Twitch.")
    c0 = comentarios[0]
    if "content_offset_seconds" not in c0:
        fail(f"Comentario sin 'content_offset_seconds'. Claves encontradas: {list(c0)}")

    msgs = np.zeros(n); caps = np.zeros(n); letras = np.zeros(n)
    subs = np.zeros(n); emo = np.zeros(n); bits = np.zeros(n)
    usuarios = [set() for _ in range(n)]
    fuera = 0
    for c in comentarios:
        t = int(math.floor(float(c["content_offset_seconds"]) + offset))
        if t < 0 or t >= n:
            fuera += 1
            continue
        msg = c.get("message") or {}
        cuerpo = msg.get("body") or ""
        cm = c.get("commenter") or {}
        user = cm.get("display_name") or cm.get("name") or str(cm.get("_id", ""))
        aviso = msg.get("user_notice_params") or {}
        es_sistema = bool(aviso.get("msg-id")) or (
            cuerpo.startswith(user + " ") and RE_SISTEMA.match(cuerpo[len(user) + 1:]))
        if es_sistema:
            subs[t] += 1
            continue
        msgs[t] += 1
        usuarios[t].add(user)
        bits[t] += float(msg.get("bits_spent") or 0)
        letras_msg = [ch for ch in cuerpo if ch.isalpha()]
        letras[t] += len(letras_msg)
        caps[t] += sum(ch.isupper() for ch in letras_msg)
        frags = msg.get("fragments")
        if frags:
            for fr in frags:
                if fr.get("emoticon"):
                    emo[t] += 1                       # emote nativo de Twitch
                elif terceros:
                    emo[t] += sum(tok in terceros for tok in (fr.get("text") or "").split())
        elif terceros:
            emo[t] += sum(tok in terceros for tok in cuerpo.split())
    ratio = np.divide(caps, letras, out=np.zeros(n), where=letras > 0)
    unicos = np.array([len(u) for u in usuarios], dtype=float)
    if fuera:
        print(f"  AVISO: {fuera} mensajes de chat fuera del rango del audio (descartados).")
    return msgs, unicos, ratio, subs, emo, bits, Counter()


def _lag_corr(a, b, max_lag):
    n = len(a); res = []
    for L in range(-max_lag, max_lag + 1):
        x = a[max(0, -L): n - max(0, L)]; y = b[max(0, L): n - max(0, -L)]
        if x.std() > 0 and y.std() > 0:
            res.append((float(np.corrcoef(x, y)[0, 1]), L))
    return sorted(res, reverse=True)


def diagnostico_sync(db, delta, palabras, msgs):
    """Informativo: desfase entre audio y (SRT | chat). No modifica datos."""
    k = np.ones(15) / 15
    suav = lambda x: np.convolve(x, k, mode="same")
    hab = db > DB_PISO + 0.1
    r = _lag_corr(np.where(hab, db, db[hab].mean() if hab.any() else 0), palabras, 10)
    if r:
        print(f"  Sync audio~SRT: mejor desfase {r[0][1]:+d} s (r={r[0][0]:.2f})")
    r = _lag_corr(suav(np.abs(delta)), suav(msgs), 30)
    if r:
        r0 = [c for c, L in r if L == 0][0]
        print(f"  Sync audio~chat: mejor desfase {r[0][1]:+d} s (r={r[0][0]:.2f}); en 0 s r={r0:.2f}. "
              "Valores bajos = alineacion no verificable con esta metrica.")


# ----------------------------------------------------------------------------
# Procesar un directo
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Deteccion de directos en una carpeta (pantalla 2, antes de ejecutar)
# ----------------------------------------------------------------------------
def listar_directos(carpeta):
    """Devuelve [{'stem':..., 'archivos':{'.mp3':bool,...}, 'completo':bool}, ...], sin tocar disco
    mas alla de listar. Usado por la ventana para pintar la tabla antes de ejecutar."""
    carpeta = Path(carpeta)
    if not carpeta.is_dir():
        raise ValueError(f"No existe la carpeta: {carpeta}")
    grupos = defaultdict(dict)
    for p in carpeta.iterdir():
        if p.is_file() and p.suffix.lower() in EXTENSIONES:
            grupos[p.stem][p.suffix.lower()] = p
    filas = []
    for stem in sorted(grupos):
        rutas = grupos[stem]
        archivos = {e: (e in rutas) for e in EXT_OBLIGATORIAS}
        archivos["chat"] = any(e in rutas for e in EXT_CHAT)
        filas.append({"stem": stem, "rutas": rutas, "archivos": archivos,
                     "completo": all(archivos.values())})
    return filas


class _Args:
    """Reemplaza al 'a' de argparse dentro de procesar(): mismos nombres de atributo."""
    def __init__(self, carpeta, timeline_start=None, chat_offset=0.0, srt_offset=0.0):
        self.carpeta = str(carpeta)
        self.timeline_start = timeline_start
        self.chat_offset = chat_offset
        self.srt_offset = srt_offset

def procesar(stem, rutas, a, ffmpeg, out):
    print("  1/4 Leyendo marcadores...")
    marcas = leer_marcadores(rutas[".edl"])

    print("  2/4 Extrayendo audio (puede tardar unos minutos)...")
    db, pico, delta = extraer_audio(rutas[".mp3"], ffmpeg)
    n = len(db)
    crest = pico - db                 # pico - RMS: el pico satura por limitador, el crest no
    mute = (db <= DB_PISO + 0.1).astype(int)   # silencio digital

    # Inicio de timeline: forzado por argumento, o autodetectado (01:00:00:00)
    if a.timeline_start:
        desplaza, inicio_tl = tc_a_seg(a.timeline_start), a.timeline_start
    elif marcas[0]["t"] >= 3600 and marcas[-1]["t"] > n + 2:
        desplaza, inicio_tl = 3600.0, "01:00:00:00"
        print("  AVISO: timeline detectada con inicio en 01:00:00:00; se restaron 3600 s.")
    else:
        desplaza, inicio_tl = 0.0, "00:00:00:00"
    for m in marcas:
        m["t"] -= desplaza
        if m["t"] < -1e-6:
            fail(f"Marcador '{m['nombre']}' ({m['tc']}) queda antes del inicio de la timeline.")
        m["t"] = max(m["t"], 0.0)

    segs = emparejar(marcas)
    if segs[-1]["fin"] > n + 2:
        fail(f"El ultimo marcador esta en {segs[-1]['fin']:.1f} s pero el audio dura "
             f"{n} s. Revisa el inicio de la timeline (--timeline-start).")

    print("  3/4 Procesando SRT y chat...")
    palabras, descartados = extraer_srt(rutas[".srt"], n, a.srt_offset)
    for seg_, w_, tx_ in descartados:
        print(f"  AVISO: cue SRT en {seg_} s descartado (> {MAX_PALABRAS_SEG} palabras/s, "
              f"{w_} palabras, probable alucinacion): '{tx_}'")
    emotes = None
    f_emo = Path(a.carpeta) / "emotes" / f"{stem}.txt"
    if f_emo.exists():
        emotes = {l.strip() for l in f_emo.read_text(encoding="utf-8").splitlines() if l.strip()}
    bits = None
    if ".json" in rutas:
        print("  Chat: JSON de TwitchDownloaderCLI")
        msgs, unicos, caps, subs, emo, bits, cand = extraer_chat_json(
            rutas[".json"], n, a.chat_offset, emotes)
    else:
        print("  Chat: TXT (sin emotes nativos ni bits; usa JSON para el dataset final)")
        msgs, unicos, caps, subs, emo, cand = extraer_chat(rutas[".txt"], n, a.chat_offset, emotes)

    print("  4/4 Asignando etiquetas y guardando...")
    nivel = [""] * n
    color = [""] * n
    for s in segs:
        for t in range(n):
            if s["inicio"] <= t + 0.5 < s["fin"]:
                nivel[t] = COLOR_A_NIVEL[s["color"]]
                color[t] = s["color"]

    streamer = stem.split("__")[0]   # nombres del descargador: canal__fecha_id
    cols = ["streamer", "vod", "t", "db_rms", "db_crest", "audio_mute", "db_delta", "srt_words",
            "chat_msgs", "chat_users", "chat_caps_ratio", "chat_subs"]
    if emo is not None:
        cols.append("chat_emotes")
    if bits is not None:
        cols.append("chat_bits")
    cols += ["nivel", "color"]
    with open(out / f"{stem}_features.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols)
        for t in range(n):
            fila = [streamer, stem, t, f"{db[t]:.2f}", f"{crest[t]:.2f}", int(mute[t]), f"{delta[t]:.2f}",
                    f"{palabras[t]:.3f}", int(msgs[t]), int(unicos[t]),
                    f"{caps[t]:.4f}", int(subs[t])]
            if emo is not None:
                fila.append(int(emo[t]))
            if bits is not None:
                fila.append(int(bits[t]))
            fila += [nivel[t], color[t]]
            w.writerow(fila)

    (out / f"{stem}_segmentos.json").write_text(
        json.dumps({"vod": stem, "fps": FPS, "timeline_start": inicio_tl,
                    "segmentos": segs}, indent=2, ensure_ascii=False), encoding="utf-8")

    if emotes is None and cand:
        top = [(k, v) for k, v in cand.most_common(60) if v >= 10]
        (out / f"{stem}_emotes_candidatos.txt").write_text(
            "\n".join(f"{k}\t{v}" for k, v in top), encoding="utf-8")

    etiquetados = sum(1 for x in nivel if x != "")
    en_mute = sum(1 for t in range(n) if nivel[t] != "" and mute[t])
    if en_mute:
        print(f"  AVISO: {en_mute} s etiquetados caen en SILENCIO DIGITAL (columna audio_mute=1). "
              "Excluirlos del entrenamiento o reetiquetarlos.")
    if abs(n - segs[-1]["fin"]) > 3:
        print(f"  AVISO: el audio dura {n} s y el ultimo marcador esta en {segs[-1]['fin']:.1f} s "
              f"(diferencia {n - segs[-1]['fin']:+.1f} s). Confirma que MP3 y timeline parten del mismo punto.")
    diagnostico_sync(db, delta, palabras, msgs)
    print(f"  OK | {n} s | {len(segs)} segmentos | etiquetados {etiquetados} s "
          f"({100*etiquetados/n:.1f}%) | chat {int(msgs.sum())} msgs, {int(subs.sum())} sistema")
    dist = Counter(x for x in nivel if x != "")
    print("  Niveles: " + ", ".join(f"{k}={dist[k]}s" for k in sorted(dist)))
    if emo is None:
        print("  Emotes no contados (sin lista). Candidatos en *_emotes_candidatos.txt")


# ----------------------------------------------------------------------------
# Punto de entrada para la ventana (antes: main() con argparse + sys.exit)
# ----------------------------------------------------------------------------
def procesar_lote(carpeta, salida=None, seleccion=None, timeline_start=None,
                  chat_offset=0.0, srt_offset=0.0, al_empezar=None, al_terminar=None):
    """seleccion: lista de 'stem' a procesar (None = todos los completos).
    al_empezar(stem) y al_terminar(stem, ok, mensaje) permiten pintar la tabla en vivo."""
    ffmpeg = C.ruta_binario("ffmpeg")
    import os
    if not os.path.exists(ffmpeg):
        raise RuntimeError(f"No se encontró ffmpeg en: {ffmpeg}")

    carpeta = Path(carpeta)
    out = Path(salida) if salida else carpeta / "dataset_salida"
    out.mkdir(parents=True, exist_ok=True)

    filas = listar_directos(carpeta)
    if not filas:
        raise RuntimeError(f"No hay archivos {EXTENSIONES} en {carpeta}")

    objetivo = [f for f in filas if (seleccion is None and f["completo"]) or
               (seleccion is not None and f["stem"] in seleccion)]
    a = _Args(carpeta, timeline_start, chat_offset, srt_offset)

    C.log(f"Carpeta: {carpeta}\nFFmpeg:  {ffmpeg}\nSalida:  {out}\n"
         f"Directos a procesar: {len(objetivo)}\n")
    ok, errores = [], []
    for fila in objetivo:
        C.comprobar_cancelacion()
        stem, rutas = fila["stem"], fila["rutas"]
        if al_empezar:
            al_empezar(stem)
        print(f"[{stem}]")
        faltan = [e for e in EXT_OBLIGATORIAS if e not in rutas]
        if not any(e in rutas for e in EXT_CHAT):
            faltan.append("chat (.json o .txt)")
        if faltan:
            msg = f"faltan archivos: {', '.join(faltan)}"
            print(f"  ERROR: {msg}. Directo omitido.\n")
            errores.append((stem, msg))
            if al_terminar:
                al_terminar(stem, False, msg)
            continue
        try:
            procesar(stem, rutas, a, ffmpeg, out)
            ok.append(stem)
            if al_terminar:
                al_terminar(stem, True, "Listo")
        except C.Cancelado:
            if al_terminar:
                al_terminar(stem, False, "Cancelado")
            raise
        except ErrorDirecto as e:
            print(f"  ERROR: {e}")
            errores.append((stem, str(e)))
            if al_terminar:
                al_terminar(stem, False, str(e))
        except Exception as e:
            print(f"  ERROR INESPERADO: {type(e).__name__}: {e}")
            errores.append((stem, f"{type(e).__name__}: {e}"))
            if al_terminar:
                al_terminar(stem, False, f"{type(e).__name__}: {e}")
        print()

    print("=== RESUMEN DEL LOTE ===")
    print(f"Procesados OK: {len(ok)} | Con error: {len(errores)}")
    for stem, msg in errores:
        print(f"  X {stem}: {msg}")
    return ok, errores, out
