"""
AGENTE DE MONITOREO CARDIACO

Ciclo del agente:   PERCIBIR  ->  DECIDIR  ->  ACTUAR

  percibir : lee lineas del Arduino  ('IBI,812' / 'NOSIGNAL'), filtra ruido,
             aprende la linea base de la persona y calcula BPM y variabilidad
  decidir  : clasifica en niveles 0-3 y los pasa por una maquina de estados
             con persistencia (subir rapido) e histeresis (bajar lento)
  actuar   : pantalla, registro CSV, pitido y correo (con cola si no hay red)

REQUISITOS
    pip install pyserial

ARDUINO (firmware pulso_serial.ino)
    MODO_PLOTTER = false;   MODO_LEGIBLE = false;   Serial.begin(115200);
    Cierra el Monitor Serie y el Serial Plotter antes de ejecutar este programa.

USO
    python agente.py                          # lista los puertos disponibles
    python agente.py --puerto COM6            # en vivo con el sensor
    python agente.py --puerto COM6 --grabar sesion.txt     # y graba los datos
    python agente.py --archivo sesion.txt --rapido         # repite una sesion (plan B)
    python agente.py --simular --rapido       # escenario de demostracion, sin hardware

CORREO: define las variables de entorno SMTP_USER, SMTP_PASS y
CORREO_DESTINO. Si no hay internet o faltan, los correos quedan en cola_correos/.
"""
import argparse
import csv
import datetime
import glob
import os
import random
import smtplib
import statistics as st
import sys
import time
from collections import deque
from email.message import EmailMessage

# ================= PARAMETROS =================
VENTANA_S = 20                  # segundos de historia que mira el agente
CALIB_LATIDOS = 20              # latidos para aprender la linea base
CALIB_TOLERANCIA = 0.25         # en la calibracion se descartan BPM a mas de 25 % de la mediana
DESCARTAR_INICIO = 3            # primeros latidos validos: el sensor se estabiliza
IBI_MIN, IBI_MAX = 300, 2000    # ms; fuera de este rango es ruido y se descarta
SIGMA_MIN = 6.0                 # BPM; desviacion minima aceptada en la base

LIM_SUAVE = (50, 110)           # BPM fuera de este rango -> nivel 1
LIM_DURO = (40, 150)            # BPM fuera de este rango -> nivel 2
LIM_CRITICO = (35, 180)         # BPM fuera de este rango -> nivel 3
Z_SOSPECHOSO, Z_ANORMAL = 2.0, 3.0          # distancia a la base personal (en sigmas)
VAR_SOSPECHOSO, VAR_ANORMAL = 0.12, 0.20    # irregularidad del ritmo
SIN_LATIDO_S = (3, 8, 15)       # segundos sin latidos -> nivel 1, 2, 3

PERSISTENCIA = {1: 4, 2: 8, 3: 5}   # segundos que debe durar un nivel para cambiar de estado
RECUPERACION_S = 10             # segundos de calma para bajar UN estado
REPETIR_CRITICO_S = 30          # segundos entre avisos repetidos en estado critico
COOLDOWN_CORREO_S = 300         # segundos minimos entre correos del mismo estado
DIAGNOSTICO_S = 8               # a los 8 s sin latidos validos, el agente explica que revisar

ESTADOS = ["NORMAL", "SOSPECHOSO", "CONFIRMADO", "CRITICO"]
ARCHIVO_REGISTRO = "registro_eventos.csv"
CARPETA_COLA = "cola_correos"


# ================================ PARSEO =====================================
def parsear(linea):
    """'IBI,812' -> 812.  Devuelve None si no es un IBI valido (basura o fuera de rango)."""
    if not linea.startswith("IBI,"):
        return None
    try:
        ibi = int(linea[4:])
    except ValueError:
        return None
    if not IBI_MIN <= ibi <= IBI_MAX:
        return None
    return ibi


# ============================== ACCIONES =====================================
def pitar(largo=False):
    try:
        import winsound
        winsound.Beep(1000, 700 if largo else 200)
    except ImportError:
        print("\a", end="", flush=True)


