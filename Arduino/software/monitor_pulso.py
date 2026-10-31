#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MONITOR DE PULSO ESTILO HOSPITAL  (extra del taller)

Dibuja la onda del pulso (fotopletismograma, PPG) como un monitor de signos
vitales: trazo verde que barre la pantalla de izquierda a derecha, BPM en
grande y un corazon que late con cada pulso.

REQUISITOS
    pip install numpy matplotlib pyserial

ARDUINO
    Este monitor necesita la SEÑAL CRUDA, asi que en el firmware:
        const bool MODO_PLOTTER = true;      // y  Serial.begin(115200)
    Cierra el Serial Plotter / Monitor Serie antes de ejecutarlo (el puerto
    no se puede compartir).

USO
    python monitor_pulso.py --puerto COM6        # con el sensor real
    python monitor_pulso.py --simular            # sin hardware (demostracion)

TECLAS
    q            salir
    f            pantalla completa
    (solo simulacion)  ↑ ↓ cambia los BPM   ·   d quita / pone el dedo

AVISO: demostracion educativa. No es un dispositivo medico.
"""
import argparse
import collections
import math
import random
import statistics as st
import sys
import threading
import time

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# ============================== PARAMETROS ==================================
VENTANA_S = 6.0          # segundos que cubre el barrido de la pantalla
BINS_POR_S = 200         # resolucion horizontal del trazo
HUECO_S = 0.35           # "borrador": hueco delante del trazo (como en un monitor real)
FPS = 30                 # cuadros por segundo de la animacion

LIM_BPM = (50, 110)      # fuera de este rango el numero se pone rojo (didactico)
AMP_MIN = 12             # unidades ADC minimas de la onda para decir "hay señal"
REFRACTARIO_S = 0.30     # tiempo minimo entre latidos (max. 200 BPM)
SIN_DATOS_S = 1.5        # segundos sin muestras para avisar "sin datos"
SIN_LATIDO_S = 4.0       # segundos sin latidos para borrar el BPM

# Colores (estilo monitor)
FONDO = "#000000"
VERDE = "#39ff7a"
VERDE_OSC = "#0d4d27"
ROJO = "#ff4b4b"
AMBAR = "#ffb020"
GRIS = "#7a8a80"


# ============================ FUENTES DE DATOS ==============================
class FuenteSerial:
    """Lee el Arduino en un hilo aparte. Espera un numero (0-1023) por linea."""

    def __init__(self, puerto, baud):
        import serial
        self.ser = serial.Serial(puerto, baud, timeout=0.5)
        self.cola = collections.deque()
        self.modo_incorrecto = False   # llegan lineas "IBI,..." o "BPM:" en vez de la señal
        self.error = None
        self.activo = True
        threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self):
        while self.activo:
            try:
                linea = self.ser.readline().decode(errors="ignore").strip()
            except Exception as e:          # puerto desconectado
                self.error = str(e)
                break
            if not linea:
                continue
            if linea.isdigit():
                v = int(linea)
                if 0 <= v <= 1023:
                    self.cola.append((time.perf_counter(), v))
            elif linea.startswith(("IBI", "BPM")):
                self.modo_incorrecto = True

    def leer(self):
        out = []
        while True:
            try:
                out.append(self.cola.popleft())
            except IndexError:
                return out

    def cerrar(self):
        self.activo = False
        try:
            self.ser.close()
        except Exception:
            pass


class FuenteSimulada:
    """Genera una onda de pulso falsa a 500 muestras/s (para probar sin sensor)."""
    FS = 500
    modo_incorrecto = False
    error = None

    def __init__(self, bpm=72):
        self.bpm = bpm
        self.sin_dedo = False
        self.fase = 0.0
        self.t_ult = time.perf_counter()

    def leer(self):
        ahora = time.perf_counter()
        n = int((ahora - self.t_ult) * self.FS)
        dt = 1.0 / self.FS
        out = []
        for i in range(n):
            t = self.t_ult + (i + 1) * dt
            if self.sin_dedo:
                v = 512 + random.gauss(0, 1.2)
            else:
                # el ritmo varia un poco con la "respiracion" (cada ~5 s)
                bpm_inst = self.bpm * (1 + 0.04 * math.sin(2 * math.pi * t / 5.0))
                self.fase += dt * bpm_inst / 60.0
                p = self.fase % 1.0
                onda = (math.exp(-((p - 0.20) / 0.08) ** 2)
                        + 0.33 * math.exp(-((p - 0.50) / 0.12) ** 2))
                v = 500 + 60 * onda + random.gauss(0, 1.0)
            out.append((t, int(max(0, min(1023, round(v))))))
        self.t_ult += n * dt
        return out

    def cerrar(self):
        pass


# ============================ DETECTOR DE LATIDOS ===========================
class DetectorLatidos:
    """
    Version en Python de la idea del firmware: umbral adaptativo.
    - Mira los ultimos 3 s para saber cuanto valen el "piso" y el "techo".
    - Cuenta un latido cuando la señal sube del 60 % del rango.
    - Se "rearma" cuando baja del 40 % (histeresis), asi el lomo pequeño de la
      onda (muesca dicrota) no se cuenta como otro latido.
    """

    def __init__(self):
        self.ventana = collections.deque()      # (t, valor) de los ultimos 3 s
        self.armado = True
        self.ultimo_t = None                    # hora del ultimo latido
        self.ibis = collections.deque(maxlen=8)  # ultimos intervalos (ms)
        self.lo = self.hi = None
        self._t_calc = 0.0

    @property
    def amplitud(self):
        return 0.0 if self.lo is None else float(self.hi - self.lo)

    def agregar(self, t, v):
        """Devuelve True si en esta muestra empieza un latido."""
        self.ventana.append((t, v))
        while self.ventana and t - self.ventana[0][0] > 3.0:
            self.ventana.popleft()
        if t - self._t_calc > 0.1 and len(self.ventana) > 100:     # recalcula 10 veces/s
            self._t_calc = t
            vals = np.fromiter((x[1] for x in self.ventana), float)
            self.lo, self.hi = np.percentile(vals, [5, 95])
        if self.lo is None or self.amplitud < AMP_MIN:
            return False

        rango = self.hi - self.lo
        alto, bajo = self.lo + 0.6 * rango, self.lo + 0.4 * rango
        if v < bajo:
            self.armado = True
        if v > alto and self.armado and (self.ultimo_t is None or t - self.ultimo_t > REFRACTARIO_S):
            self.armado = False
            if self.ultimo_t is not None:
                ibi = (t - self.ultimo_t) * 1000
                if ibi <= 2000:
                    self.ibis.append(ibi)
                else:
                    self.ibis.clear()           # hubo un corte: no mezclar
            self.ultimo_t = t
            return True
        return False

    def bpm(self, ahora):
        if self.ultimo_t is None or ahora - self.ultimo_t > SIN_LATIDO_S or len(self.ibis) < 4:
            return None
        return 60000.0 / st.median(self.ibis)


# ================================ MONITOR ===================================
class Monitor:
    def __init__(self, fuente, simulado=False):
        self.fuente = fuente
        self.simulado = simulado
        self.det = DetectorLatidos()

        self.N = int(VENTANA_S * BINS_POR_S)
        self.y = np.full(self.N, np.nan)
        self.x = np.arange(self.N) / BINS_POR_S
        self.t0 = time.perf_counter()
        self.ult_idx = None
        self.t_ult_dato = self.t0
        self.ymin, self.ymax = 440.0, 580.0

        self._crear_figura()

    # ---------- dibujo ----------
    def _crear_figura(self):
        plt.rcParams["toolbar"] = "None"
        plt.rcParams["keymap.save"] = []
        self.fig = plt.figure(figsize=(12, 6.6), facecolor=FONDO)
        try:
            self.fig.canvas.manager.set_window_title("Monitor de pulso")
        except Exception:
            pass

        self.ax = self.fig.add_axes([0.03, 0.14, 0.74, 0.70], facecolor=FONDO)
        ax = self.ax
        ax.set_xlim(0, VENTANA_S)
        ax.set_ylim(self.ymin, self.ymax)
        ax.set_xticks(np.arange(0, VENTANA_S + 0.01, 0.5))
        ax.set_yticks([])
        ax.tick_params(labelbottom=False, length=0)
        ax.grid(axis="x", color=VERDE_OSC, linewidth=0.6)
        for f in np.linspace(0.1, 0.9, 5):                # rejilla horizontal fija
            ax.plot([0, 1], [f, f], transform=ax.transAxes, color=VERDE_OSC, linewidth=0.6, zorder=0)
        for s in ax.spines.values():
            s.set_color(VERDE_OSC)

        # brillo (linea ancha y tenue) + trazo fino encima
        (self.brillo,) = ax.plot(self.x, self.y, color=VERDE, linewidth=6, alpha=0.12, solid_capstyle="round")
        (self.linea,) = ax.plot(self.x, self.y, color=VERDE, linewidth=1.8, solid_capstyle="round")

        f = self.fig
        f.text(0.03, 0.90, "PULSO", color=VERDE, fontsize=15, fontweight="bold")
        f.text(0.10, 0.905, "PPG · fotopletismograma", color=GRIS, fontsize=10)
        f.text(0.80, 0.80, "BPM", color=GRIS, fontsize=16, fontweight="bold")
        self.t_bpm = f.text(0.885, 0.50, "--", color=GRIS, fontsize=92, fontweight="bold",
                            ha="center", va="center")
        self.t_corazon = f.text(0.94, 0.80, "♥", color=VERDE_OSC, fontsize=34, ha="center", va="center")
        f.text(0.885, 0.27, f"límites {LIM_BPM[0]}–{LIM_BPM[1]}", color=GRIS, fontsize=11, ha="center")
        self.t_estado = f.text(0.03, 0.07, "", color=VERDE, fontsize=13, fontweight="bold")
        pie = "Demostración educativa · no es un dispositivo médico"
        if self.simulado:
            pie = "[SIMULACIÓN]  ↑ ↓ cambia BPM · d quita/pone el dedo     " + pie
        f.text(0.03, 0.025, pie, color=GRIS, fontsize=9)

        self.fig.canvas.mpl_connect("key_press_event", self._tecla)
        self.fig.canvas.mpl_connect("close_event", lambda e: self.fuente.cerrar())

    def _tecla(self, ev):
        if not self.simulado:
            return
        if ev.key == "up":
            self.fuente.bpm = min(200, self.fuente.bpm + 10)
        elif ev.key == "down":
            self.fuente.bpm = max(30, self.fuente.bpm - 10)
        elif ev.key == "d":
            self.fuente.sin_dedo = not self.fuente.sin_dedo

    # ---------- datos ----------
    def procesar(self, muestras):
        for t, v in muestras:
            idx = int(((t - self.t0) % VENTANA_S) * BINS_POR_S) % self.N
            if self.ult_idx is not None:
                paso = (idx - self.ult_idx) % self.N
                if 1 < paso < self.N // 2:                  # rellena bins saltados
                    for k in range(1, paso):
                        self.y[(self.ult_idx + k) % self.N] = v
            self.y[idx] = v
            self.ult_idx = idx
            self.t_ult_dato = t
            self.det.agregar(t, v)
        if self.ult_idx is not None:                         # hueco "borrador" delante del trazo
            n = int(HUECO_S * BINS_POR_S)
            self.y[(self.ult_idx + np.arange(1, n + 1)) % self.N] = np.nan

    def _autoescala(self):
        validos = self.y[~np.isnan(self.y)]
        if len(validos) < 50:
            return
        lo, hi = np.percentile(validos, [1, 99])
        rango = max(hi - lo, 40.0)
        centro = (hi + lo) / 2
        obj_min, obj_max = centro - rango * 0.65, centro + rango * 0.65
        self.ymin += 0.08 * (obj_min - self.ymin)            # suavizado: no salta
        self.ymax += 0.08 * (obj_max - self.ymax)
        self.ax.set_ylim(self.ymin, self.ymax)

    def _estado(self, ahora, bpm):
        f = self.fuente
        if f.error:
            return "PUERTO DESCONECTADO", ROJO
        if ahora - self.t_ult_dato > SIN_DATOS_S:
            if f.modo_incorrecto:
                return "ARDUINO EN MODO IBI/LEGIBLE: pon MODO_PLOTTER = true", AMBAR
            return "SIN DATOS DEL ARDUINO (¿puerto/velocidad correctos?)", ROJO
        if self.det.amplitud < AMP_MIN:
            return "SIN SEÑAL · COLOCA EL DEDO", AMBAR
        if bpm is None:
            return "BUSCANDO LATIDOS...", GRIS
        if not LIM_BPM[0] <= bpm <= LIM_BPM[1]:
            return f"PULSO FUERA DEL RANGO ({LIM_BPM[0]}–{LIM_BPM[1]})", ROJO
        return "SEÑAL OK", VERDE

    def actualizar(self, _=None):
        self.procesar(self.fuente.leer())
        ahora = time.perf_counter()
        bpm = self.det.bpm(ahora)
        texto, color = self._estado(ahora, bpm)

        self.linea.set_ydata(self.y)
        self.brillo.set_ydata(self.y)
        self._autoescala()

        self.t_estado.set_text(texto)
        self.t_estado.set_color(color)
        if bpm is None:
            self.t_bpm.set_text("--")
            self.t_bpm.set_color(GRIS)
        else:
            self.t_bpm.set_text(str(int(round(bpm))))
            self.t_bpm.set_color(VERDE if LIM_BPM[0] <= bpm <= LIM_BPM[1] else ROJO)
        latiendo = self.det.ultimo_t is not None and ahora - self.det.ultimo_t < 0.15
        self.t_corazon.set_color(VERDE if latiendo and bpm is not None else VERDE_OSC)
        return []

    def correr(self):
        self.anim = FuncAnimation(self.fig, self.actualizar, interval=int(1000 / FPS),
                                  cache_frame_data=False)
        plt.show()


# ================================== MAIN ====================================
def listar_puertos():
    try:
        from serial.tools import list_ports
        puertos = list(list_ports.comports())
    except Exception:
        puertos = []
    print("No indicaste puerto. Puertos encontrados:")
    for p in puertos:
        print(f"  {p.device}  {p.description}")
    if not puertos:
        print("  (ninguno)")
    print("\nEjemplos:\n  python monitor_pulso.py --puerto COM6\n  python monitor_pulso.py --simular")


def main():
    global VENTANA_S
    ap = argparse.ArgumentParser(description="Monitor de pulso estilo hospital (extra del taller)")
    ap.add_argument("--puerto", help="puerto serial, ej. COM6 o /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200, help="velocidad (debe coincidir con Serial.begin)")
    ap.add_argument("--simular", action="store_true", help="usar una señal simulada (sin hardware)")
    ap.add_argument("--ventana", type=float, default=VENTANA_S, help="segundos visibles en pantalla")
    a = ap.parse_args()
    VENTANA_S = a.ventana

    if a.simular:
        fuente = FuenteSimulada()
    elif a.puerto:
        try:
            fuente = FuenteSerial(a.puerto, a.baud)
        except Exception as e:
            sys.exit(f"No pude abrir {a.puerto}: {e}\n"
                     "¿Está abierto el Monitor Serie o el Serial Plotter? Ciérralos e inténtalo de nuevo.")
    else:
        listar_puertos()
        return
    Monitor(fuente, simulado=a.simular).correr()


if __name__ == "__main__":
    main()
