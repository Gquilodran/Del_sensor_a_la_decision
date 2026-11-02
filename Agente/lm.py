"""
MACHINE LEARNING BASICO PARA EL MONITOR DE PULSO
Deteccion de anomalias con Isolation Forest (aprendizaje NO supervisado).

IDEA EN UNA FRASE
    El modelo mira mucho pulso NORMAL de una persona y aprende como "se ve" lo normal.
    Despues, a cada tramo nuevo de 20 s le pone una nota: si se parece poco a lo
    normal, lo marca como anomalo. Nunca vio un pulso anormal; solo aprendio lo normal.

PASOS
    1) Graba pulso NORMAL en VARIAS sesiones y momentos distintos (recien sentado, despues de
       conversar, de pie, en la tarde...), 3 a 5 min cada una, con el agente:
           python agente.py --puerto COM6 --grabar normal1.txt
           python agente.py --puerto COM6 --grabar normal2.txt     (y asi)
    2) Graba otra sesion para probar (con esfuerzo leve, movimiento, etc.):
           python agente.py --puerto COM6 --grabar prueba.txt
    3) Entrena con las normales y evalua la de prueba:
           python ml_basico.py --entrenar normal1.txt normal2.txt normal3.txt --probar prueba.txt

LIMITACION IMPORTANTE (y buena leccion para el taller)
    Este modelo NO extrapola: lo que no se parezca a lo que vio al entrenar lo marca como anomalo.
    Si solo entrenas con una sesion a 72 BPM, un pulso normal de 76 BPM puede salir "anomalo".
    Por eso hacen falta datos normales variados (y de varias personas, si el modelo sera general).

REQUISITOS:  pip install numpy scikit-learn

AVISO: ejemplo educativo. No es un dispositivo medico.
"""
import argparse
import sys

import numpy as np
from sklearn.ensemble import IsolationForest

IBI_MIN, IBI_MAX = 300, 2000     # ms: fuera de este rango es ruido
VENTANA_S = 20                   # cada "ejemplo" que ve el modelo dura 20 s
PASO_S = 5                       # una ventana nueva cada 5 s
MIN_LATIDOS = 8                  # con menos latidos no se evalua la ventana
NOMBRES = ["bpm", "sdnn_ms", "rmssd_rel", "ibi_min_ms", "ibi_max_ms"]


# ---------- 1. LEER LA GRABACION ----------
def cargar(ruta):
    """Lee un archivo grabado ('t,IBI,812' por linea) y devuelve [(t, ibi), ...] validos."""
    latidos = []
    with open(ruta) as f:
        for fila in f:
            t, _, linea = fila.strip().partition(",")
            if linea.startswith("IBI,"):
                try:
                    ibi = int(linea[4:])
                except ValueError:
                    continue
                if IBI_MIN <= ibi <= IBI_MAX:
                    latidos.append((float(t), ibi))
    return latidos


# ---------- 2. CARACTERISTICAS ("features") ----------
def caracteristicas(ibis):
    """Resume una ventana de IBI en 5 numeros. El modelo trabaja con estos numeros, no con la señal."""
    ibis = np.array(ibis, dtype=float)
    difs = np.diff(ibis)
    return [
        60000 / np.median(ibis),                          # bpm
        ibis.std(),                                       # sdnn: cuanto varian los intervalos
        np.sqrt(np.mean(difs ** 2)) / ibis.mean(),        # rmssd relativo: irregularidad latido a latido
        ibis.min(),
        ibis.max(),
    ]


def ventanas(latidos):
    """Parte la grabacion en ventanas de 20 s. Devuelve (hora_inicio, caracteristicas)."""
    if not latidos:
        return []
    t_ini, t_fin = latidos[0][0], latidos[-1][0]
    salida = []
    inicio = t_ini
    while inicio + VENTANA_S <= t_fin:
        ibis = [i for t, i in latidos if inicio <= t < inicio + VENTANA_S]
        if len(ibis) >= MIN_LATIDOS:
            salida.append((inicio, caracteristicas(ibis)))
        inicio += PASO_S
    return salida


# ---------- 3. ENTRENAR ----------
def entrenar(rutas, porcentaje):
    filas = []
    for ruta in rutas:                      # cada archivo se parte por separado
        filas += [c for _, c in ventanas(cargar(ruta))]
    if len(filas) < 20:
        sys.exit(f"Muy pocos datos ({len(filas)} ventanas). Graba al menos 3 a 5 minutos de pulso normal.")
    X = np.array(filas)
    modelo = IsolationForest(n_estimators=200, random_state=0).fit(X)
    # Nota de cada ventana normal: mas baja = mas rara. El umbral deja fuera al 'porcentaje' mas raro.
    notas = modelo.score_samples(X)
    umbral = np.percentile(notas, porcentaje)
    print(f"Entrenado con {len(X)} ventanas normales ({len(X) * PASO_S / 60:.1f} min aprox.).")
    print(f"Umbral de anomalia: nota < {umbral:.3f}  (deja fuera al {porcentaje}% mas raro del entrenamiento)")
    print("Rango normal aprendido:")
    for i, n in enumerate(NOMBRES):
        print(f"  {n:11s}  {X[:, i].min():8.2f}  a  {X[:, i].max():8.2f}")
    return modelo, umbral


# ---------- 4. PROBAR ----------
def probar(modelo, umbral, ruta):
    datos = ventanas(cargar(ruta))
    if not datos:
        sys.exit("No hay ventanas utilizables en el archivo de prueba.")
    X = np.array([c for _, c in datos])
    notas = modelo.score_samples(X)
    print(f"\n{'t(s)':>6} {'bpm':>5} {'sdnn':>6} {'rmssd':>6} {'nota':>7}   resultado")
    anomalas = 0
    for (t, c), nota in zip(datos, notas):
        es_anomala = nota < umbral
        anomalas += es_anomala
        print(f"{t:6.0f} {c[0]:5.0f} {c[1]:6.1f} {c[2]:6.2f} {nota:7.3f}   {'<<< ANOMALA' if es_anomala else 'normal'}")
    print(f"\n{anomalas} de {len(datos)} ventanas marcadas como anomalas ({100 * anomalas / len(datos):.0f}%).")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Deteccion de anomalias con Isolation Forest")
    p.add_argument("--entrenar", required=True, nargs="+", help="uno o varios archivos grabados con pulso NORMAL")
    p.add_argument("--probar", help="archivo grabado a evaluar")
    p.add_argument("--porcentaje", type=float, default=2.0,
                   help="% de ventanas normales que se aceptan como 'raras' al fijar el umbral (por defecto 2)")
    a = p.parse_args()
    modelo, umbral = entrenar(a.entrenar, a.porcentaje)
    if a.probar:
        probar(modelo, umbral, a.probar)