def registrar_csv(t, anterior, nuevo, bpm, var, motivo):
    nuevo_archivo = not os.path.exists(ARCHIVO_REGISTRO)
    with open(ARCHIVO_REGISTRO, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo_archivo:
            w.writerow(["fecha_hora", "t_s", "estado_anterior", "estado_nuevo",
                        "bpm", "variabilidad", "motivo"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), f"{t:.1f}",
                    anterior, nuevo,
                    "" if bpm is None else f"{bpm:.0f}",
                    "" if var is None else f"{var:.2f}", motivo])


def plantilla(estado, motivo, bpm, base):
    bpm_txt = "sin datos" if bpm is None else f"{bpm:.0f} BPM"
    return (f"ALERTA ({estado}) - monitor de pulso\n"
            f"Hora: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Pulso estimado: {bpm_txt} (base personal: {base:.0f} BPM)\n"
            f"Motivo: {motivo}\n\n"
            "Sistema educativo, no es un dispositivo medico. Verifique a la persona "
            "y el sensor; ante la duda, busque atencion medica.")


def redactar_ollama(estado, motivo, bpm, base):
    """OPCIONAL: Ollama solo redacta el texto. La decision ya la tomaron las reglas."""
    import ollama
    bpm_txt = "sin datos" if bpm is None else f"{bpm:.0f}"
    r = ollama.chat(model="llama3.2:3b", options={"temperature": 0}, messages=[{
        "role": "user",
        "content": (f"Redacta en espanol una alerta de 3 lineas. Estado: {estado}. "
                    f"Pulso: {bpm_txt} BPM (base {base:.0f}). Motivo: {motivo}. "
                    "No des diagnosticos. Aclara que es un sistema educativo.")}])
    return r["message"]["content"]


def enviar_smtp(asunto, cuerpo):
    usuario = os.environ.get("SMTP_USER")
    clave = os.environ.get("SMTP_PASS")
    destino = os.environ.get("CORREO_DESTINO")
    if not (usuario and clave and destino):
        raise RuntimeError("faltan variables de entorno")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = asunto, usuario, destino
    msg.set_content(cuerpo)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
        s.login(usuario, clave)
        s.send_message(msg)


def enviar_o_encolar(asunto, cuerpo):
    try:
        enviar_smtp(asunto, cuerpo)
        print("   [correo] enviado")
    except Exception as e:
        os.makedirs(CARPETA_COLA, exist_ok=True)
        ruta = os.path.join(CARPETA_COLA, f"{int(time.time() * 1000)}.txt")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(asunto + "\n" + cuerpo)
        print(f"   [correo] no enviado ({type(e).__name__}); guardado en {ruta}")


def reintentar_cola():
    for ruta in sorted(glob.glob(os.path.join(CARPETA_COLA, "*.txt"))):
        with open(ruta, encoding="utf-8") as f:
            asunto, _, cuerpo = f.read().partition("\n")
        try:
            enviar_smtp(asunto, cuerpo)
            os.remove(ruta)
            print(f"   [correo] pendiente enviado: {ruta}")
        except Exception:
            return  # sigue sin red; se intentara mas tarde


