"""
app.py - Ventana de NextFrames (CustomTkinter), negro y azul.
Dos pantallas (Descarga y transcripcion -con el gestor de modelos integrado-
y Extraccion). Sin panel de registro aparte: los errores se muestran en la
misma fila/barra donde ocurren. Todo lo pesado corre en hilos.
"""
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

import nf_comun as C
import nf_cuda as CUDA
import nf_modelos as M

# Version visible en el titulo de la ventana y en VERSION.txt: sirve para
# confirmar de un vistazo que el .exe que abriste es el que se compilo con
# ESTE app.py, y no uno viejo que haya quedado por ahi.
VERSION_BUILD = "2026-09-29.4"

# ==========================================
# Tema: negro y azul
# ==========================================
ctk.set_appearance_mode("dark")
FONDO = "#0a1e42"  # azul marino: reemplaza el negro puro de fondo (ya no hay "bordes negros" a los lados)
PANEL = "#0a0e17"
RAISE = "#0d1220"
LINEA = "#16213d"
AZUL = "#2f7bff"
AZUL_OSC = "#0d2a66"
HIELO = "#9cc4ff"
TEXTO = "#e8eefc"
MUT = "#7d8bad"
ERR = "#ff6b6b"
OK = "#5fd0a0"
FUENTE = ("Segoe UI", 13)
FUENTE_B = ("Segoe UI", 13, "bold")
FUENTE_H2 = ("Segoe UI", 19, "bold")
ANCHO_TARJETA = 820

IDIOMAS = [("", "Elegir idioma..."), ("es", "Español"), ("en", "Inglés"),
          ("pt", "Portugués"), ("fr", "Francés"), ("de", "Alemán"), ("it", "Italiano")]
ETAPAS_ITEM = [("video", "Video"), ("audio", "Audio"), ("chat", "Chat"),
              ("modelo", "Modelo"), ("transcripcion", "Transcripción")]


# ==========================================
# Cola de eventos: los hilos nunca tocan widgets directamente
# ==========================================
class Bus:
    def __init__(self):
        self.q = queue.Queue()

    def put(self, tipo, *datos):
        self.q.put((tipo, datos))

    def drenar(self):
        eventos = []
        try:
            while True:
                eventos.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return eventos


BUS = Bus()


def hilo(fn):
    """Lanza fn en un hilo daemon; cualquier excepcion no capturada llega al aviso."""
    def envoltura(*a, **k):
        def correr():
            try:
                fn(*a, **k)
            except C.Cancelado:
                BUS.put("log", "Cancelado por el usuario.", "aviso")
            except Exception as e:
                BUS.put("log", f"ERROR INESPERADO: {type(e).__name__}: {e}", "error")
            finally:
                BUS.put("hilo_fin")
        threading.Thread(target=correr, daemon=True).start()
    return envoltura


# ==========================================
# Widgets reutilizables
# ==========================================
class Tarjeta(ctk.CTkFrame):
    def __init__(self, master, **kw):
        kw.setdefault("fg_color", PANEL)
        kw.setdefault("border_color", LINEA)
        kw.setdefault("border_width", 1)
        kw.setdefault("corner_radius", 8)
        super().__init__(master, **kw)


class ItemCola(Tarjeta):
    """Una fila de la pantalla de descarga: titulo + estado + 5 barras.
    set_estado() es tambien donde aparece el error si algo falla (en rojo,
    ahi mismo, en vez de en un registro aparte)."""
    def __init__(self, master, url):
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)
        cab = ctk.CTkFrame(self, fg_color="transparent")
        cab.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        cab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cab, text=(url[:60] + "…") if len(url) > 60 else url,
                    font=FUENTE_B, text_color=TEXTO, anchor="w").grid(row=0, column=0, sticky="w")
        self.lbl_estado = ctk.CTkLabel(cab, text="en cola", font=("Segoe UI", 12),
                                       text_color=MUT, anchor="e", justify="right",
                                       wraplength=340)
        self.lbl_estado.grid(row=0, column=1, sticky="e")

        fila = ctk.CTkFrame(self, fg_color="transparent")
        fila.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.barras = {}
        for i, (clave, etiqueta) in enumerate(ETAPAS_ITEM):
            fila.grid_columnconfigure(i, weight=1)
            col = ctk.CTkFrame(fila, fg_color="transparent")
            col.grid(row=0, column=i, sticky="ew", padx=4)
            ctk.CTkLabel(col, text=etiqueta, font=("Segoe UI", 10), text_color=MUT).pack(anchor="w")
            b = ctk.CTkProgressBar(col, height=6, progress_color=AZUL, fg_color=RAISE)
            b.set(0)
            b.pack(fill="x")
            self.barras[clave] = b

    def set_estado(self, texto, color=MUT):
        self.lbl_estado.configure(text=texto, text_color=color)

    def marcar_error_etapa(self, etapa, texto):
        """Pinta de rojo la barra de la etapa donde ocurrio el fallo."""
        b = self.barras.get(etapa if etapa != "benchmark" else "modelo")
        if b:
            try:
                b.stop()
            except Exception:
                pass
            b.configure(progress_color=ERR)
        self.set_estado(texto, ERR)

    def set_progreso(self, etapa, valor, texto):
        if etapa == "benchmark":
            etapa = "modelo"
        b = self.barras.get(etapa)
        if b is None:
            return
        if valor is None:
            b.configure(mode="indeterminate")
            b.start()
        else:
            try:
                b.stop()
            except Exception:
                pass
            b.configure(mode="determinate")
            b.set(max(0.0, min(1.0, valor)))
        if texto:
            self.set_estado(f"{dict(ETAPAS_ITEM).get(etapa, etapa)}: {texto}", HIELO)


