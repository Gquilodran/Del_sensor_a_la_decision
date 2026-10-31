/*  
 *   Flujo de trabajo de la electronica
 *   
 *   Sensor de pulso -> Arduino -> Serial
 *   Salida por serial (9600 baudios)
 *   IBI,812      -> intervalo entre latidos en milisegundos (BPM = 60000/812)
 *   NOSIGNAL     -> no se detecta latido hace 2.5 s (dedo suelto o sin pulso)
 *   Conexion: sensor cable amarillo -> A0, VCC (cable rojo) -> 5V (o 3.3V), GND (cable negro) -> GND
*/

const int PIN_PULSO = A0; /*Pueden conectarlo a otro pin, pero si o si debe ser a uno analogico*/


const unsigned long MUESTREO_MS = 2;       /* lee el sensor cada 2 ms*/
const unsigned long REFRACTARIO_MS = 250;  /* ignora "latidos" mas rapidos (ruido)*/
const unsigned long SIN_SENAL_MS = 2500;   /* tiempo sin latido. Servira para notificar al agente*/

const bool MODO_PLOTTER = false;  /* true = envia señal cruda para el Serial Plotter*/
 const bool MODO_LEGIBLE = true;   /*Ver pulso normal (lectura humana*/

int pico = 512, valle = 512, umbral = 530;
bool enPulso = false;
unsigned long ultimoLatido = 0;
unsigned long ultimaMuestra = 0;
unsigned long ultimoAviso = 0;

void setup() {
  Serial.begin(115200);

}

void loop() {
  unsigned long ahora = millis();
  if (ahora - ultimaMuestra < MUESTREO_MS) return;
  ultimaMuestra = ahora;

  int v = analogRead(PIN_PULSO);

  if (MODO_PLOTTER) {
    Serial.println(v);
    return;
  }

  // Umbral adaptativo: sigue el pico y el valle de la señal
  if (v < umbral && v < valle) valle = v;
  if (v > umbral && v > pico) pico = v;

  // Subida de la señal por encima del umbral = latido
  if (v > umbral && !enPulso && (ahora - ultimoLatido) > REFRACTARIO_MS) {
    enPulso = true;
    if (ultimoLatido > 0) {
      unsigned long ibi = ahora - ultimoLatido;
      if (MODO_LEGIBLE) {
        Serial.print("BPM: ");
        Serial.println(60000UL / ibi);
      } else {
        Serial.print("IBI,");
        Serial.println(ibi);
      }
    }
    ultimoLatido = ahora;
  }

  // Bajada de la señal: recalcula el umbral para el siguiente latido
  if (v < umbral && enPulso) {
    enPulso = false;
    int amplitud = pico - valle;
    umbral = valle + amplitud / 2;
    pico = umbral;
    valle = umbral;
  }

  // Sin latidos: reinicia y avisa
  if (ultimoLatido > 0 && (ahora - ultimoLatido) > SIN_SENAL_MS) {
    umbral = 530; pico = 512; valle = 512;
    enPulso = false;
    if (ahora - ultimoAviso > 1000) {
      Serial.println("NOSIGNAL");
      ultimoAviso = ahora;
    }
  }
}