# ================================ AGENTE =====================================
class Agente:
    def __init__(self, usar_ollama=False):
        self.usar_ollama = usar_ollama
        # --- memoria ---
        self.latidos = deque()          # (t, ibi) de la ventana
        self.calib = []                 # BPM de la calibracion
        self.vistos = 0                 # latidos validos recibidos
        self.mu = self.sigma = None     # linea base personal (media y desviacion de BPM)
        self.ultimo_latido = None
        # --- estado de la maquina ---
        self.estado = 0                 # indice en ESTADOS
        self.desde = {1: None, 2: None, 3: None}   # desde cuando se sostiene cada nivel
        self.bajo_desde = None
        self.ultimo_correo = {}
        self.t_ultima_alerta = 0.0
        # --- diagnostico ---
        self.t_inicio = None
        self.rx_ibi = 0                 # lineas IBI validas
        self.rx_otras = 0               # lineas con otro formato (ej. 'BPM: 73' o numeros crudos)
        self.descartados = 0            # IBI fuera de rango
        self.avisado = False
        self.t_print = -999.0

    # ---------- PERCIBIR ----------
    def percibir(self, t, linea):
        if self.t_inicio is None:
            self.t_inicio = t
        if linea.startswith("IBI,"):
            ibi = parsear(linea)
            if ibi is None:
                self.descartados += 1
                return
        elif linea == "NOSIGNAL":
            return                      # el silencio lo detecta decidir() con el reloj
        else:
            self.rx_otras += 1
            return
        self.rx_ibi += 1
        self.ultimo_latido = t
        self.vistos += 1
        if self.mu is None:             # todavia calibrando
            self._calibrar(ibi)
            return
        self.latidos.append((t, ibi))

    def _calibrar(self, ibi):
        if self.vistos <= DESCARTAR_INICIO:
            return
        self.calib.append(60000 / ibi)
        n = len(self.calib)
        if n < CALIB_LATIDOS:
            if n % 5 == 0:
                print(f"[calibrando] {n}/{CALIB_LATIDOS} latidos (quedate quieto, sentado)")
            return
        # Se descartan los BPM que se alejan mucho de la mediana (ruido del arranque)
        med = st.median(self.calib)
        buenos = [b for b in self.calib if abs(b - med) <= CALIB_TOLERANCIA * med]
        if len(buenos) < 0.7 * CALIB_LATIDOS:
            print("[calibrando] senal inestable (movimiento o mal contacto); reinicio la calibracion")
            self.calib = []
            return
        self.mu = st.mean(buenos)
        self.sigma = max(st.pstdev(buenos), SIGMA_MIN)
        print(f"[calibrado] base = {self.mu:.0f} BPM (sigma {self.sigma:.1f}). Monitoreando...")

    def metricas(self, t):
        while self.latidos and t - self.latidos[0][0] > VENTANA_S:
            self.latidos.popleft()
        ibis = [i for _, i in self.latidos]
        if len(ibis) < 8:
            return None
        bpm = 60000 / st.median(ibis)               # la mediana no se deja mover por un latido falso
        difs = [b - a for a, b in zip(ibis, ibis[1:])]
        rmssd = (sum(d * d for d in difs) / len(difs)) ** 0.5
        return bpm, rmssd / st.mean(ibis)           # variabilidad relativa al IBI medio

    # ---------- DECIDIR (1): nivel instantaneo 0-3 ----------
    def nivel(self, t):
        # Pregunta 1: puedo confiar en la senal? (el silencio va primero)
        sin = t - self.ultimo_latido
        if sin > SIN_LATIDO_S[0]:
            n = sum(sin > s for s in SIN_LATIDO_S)
            return n, f"sin latidos hace {sin:.0f} s (sensor suelto o ausencia de pulso)", None, None
        m = self.metricas(t)
        if m is None:
            return 0, "pocos datos", None, None
        bpm, var = m
        z = (bpm - self.mu) / self.sigma
        # Preguntas 2, 3 y 4: limites absolutos, base personal y regularidad
        reglas = [
            (3, bpm < LIM_CRITICO[0] or bpm > LIM_CRITICO[1], f"BPM {bpm:.0f} en limite critico"),
            (2, bpm < LIM_DURO[0] or bpm > LIM_DURO[1], f"BPM {bpm:.0f} fuera del rango duro"),
            (2, abs(z) > Z_ANORMAL, f"BPM {bpm:.0f} muy lejos de su base ({self.mu:.0f})"),
            (2, var > VAR_ANORMAL, f"ritmo muy irregular (var {var:.2f})"),
            (1, bpm < LIM_SUAVE[0] or bpm > LIM_SUAVE[1], f"BPM {bpm:.0f} fuera del rango habitual"),
            (1, abs(z) > Z_SOSPECHOSO, f"BPM {bpm:.0f} algo lejos de su base ({self.mu:.0f})"),
            (1, var > VAR_SOSPECHOSO, f"ritmo algo irregular (var {var:.2f})"),
        ]
        nivel, motivo = 0, "dentro de lo esperado"
        for n, cond, texto in reglas:
            if cond and n > nivel:
                nivel, motivo = n, texto
        return nivel, motivo, bpm, var

    # ---------- DECIDIR (2): maquina de estados ----------
    def decidir(self, t):
        if self.t_inicio is None:
            self.t_inicio = t
        self._diagnosticar(t)
        if self.mu is None or self.ultimo_latido is None:
            return
        nivel, motivo, bpm, var = self.nivel(t)

        if t - self.t_print >= 5:
            self.t_print = t
            b = "--" if bpm is None else f"{bpm:.0f}"
            v = "--" if var is None else f"{var:.2f}"
            print(f"[t={t:6.0f}s] BPM {b:>3}  var {v}  nivel {nivel}  estado {ESTADOS[self.estado]}")

        # 1. desde cuando se sostiene cada nivel
        for k in (1, 2, 3):
            if nivel >= k:
                if self.desde[k] is None:
                    self.desde[k] = t
            else:
                self.desde[k] = None
        # 2. que estado se gano por persistencia
        objetivo = 0
        for k in (1, 2, 3):
            if self.desde[k] is not None and t - self.desde[k] >= PERSISTENCIA[k]:
                objetivo = k
        # 3. subir rapido, bajar lento (histeresis)
        if objetivo > self.estado:
            self.cambiar_estado(t, objetivo, motivo, bpm, var)
            self.bajo_desde = None
        elif nivel < self.estado:
            if self.bajo_desde is None:
                self.bajo_desde = t
            elif t - self.bajo_desde >= RECUPERACION_S:
                self.cambiar_estado(t, self.estado - 1, "recuperando: " + motivo, bpm, var)
                self.bajo_desde = None
        else:
            self.bajo_desde = None
            if self.estado == 3 and t - self.t_ultima_alerta >= REPETIR_CRITICO_S:
                self.t_ultima_alerta = t
                print(f"   !!! SIGUE EN ESTADO CRITICO: {motivo}")
                pitar(largo=True)

    def _diagnosticar(self, t):
        """Si despues de DIAGNOSTICO_S no hay latidos validos, explica que revisar (una vez)."""
        if self.mu is not None or self.avisado or t - self.t_inicio < DIAGNOSTICO_S or self.rx_ibi > 0:
            return
        self.avisado = True
        if self.rx_otras > 20:
            print("[aviso] llegan datos, pero no con formato 'IBI,xxx'. En el Arduino pon "
                  "MODO_PLOTTER = false y MODO_LEGIBLE = false, y vuelve a subir el programa.")
        else:
            print("[aviso] aun no llegan latidos. Coloca el dedo en el sensor y revisa el puerto, "
                  "los baudios (115200) y que el Monitor Serie este cerrado.")

    # ---------- ACTUAR ----------
    def cambiar_estado(self, t, nuevo, motivo, bpm, var):
        anterior = self.estado
        self.estado = nuevo
        registrar_csv(t, ESTADOS[anterior], ESTADOS[nuevo], bpm, var, motivo)
        print(f">>> t={t:.0f}s  {ESTADOS[anterior]} -> {ESTADOS[nuevo]}  ({motivo})")
        if nuevo < anterior:
            return                      # recuperacion: solo se registra
        self.t_ultima_alerta = t
        if nuevo >= 2:
            pitar(largo=(nuevo == 3))
            if t - self.ultimo_correo.get(nuevo, -1e9) >= COOLDOWN_CORREO_S:
                self.ultimo_correo[nuevo] = t
                try:
                    cuerpo = (redactar_ollama(ESTADOS[nuevo], motivo, bpm, self.mu)
                              if self.usar_ollama else None)
                except Exception:
                    cuerpo = None       # si Ollama falla, se usa la plantilla
                cuerpo = cuerpo or plantilla(ESTADOS[nuevo], motivo, bpm, self.mu)
                enviar_o_encolar(f"[Monitor de pulso] Estado {ESTADOS[nuevo]}", cuerpo)
            else:
                print("   [correo] omitido por enfriamiento")