class FilaExtraccion(Tarjeta):
    """Una fila de la tabla de la pantalla de Extraccion. Si algo falla al
    procesar, el error aparece aqui mismo, en la columna de estado."""
    MARCAS = {".mp3": "mp3", ".srt": "srt", ".edl": "edl", "chat": "chat"}

    def __init__(self, master, fila_datos):
        super().__init__(master, fg_color="transparent", border_width=0)
        self.stem = fila_datos["stem"]
        self.completo = fila_datos["completo"]
        self.grid_columnconfigure(1, weight=1)

        self.var = tk.BooleanVar(value=self.completo)
        self.chk = ctk.CTkCheckBox(self, text="", variable=self.var, width=20,
                                   state=("normal" if self.completo else "disabled"),
                                   fg_color=AZUL, border_color=LINEA)
        self.chk.grid(row=0, column=0, padx=(8, 4), pady=6)

        ctk.CTkLabel(self, text=self.stem, font=FUENTE, text_color=TEXTO, anchor="w").grid(
            row=0, column=1, sticky="w", padx=4)

        marcas = ctk.CTkFrame(self, fg_color="transparent")
        marcas.grid(row=0, column=2, padx=4)
        faltan = []
        for clave, corta in self.MARCAS.items():
            presente = fila_datos["archivos"].get(clave, False)
            if not presente:
                faltan.append(corta)
            ctk.CTkLabel(marcas, text=("✔ " if presente else "✘ ") + corta,
                        font=("Segoe UI", 11), width=52,
                        text_color=(OK if presente else ERR)).pack(side="left")

        self.lbl_estado = ctk.CTkLabel(
            self, font=FUENTE, wraplength=220, justify="right",
            text=("Listo" if self.completo else "Falta " + ", ".join(faltan)),
            text_color=(OK if self.completo else ERR))
        self.lbl_estado.grid(row=0, column=3, sticky="e", padx=(4, 10))

    def set_estado(self, texto, color):
        self.lbl_estado.configure(text=texto, text_color=color)


class FilaModelo(Tarjeta):
    """Fila del gestor de modelos, ahora dentro de la pantalla de descarga.
    Un fallo de descarga se muestra en su propia columna de estado."""
    def __init__(self, master, m, on_cambio):
        super().__init__(master, fg_color="transparent", border_width=0)
        self.m = m
        self.on_cambio = on_cambio
        self.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(self, text=m.nombre, font=FUENTE_B, text_color=TEXTO, width=140,
                    anchor="w").grid(row=0, column=0, padx=(4, 8), pady=6, sticky="w")
        ctk.CTkLabel(self, text=M.tam_texto(m), font=FUENTE, text_color=MUT, width=60).grid(
            row=0, column=1)
        idi = "Solo inglés" if m.solo_ingles else "Todos"
        ctk.CTkLabel(self, text=idi, font=("Segoe UI", 12), width=90,
                    text_color=(ERR if m.solo_ingles else MUT)).grid(row=0, column=2, sticky="w")
        self.lbl_estado = ctk.CTkLabel(self, text="", font=("Segoe UI", 12), width=180,
                                       wraplength=180, justify="left", anchor="w")
        self.lbl_estado.grid(row=0, column=3, sticky="w")
        der = ctk.CTkFrame(self, fg_color="transparent")
        der.grid(row=0, column=4, padx=4)
        self.barra = ctk.CTkProgressBar(der, width=70, height=6, progress_color=AZUL, fg_color=RAISE)
        self.barra.set(0)
        self.btn = ctk.CTkButton(der, text="", width=80, height=26, font=("Segoe UI", 11),
                                 command=self._clic)
        self.btn.pack(side="left")
        self.actualizar()

    def actualizar(self):
        if M.instalado(self.m.nombre):
            self.lbl_estado.configure(text="Instalado", text_color=OK)
            self.btn.configure(text="Eliminar", fg_color="transparent", border_width=1,
                              border_color=LINEA, text_color=HIELO, hover_color=RAISE,
                              state="normal")
            self.barra.pack_forget()
        else:
            self.lbl_estado.configure(text="No descargado", text_color=MUT)
            self.btn.configure(text="Descargar", fg_color=AZUL, text_color="white",
                              hover_color="#255fd1", state="normal")
            self.barra.pack(side="left", padx=(6, 0))
            self.barra.set(0)

    def marcar_error(self, texto):
        self.lbl_estado.configure(text=texto, text_color=ERR)
        self.btn.configure(state="normal", text="Reintentar")

    def _clic(self):
        if M.instalado(self.m.nombre):
            M.eliminar(self.m.nombre)
            self.actualizar()
            self.on_cambio()
        else:
            self.btn.configure(state="disabled", text="Descargando…")
            self.lbl_estado.configure(text="Descargando…", text_color=HIELO)

            @hilo
            def ir():
                try:
                    M.descargar(self.m.nombre, lambda f: BUS.put("modelo_prog", self.m.nombre, f))
                except Exception as e:
                    BUS.put("modelo_error", self.m.nombre, f"{type(e).__name__}: {e}")
                    raise
                BUS.put("modelo_listo", self.m.nombre)
            ir()