# ============================= ORIGENES DE DATOS =============================
def modo_serial(agente, puerto, baud, grabar):
    try:
        import serial
    except ImportError:
        sys.exit("Falta pyserial. Instalalo con:  pip install pyserial")
    try:
        ser = serial.Serial(puerto, baud, timeout=1)
    except Exception as e:
        sys.exit(f"No pude abrir {puerto}: {e}\n"
                 "Cierra el Monitor Serie / Serial Plotter del Arduino IDE e intentalo de nuevo.")
    time.sleep(2)                       # el Arduino se reinicia al abrir el puerto
    ser.reset_input_buffer()
    f = open(grabar, "a") if grabar else None
    t0 = time.monotonic()
    t_cola = t0
    print(f"Escuchando {puerto} a {baud} baudios... (Ctrl+C para salir)")
    try:
        while True:
            try:
                linea = ser.readline().decode(errors="ignore").strip()
            except serial.SerialException as e:
                print(f"\n[error] se perdio la conexion con el puerto: {e}")
                break
            t = time.monotonic() - t0
            if linea:
                if f:
                    f.write(f"{t:.3f},{linea}\n")
                    f.flush()
                agente.percibir(t, linea)
            agente.decidir(t)           # SE EJECUTA AUNQUE NO LLEGUE NADA (detecta el silencio)
            if time.monotonic() - t_cola > 60:
                t_cola = time.monotonic()
                reintentar_cola()
    except KeyboardInterrupt:
        print("\nFin.")
    finally:
        ser.close()
        if f:
            f.close()


def eventos_archivo(ruta):
    with open(ruta) as f:
        for fila in f:
            t, _, linea = fila.strip().partition(",")
            if linea:
                yield float(t), linea


def eventos_simulados(semilla=1):
    """Escenario de demostracion: reposo, taquicardia, reposo, ritmo irregular, corte, reposo."""
    random.seed(semilla)
    fases = [(45, 72, "normal"), (30, 135, "normal"), (25, 72, "normal"),
             (30, 80, "irregular"), (30, 0, "sinsenal"), (35, 72, "normal")]
    t = 0.0
    for dur, bpm, tipo in fases:
        fin = t + dur
        if tipo == "sinsenal":
            for s in range(3, dur):
                yield t + s, "NOSIGNAL"
            t = fin
            continue
        while t < fin:
            ibi = random.uniform(450, 1300) if tipo == "irregular" \
                else 60000 / bpm + random.gauss(0, 25)
            t += ibi / 1000
            yield t, f"IBI,{int(ibi)}"


def reproducir(agente, eventos, rapido):
    """Entrega los eventos al agente; avanza el reloj segundo a segundo para que detecte el silencio."""
    previo = 0.0
    for t, linea in eventos:
        if not rapido:
            time.sleep(max(0.0, t - previo))
        # el tiempo pasa aunque no lleguen lineas: asi se detecta el sensor suelto
        while previo + 1.0 < t:
            previo += 1.0
            agente.decidir(previo)
        previo = t
        agente.percibir(t, linea)
        agente.decidir(t)


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
    print("\nEjemplos:\n  python agente.py --puerto COM6\n  python agente.py --simular --rapido")


# ================================== MAIN =====================================
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Agente de monitoreo cardiaco")
    p.add_argument("--puerto", help="puerto serial, ej. COM6 o /dev/ttyACM0")
    p.add_argument("--baud", type=int, default=115200, help="debe coincidir con Serial.begin (por defecto 115200)")
    p.add_argument("--grabar", help="guarda los datos crudos en este archivo")
    p.add_argument("--archivo", help="repite datos grabados (plan B)")
    p.add_argument("--simular", action="store_true", help="escenario de demostracion")
    p.add_argument("--rapido", action="store_true", help="sin esperas (archivo/simulacion)")
    p.add_argument("--ollama", action="store_true", help="redactar los correos con Ollama (opcional)")
    a = p.parse_args()

    ag = Agente(usar_ollama=a.ollama)
    if a.puerto:
        modo_serial(ag, a.puerto, a.baud, a.grabar)
    elif a.archivo:
        reproducir(ag, eventos_archivo(a.archivo), a.rapido)
    elif a.simular:
        reproducir(ag, eventos_simulados(), a.rapido)
    else:
        listar_puertos()