# ==========================================
# Ventana principal
# ==========================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"NextFrames — {VERSION_BUILD}")
        self.geometry("1150x780")
        self.minsize(880, 620)
        self.configure(fg_color=FONDO)
        self.items_cola = []
        self.item_actual = 0
        self.filas_extraccion = {}
        self.filas_modelo = []
        self.procesando = False
        self.cuda_ok = CUDA.cuda_instalado()
        self._modelo_valor = {"Automático (benchmark)": "auto"}
        self._modelo_elegido_previo = "auto"

        self._construir()
        C.set_callbacks(
            log=lambda t, n: BUS.put("log", t, n),
            progreso=lambda e, v, t: BUS.put("progreso", e, v, t),
        )
        sys.stdout = C.SalidaALog("info")
        sys.stderr = C.SalidaALog("error")
        faltan = C.binarios_faltantes()
        if faltan:
            self._aviso(f"Faltan en bin/: {', '.join(faltan)}. Cópialos ahí antes de iniciar.", "error")
        self.after(80, self._bombear)

    # ---------- estructura general ----------
    def _construir(self):
        cuerpo = ctk.CTkFrame(self, fg_color=FONDO, corner_radius=0)
        cuerpo.pack(fill="both", expand=True)
        cuerpo.grid_columnconfigure(1, weight=1)
        cuerpo.grid_rowconfigure(0, weight=1)

        nav = ctk.CTkFrame(cuerpo, fg_color=PANEL, width=190, corner_radius=0)
        nav.grid(row=0, column=0, sticky="ns")
        nav.grid_propagate(False)
        self.botones_nav = {}
        for clave, texto in (("descarga", "Descarga y\ntranscripción"), ("extraccion", "Extracción")):
            b = ctk.CTkButton(nav, text=texto, anchor="w", font=FUENTE, height=44,
                             fg_color="transparent", hover_color=RAISE, text_color=MUT,
                             command=lambda c=clave: self._mostrar(c))
            b.pack(fill="x", padx=8, pady=(16 if clave == "descarga" else 3, 3))
            self.botones_nav[clave] = b

        # separador vertical entre el menu y el contenido: marca la division
        # entre pantallas que pediste que se notara mas.
        ctk.CTkFrame(cuerpo, fg_color=LINEA, width=1, corner_radius=0).grid(
            row=0, column=0, sticky="nse")

        centro = ctk.CTkFrame(cuerpo, fg_color=FONDO, corner_radius=0)
        centro.grid(row=0, column=1, sticky="nsew")
        centro.grid_rowconfigure(0, weight=1)
        centro.grid_columnconfigure(0, weight=1)

        self.pantallas = {}
        self.pantallas["descarga"] = self._pantalla_descarga(centro)
        self.pantallas["extraccion"] = self._pantalla_extraccion(centro)
        self._mostrar("descarga")

    def _mostrar(self, clave):
        """Muestra solo la pantalla elegida (grid/grid_remove, no tkraise: con
        CTkScrollableFrame, tkraise() no siempre trae al frente el widget
        correcto si hay mas de uno en la misma celda)."""
        for c, p in self.pantallas.items():
            if c == clave:
                p.grid(row=0, column=0, sticky="nsew")
            else:
                p.grid_remove()
        for c, b in self.botones_nav.items():
            b.configure(fg_color=(AZUL_OSC if c == clave else "transparent"),
                       text_color=(TEXTO if c == clave else MUT))

    def _tarjeta_centrada(self, master):
        """Contenedor con borde, dentro de un scrollable que ocupa toda la
        pantalla y centra la tarjeta horizontalmente. El ancho lo termina
        marcando el wraplength de las etiquetas de adentro (~ANCHO_TARJETA),
        sin forzar un ancho fijo que pueda recortar contenido más alto."""
        externo = ctk.CTkScrollableFrame(master, fg_color=FONDO)
        externo.grid_columnconfigure(0, weight=1)
        tarjeta = Tarjeta(externo)
        tarjeta.pack(pady=24, padx=20)
        return externo, tarjeta

    # ---------- pantalla 1: descarga y transcripcion (+ modelos) ----------
    def _pantalla_descarga(self, master):
        externo, f = self._tarjeta_centrada(master)
        interior = ctk.CTkFrame(f, fg_color="transparent")
        interior.pack(fill="both", expand=True, padx=22, pady=20)

        ctk.CTkLabel(interior, text="Descarga y transcripción", font=FUENTE_H2,
                    text_color=TEXTO).pack(anchor="w")
        ctk.CTkLabel(interior, text="Pega uno o varios enlaces, elige el idioma y empieza. "
                                    "El primer directo que proceses calibra automáticamente "
                                    "cuál modelo conviene para ese idioma.",
                    font=FUENTE, text_color=MUT, wraplength=ANCHO_TARJETA - 60,
                    justify="left").pack(anchor="w", pady=(2, 14))

        # ---- aviso general (reemplaza al panel de registro) ----
        self.lbl_aviso = ctk.CTkLabel(interior, text="", font=("Segoe UI", 12),
                                      anchor="w", justify="left",
                                      wraplength=ANCHO_TARJETA - 60)
        self.lbl_aviso.pack(anchor="w", fill="x")

        ctk.CTkLabel(interior, text="ENLACES (uno por línea)", font=("Segoe UI", 11),
                    text_color=HIELO).pack(anchor="w", pady=(6, 0))
        self.txt_links = ctk.CTkTextbox(interior, height=90, fg_color=RAISE, text_color=TEXTO,
                                        border_color=LINEA, border_width=1)
        self.txt_links.pack(fill="x", pady=(2, 10))
        self.txt_links.bind("<KeyRelease>", lambda e: self._revisar_listo())

        fila = ctk.CTkFrame(interior, fg_color="transparent")
        fila.pack(fill="x", pady=(0, 6))
        fila.grid_columnconfigure((0, 1), weight=1)
        col1 = ctk.CTkFrame(fila, fg_color="transparent")
        col1.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkLabel(col1, text="IDIOMA DEL DIRECTO", font=("Segoe UI", 11),
                    text_color=HIELO).pack(anchor="w")
        self.op_idioma = ctk.CTkOptionMenu(
            col1, values=[t for _, t in IDIOMAS], fg_color=RAISE, button_color=AZUL_OSC,
            button_hover_color=AZUL, text_color=TEXTO, command=self._cambio_idioma)
        self.op_idioma.set(IDIOMAS[0][1])
        self.op_idioma.pack(fill="x", pady=2)

        col2 = ctk.CTkFrame(fila, fg_color="transparent")
        col2.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(col2, text="MODELO DE WHISPER", font=("Segoe UI", 11),
                    text_color=HIELO).pack(anchor="w")
        self.op_modelo = ctk.CTkOptionMenu(col2, values=["Automático (benchmark)"], fg_color=RAISE,
                                          button_color=AZUL_OSC, button_hover_color=AZUL,
                                          text_color=TEXTO, state="disabled",
                                          command=self._cambio_modelo)
        self.op_modelo.pack(fill="x", pady=2)

        fila_b = ctk.CTkFrame(interior, fg_color="transparent")
        fila_b.pack(fill="x", pady=(4, 4))
        self.btn_recalibrar = ctk.CTkButton(
            fila_b, text="Recalibrar benchmark", height=28, font=("Segoe UI", 11),
            fg_color="transparent", border_width=1, border_color=LINEA, text_color=HIELO,
            hover_color=RAISE, state="disabled", command=self._recalibrar)
        self.btn_recalibrar.pack(side="left")
        ctk.CTkLabel(fila_b, text="  120 s · no cuenta la descarga del modelo",
                    font=("Segoe UI", 11), text_color=MUT).pack(side="left")

        # ---- modelos disponibles: integrado aqui mismo, no en otra pantalla ----
        self.modelos_abiertos = True
        self.btn_modelos_toggle = ctk.CTkButton(
            interior, text="▾ Modelos disponibles", anchor="w", fg_color="transparent",
            text_color=HIELO, hover_color=RAISE, command=self._alternar_modelos)
        self.btn_modelos_toggle.pack(fill="x", pady=(10, 0))

        self.marco_modelos_todo = ctk.CTkFrame(interior, fg_color=PANEL, corner_radius=6)
        self.marco_modelos_todo.pack(fill="x", pady=(4, 10))
        fila_carpeta = ctk.CTkFrame(self.marco_modelos_todo, fg_color="transparent")
        fila_carpeta.pack(fill="x", padx=10, pady=(10, 6))
        self.ent_carpeta_modelos = ctk.CTkEntry(fila_carpeta, fg_color=RAISE, text_color=TEXTO,
                                                border_color=LINEA, height=28)
        self.ent_carpeta_modelos.insert(0, C.cargar_config()["carpeta_modelos"])
        self.ent_carpeta_modelos.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(fila_carpeta, text="Cambiar…", width=90, height=28, fg_color=RAISE,
                     border_width=1, border_color=LINEA, text_color=HIELO, hover_color=PANEL,
                     command=self._cambiar_carpeta_modelos).pack(side="left")
        self.marco_modelos = ctk.CTkFrame(self.marco_modelos_todo, fg_color="transparent")
        self.marco_modelos.pack(fill="x", padx=10, pady=(0, 10))
        self._cargar_modelos_tabla()

        self.lbl_modelo_aviso = ctk.CTkLabel(
            interior, text="", font=("Segoe UI", 12), text_color=ERR, anchor="w",
            wraplength=ANCHO_TARJETA - 60, justify="left")
        self.lbl_modelo_aviso.pack(anchor="w")

        ctk.CTkLabel(interior, text="PROCESADOR", font=("Segoe UI", 11), text_color=HIELO).pack(
            anchor="w", pady=(6, 0))
        self.seg_dev = ctk.CTkSegmentedButton(
            interior, values=["CPU", "GPU NVIDIA (CUDA)"], fg_color=RAISE, selected_color=AZUL_OSC,
            selected_hover_color=AZUL_OSC, unselected_color=RAISE, text_color=TEXTO,
            command=self._cambio_dispositivo)
        self.seg_dev.set("CPU")
        self.seg_dev.pack(fill="x", pady=(2, 4))

        self.marco_cuda = ctk.CTkFrame(interior, fg_color=RAISE, corner_radius=6)
        self.lbl_cuda = ctk.CTkLabel(self.marco_cuda, font=FUENTE, text_color=HIELO,
                                     justify="left", wraplength=ANCHO_TARJETA - 80)
        self.lbl_cuda.pack(anchor="w", padx=10, pady=(8, 4))
        fila_cuda = ctk.CTkFrame(self.marco_cuda, fg_color="transparent")
        fila_cuda.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_cuda = ctk.CTkButton(fila_cuda, text="Descargar librerías CUDA", height=28,
                                      font=("Segoe UI", 11), fg_color=AZUL, hover_color="#255fd1",
                                      command=self._descargar_cuda)
        self.btn_cuda.pack(side="left")
        self.barra_cuda = ctk.CTkProgressBar(fila_cuda, height=8, progress_color=AZUL, fg_color=PANEL)
        self.barra_cuda.set(0)
        self.barra_cuda.pack(side="left", fill="x", expand=True, padx=10)

        self.lbl_cpu_hint = ctk.CTkLabel(
            interior, text="Con CPU funciona en cualquier PC. Si la GPU falla, el programa "
                          "vuelve a CPU automáticamente.", font=("Segoe UI", 11), text_color=MUT,
            anchor="w", wraplength=ANCHO_TARJETA - 60, justify="left")
        self.lbl_cpu_hint.pack(anchor="w", pady=(6, 10))

        fila_ini = ctk.CTkFrame(interior, fg_color="transparent")
        fila_ini.pack(fill="x", pady=(4, 4))
        self.btn_iniciar = ctk.CTkButton(fila_ini, text="Iniciar", height=32, width=110,
                                        fg_color=AZUL, hover_color="#255fd1", state="disabled",
                                        command=self._iniciar_descarga)
        self.btn_iniciar.pack(side="left")
        self.btn_cancelar = ctk.CTkButton(fila_ini, text="Cancelar", height=32, width=110,
                                         fg_color="transparent", border_width=1, border_color=LINEA,
                                         text_color=HIELO, hover_color=RAISE, state="disabled",
                                         command=self._cancelar_descarga)
        self.btn_cancelar.pack(side="left", padx=8)
        self.lbl_falta = ctk.CTkLabel(fila_ini, text="Faltan enlaces e idioma.", font=("Segoe UI", 11),
                                      text_color=MUT, wraplength=380, justify="left")
        self.lbl_falta.pack(side="left", padx=8)

        self.marco_cola = ctk.CTkFrame(interior, fg_color="transparent")
        self.marco_cola.pack(fill="both", expand=True, pady=(8, 4))
        return externo

    def _alternar_modelos(self):
        self.modelos_abiertos = not self.modelos_abiertos
        if self.modelos_abiertos:
            self.btn_modelos_toggle.configure(text="▾ Modelos disponibles")
            self.marco_modelos_todo.pack(fill="x", pady=(4, 10), after=self.btn_modelos_toggle)
        else:
            self.btn_modelos_toggle.configure(text="▸ Modelos disponibles")
            self.marco_modelos_todo.pack_forget()

    def _cambio_idioma(self, _=None):
        self._revisar_listo()

    def _idioma_actual(self):
        texto = self.op_idioma.get()
        for c, t in IDIOMAS:
            if t == texto:
                return c
        return ""

    def _cambio_dispositivo(self, valor):
        gpu = valor.startswith("GPU")
        if gpu:
            self.cuda_ok = CUDA.cuda_instalado()
            self.marco_cuda.pack(fill="x", pady=(2, 8), before=self.lbl_cpu_hint)
            self.lbl_cpu_hint.pack_forget()
            if self.cuda_ok:
                self.lbl_cuda.configure(
                    text="Librerías CUDA instaladas. Se usará la GPU; si falla, el programa "
                        "vuelve a CPU automáticamente.")
                self.btn_cuda.pack_forget()
            else:
                self.lbl_cuda.configure(
                    text="Las librerías CUDA (~1 GB) no están instaladas. Se descargan ahora, "
                        "solo porque elegiste GPU.")
                self.btn_cuda.pack(side="left")
        else:
            self.marco_cuda.pack_forget()
            self.lbl_cpu_hint.pack(anchor="w", pady=(6, 10))
        self._revisar_listo()

    def _descargar_cuda(self):
        if not CUDA.hay_nvidia():
            self._aviso("No se detecta una GPU NVIDIA en este equipo.", "aviso")
        self.btn_cuda.configure(state="disabled", text="Descargando…")

        @hilo
        def ir():
            try:
                CUDA.instalar_cuda(lambda f, t: BUS.put("cuda_prog", f, t))
            except Exception as e:
                BUS.put("cuda_error", f"{type(e).__name__}: {e}")
                raise
            BUS.put("cuda_listo")
        ir()

    def _cambio_modelo(self, _=None):
        self._revisar_listo()

    def _revisar_listo(self):
        faltan = []
        if not self.txt_links.get("1.0", "end").strip():
            faltan.append("enlaces")
        if not self._idioma_actual():
            faltan.append("idioma")
        if self.seg_dev.get().startswith("GPU") and not self.cuda_ok:
            faltan.append("librerías CUDA")
        modelo_elegido = self._modelo_valor.get(self.op_modelo.get(), "auto")
        aviso_modelo = ""
        if modelo_elegido != "auto" and not M.instalado(modelo_elegido):
            faltan.append(f"descargar el modelo '{modelo_elegido}'")
            aviso_modelo = (f"Elegiste '{modelo_elegido}' a mano: descárgalo en "
                            f"«Modelos disponibles» antes de iniciar.")
        self.lbl_modelo_aviso.configure(text=aviso_modelo)
        listo = not faltan and not self.procesando
        self.btn_iniciar.configure(state=("normal" if listo else "disabled"))
        self.lbl_falta.configure(text="" if not faltan else "Faltan: " + ", ".join(faltan) + ".")
        self._actualizar_modelos_disponibles()

    def _actualizar_modelos_disponibles(self):
        idioma = self._idioma_actual()
        seleccion_previa = self._modelo_valor.get(self.op_modelo.get(), self._modelo_elegido_previo)
        if not idioma:
            self.op_modelo.configure(values=["Automático (benchmark)"], state="disabled")
            self.op_modelo.set("Automático (benchmark)")
            self.btn_recalibrar.configure(state="disabled")
            return
        cfg = C.cargar_config()
        dispositivo = "cuda" if self.seg_dev.get().startswith("GPU") else "cpu"
        recomendado = cfg.get("benchmark", {}).get(f"{idioma}:{dispositivo}", {}).get("modelo")
        opciones = ["Automático (benchmark)"]
        self._modelo_valor = {"Automático (benchmark)": "auto"}
        etiqueta_previa = opciones[0]
        for m in M.CATALOGO:
            if not M.compatible(m.nombre, idioma):
                continue
            etiqueta = m.nombre + ("" if M.instalado(m.nombre) else " (se descarga a mano)")
            if m.nombre == recomendado:
                etiqueta += " — recomendado"
            opciones.append(etiqueta)
            self._modelo_valor[etiqueta] = m.nombre
            if m.nombre == seleccion_previa:
                etiqueta_previa = etiqueta
        self.op_modelo.configure(values=opciones, state="normal")
        self.op_modelo.set(etiqueta_previa)
        self._modelo_elegido_previo = self._modelo_valor.get(etiqueta_previa, "auto")
        self.btn_recalibrar.configure(state="normal")

    def _recalibrar(self):
        idioma = self._idioma_actual()
        dispositivo = "cuda" if self.seg_dev.get().startswith("GPU") else "cpu"
        if not idioma:
            return
        cfg = C.cargar_config()
        cfg.get("benchmark", {}).pop(f"{idioma}:{dispositivo}", None)
        C.guardar_config(cfg)
        self._aviso(f"Se recalculará el mejor modelo para '{idioma}' en el próximo Iniciar.", "ok")
        self._actualizar_modelos_disponibles()

    def _iniciar_descarga(self):
        urls = [l.strip() for l in self.txt_links.get("1.0", "end").splitlines() if l.strip()]
        idioma = self._idioma_actual()
        modelo = self._modelo_valor.get(self.op_modelo.get(), "auto")
        dispositivo = "cuda" if self.seg_dev.get().startswith("GPU") else "cpu"
        if not urls or not idioma:
            return
        for w in self.marco_cola.winfo_children():
            w.destroy()
        self.items_cola = []
        for u in urls:
            it = ItemCola(self.marco_cola, u)
            it.pack(fill="x", pady=4)
            self.items_cola.append(it)
        self.item_actual = 0
        self.procesando = True
        self._revisar_listo()
        self.btn_cancelar.configure(state="normal")
        self._aviso(f"Inicio: {len(urls)} enlace(s), idioma {idioma}, {dispositivo.upper()}.", "ok")
        C.reiniciar_cancelacion()

        @hilo
        def ir():
            import motor_descarga as MD
            MD.configurar(C.cargar_config()["carpeta_salida"], idioma, modelo, dispositivo)

            def al_empezar(i, url):
                BUS.put("item_empieza", i)

            def al_terminar(i, ok, msg):
                BUS.put("item_termina", i, ok, msg)

            MD.procesar_lista(urls, al_empezar=al_empezar, al_terminar=al_terminar)
        ir()

    def _cancelar_descarga(self):
        C.cancelar()
        self._aviso("Cancelando…", "aviso")

    # ---------- gestor de modelos (integrado en la pantalla de descarga) ----------
    def _cambiar_carpeta_modelos(self):
        d = filedialog.askdirectory()
        if d:
            self.ent_carpeta_modelos.delete(0, "end")
            self.ent_carpeta_modelos.insert(0, d)
            C.actualizar_config(carpeta_modelos=d)
            self._cargar_modelos_tabla()
            self._actualizar_modelos_disponibles()

    def _cargar_modelos_tabla(self):
        for w in self.marco_modelos.winfo_children():
            w.destroy()
        self.filas_modelo = []
        for m in M.CATALOGO:
            fila = FilaModelo(self.marco_modelos, m, self._on_cambio_modelos)
            fila.pack(fill="x", pady=1)
            self.filas_modelo.append(fila)

    def _on_cambio_modelos(self):
        self._cargar_modelos_tabla()
        self._revisar_listo()

    # ---------- pantalla 2: extraccion ----------
    def _pantalla_extraccion(self, master):
        externo, f = self._tarjeta_centrada(master)
        interior = ctk.CTkFrame(f, fg_color="transparent")
        interior.pack(fill="both", expand=True, padx=22, pady=20)

        ctk.CTkLabel(interior, text="Extracción de dataset", font=FUENTE_H2, text_color=TEXTO).pack(
            anchor="w")
        ctk.CTkLabel(interior, text="Revisa qué archivos tiene cada directo antes de ejecutar.",
                    font=FUENTE, text_color=MUT).pack(anchor="w", pady=(2, 14))

        fila = ctk.CTkFrame(interior, fg_color="transparent")
        fila.pack(fill="x", pady=(0, 10))
        self.ent_carpeta = ctk.CTkEntry(fila, fg_color=RAISE, text_color=TEXTO, border_color=LINEA,
                                        placeholder_text="Carpeta con los directos")
        self.ent_carpeta.insert(0, str((C.BASE / "Contenido_Extraido")))
        self.ent_carpeta.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(fila, text="Elegir…", width=90, fg_color=RAISE, border_width=1,
                     border_color=LINEA, text_color=HIELO, hover_color=PANEL,
                     command=self._elegir_carpeta_extraccion).pack(side="left", padx=(0, 8))
        ctk.CTkButton(fila, text="Buscar directos", width=140, fg_color=AZUL,
                     hover_color="#255fd1", command=self._buscar_directos).pack(side="left")

        self.marco_tabla = ctk.CTkFrame(interior, fg_color="transparent")
        self.marco_tabla.pack(fill="both", expand=True)

        self.marco_avanzado_abierto = False
        self.btn_avanzado = ctk.CTkButton(
            interior, text="▸ Opciones avanzadas", anchor="w", fg_color="transparent",
            text_color=HIELO, hover_color=RAISE, command=self._alternar_avanzado)
        self.btn_avanzado.pack(fill="x", pady=(12, 0))
        self.marco_avanzado = ctk.CTkFrame(interior, fg_color=PANEL, corner_radius=6)
        for etq, campo in (("Inicio de timeline", "timeline"),
                          ("Desfase del chat (s)", "chat_off"),
                          ("Desfase del srt (s)", "srt_off")):
            c = ctk.CTkFrame(self.marco_avanzado, fg_color="transparent")
            c.pack(side="left", fill="x", expand=True, padx=10, pady=10)
            ctk.CTkLabel(c, text=etq, font=("Segoe UI", 11), text_color=HIELO).pack(anchor="w")
            e = ctk.CTkEntry(c, fg_color=RAISE, text_color=TEXTO, border_color=LINEA)
            if campo != "timeline":
                e.insert(0, "0")
            e.pack(fill="x")
            setattr(self, f"ent_{campo}", e)

        fila_run = ctk.CTkFrame(interior, fg_color="transparent")
        fila_run.pack(fill="x", pady=(12, 4))
        self.btn_extraer = ctk.CTkButton(fila_run, text="Extraer directos listos", height=32,
                                        fg_color=AZUL, hover_color="#255fd1",
                                        command=self._extraer, state="disabled")
        self.btn_extraer.pack(side="left")
        self.lbl_extraccion = ctk.CTkLabel(fila_run, text="", font=FUENTE, text_color=MUT,
                                          wraplength=440, justify="left")
        self.lbl_extraccion.pack(side="left", padx=10)
        return externo

    def _elegir_carpeta_extraccion(self):
        d = filedialog.askdirectory()
        if d:
            self.ent_carpeta.delete(0, "end")
            self.ent_carpeta.insert(0, d)

    def _alternar_avanzado(self):
        self.marco_avanzado_abierto = not self.marco_avanzado_abierto
        if self.marco_avanzado_abierto:
            self.btn_avanzado.configure(text="▾ Opciones avanzadas")
            self.marco_avanzado.pack(fill="x", pady=(4, 0))
        else:
            self.btn_avanzado.configure(text="▸ Opciones avanzadas")
            self.marco_avanzado.pack_forget()

    def _buscar_directos(self):
        carpeta = self.ent_carpeta.get().strip()
        for w in self.marco_tabla.winfo_children():
            w.destroy()
        self.filas_extraccion = {}

        @hilo
        def ir():
            import motor_extraer as ME
            filas = ME.listar_directos(carpeta)
            BUS.put("directos_listados", filas)
        ir()

    def _extraer(self):
        seleccion = [s for s, fila in self.filas_extraccion.items() if fila.var.get()]
        if not seleccion:
            self._aviso("No hay directos seleccionados para extraer.", "aviso")
            return
        carpeta = self.ent_carpeta.get().strip()
        timeline = self.ent_timeline.get().strip() or None
        try:
            chat_off = float(self.ent_chat_off.get() or 0)
            srt_off = float(self.ent_srt_off.get() or 0)
        except ValueError:
            self._aviso("Los desfases deben ser numéricos.", "error")
            return
        self.btn_extraer.configure(state="disabled")
        C.reiniciar_cancelacion()

        @hilo
        def ir():
            import motor_extraer as ME

            def al_empezar(stem):
                BUS.put("extraccion_empieza", stem)

            def al_terminar(stem, ok, msg):
                BUS.put("extraccion_termina", stem, ok, msg)

            ok, errores, out = ME.procesar_lote(
                carpeta, seleccion=seleccion, timeline_start=timeline,
                chat_offset=chat_off, srt_offset=srt_off,
                al_empezar=al_empezar, al_terminar=al_terminar)
            BUS.put("extraccion_fin", len(ok), len(errores), str(out))
        ir()

    # ---------- aviso general (sustituye al panel de registro) ----------
    def _aviso(self, texto, nivel="info"):
        color = {"error": ERR, "aviso": MUT, "ok": OK}.get(nivel, TEXTO)
        self.lbl_aviso.configure(text=texto, text_color=color)

    # ---------- bombeo de eventos desde los hilos ----------
    def _bombear(self):
        for tipo, datos in BUS.drenar():
            try:
                self._procesar_evento(tipo, datos)
            except Exception as e:
                self._aviso(f"Error en la ventana al procesar '{tipo}': {e}", "error")
        self.after(80, self._bombear)

    def _procesar_evento(self, tipo, datos):
        if tipo == "log":
            texto, nivel = datos
            # Si hay un directo en curso, el aviso se pinta en SU fila/barra
            # (ahi es donde pediste que se vea), no en un registro aparte.
            if nivel in ("error", "aviso") and self.items_cola and self.procesando:
                if 0 <= self.item_actual < len(self.items_cola):
                    self.items_cola[self.item_actual].set_estado(texto, ERR if nivel == "error" else MUT)
                    return
            if nivel in ("error", "aviso", "ok"):
                self._aviso(texto, nivel)
        elif tipo == "progreso":
            etapa, valor, texto = datos
            if self.items_cola and 0 <= self.item_actual < len(self.items_cola):
                self.items_cola[self.item_actual].set_progreso(etapa, valor, texto)
        elif tipo == "item_empieza":
            (i,) = datos
            self.item_actual = i
            if i < len(self.items_cola):
                self.items_cola[i].set_estado("procesando…", HIELO)
        elif tipo == "item_termina":
            i, ok, msg = datos
            if i < len(self.items_cola):
                # No sabemos con certeza en cual de las 5 etapas fallo (el error
                # puede venir de video, chat o transcripcion), asi que el error
                # se muestra en el texto de estado del directo, sin adivinar
                # cual barra individual pintar de rojo.
                self.items_cola[i].set_estado(msg, OK if ok else ERR)
        elif tipo == "cuda_prog":
            f, t = datos
            self.barra_cuda.set(f)
            self.lbl_cuda.configure(text=t, text_color=HIELO)
        elif tipo == "cuda_error":
            (msg,) = datos
            self.btn_cuda.configure(state="normal", text="Reintentar descarga")
            self.lbl_cuda.configure(text=f"Falló la descarga de CUDA: {msg}", text_color=ERR)
        elif tipo == "cuda_listo":
            self.cuda_ok = True
            self.btn_cuda.configure(state="normal", text="Descargar librerías CUDA")
            self._cambio_dispositivo("GPU NVIDIA (CUDA)")
        elif tipo == "modelo_prog":
            nombre, f = datos
            for fila in self.filas_modelo:
                if fila.m.nombre == nombre:
                    fila.barra.set(f)
        elif tipo == "modelo_error":
            nombre, msg = datos
            for fila in self.filas_modelo:
                if fila.m.nombre == nombre:
                    fila.marcar_error(msg)
        elif tipo == "modelo_listo":
            (nombre,) = datos
            for fila in self.filas_modelo:
                if fila.m.nombre == nombre:
                    fila.actualizar()
            self._revisar_listo()
        elif tipo == "directos_listados":
            (filas,) = datos
            for w in self.marco_tabla.winfo_children():
                w.destroy()
            self.filas_extraccion = {}
            if not filas:
                ctk.CTkLabel(self.marco_tabla, text="No se encontraron directos en esa carpeta.",
                           font=FUENTE, text_color=MUT).pack(anchor="w", pady=10)
            for d in filas:
                fila = FilaExtraccion(self.marco_tabla, d)
                fila.pack(fill="x", pady=2)
                self.filas_extraccion[d["stem"]] = fila
            n = sum(1 for d in filas if d["completo"])
            self.lbl_extraccion.configure(text=f"{n} de {len(filas)} directos listos.", text_color=MUT)
            self.btn_extraer.configure(state=("normal" if filas else "disabled"))
        elif tipo == "extraccion_empieza":
            (stem,) = datos
            if stem in self.filas_extraccion:
                self.filas_extraccion[stem].set_estado("procesando…", HIELO)
        elif tipo == "extraccion_termina":
            stem, ok, msg = datos
            if stem in self.filas_extraccion:
                self.filas_extraccion[stem].set_estado(msg, OK if ok else ERR)
        elif tipo == "extraccion_fin":
            n_ok, n_err, out = datos
            self.btn_extraer.configure(state="normal")
            color = OK if n_err == 0 else ERR
            self.lbl_extraccion.configure(
                text=f"Terminado: {n_ok} OK, {n_err} con error. Salida: {out}", text_color=color)
        elif tipo == "hilo_fin":
            if self.procesando:
                self.procesando = False
                self.btn_cancelar.configure(state="disabled")
                self._revisar_listo()


if __name__ == "__main__":
    C.preparar_entorno()
    App().mainloop()
