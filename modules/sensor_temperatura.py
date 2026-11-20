"""
modules/sensor_temperatura.py

Cliente de la temperatura ambiente de la sala.

La lectura real la hace sensor_exporter.py, que corre en el nodo del cluster con los
sensores USB. Este modulo la consulta por HTTP (endpoint /temperatura, JSON).

Backends (SENSOR_BACKEND en config/settings.py):

  HTTP  - consulta SENSOR_HTTP_URL.
  MOCK  - datos simulados para desarrollo sin sensor fisico.

Si se deja en AUTO equivale a HTTP. MOCK solo se usa si se configura de forma
explicita: nunca sustituye a una lectura real que ha fallado.

Diagnostico rapido:
    python -m modules.sensor_temperatura
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from config.settings import (
        SENSOR_BACKEND,
        SENSOR_HTTP_URL,
        SENSOR_POLL_INTERVAL,
    )
except ImportError:
    SENSOR_BACKEND       = "AUTO"
    SENSOR_HTTP_URL      = "http://localhost:8001/temperatura"
    SENSOR_POLL_INTERVAL = 30


# ---------------------------------------------------------------------------
# Modelo de lectura
# ---------------------------------------------------------------------------
@dataclass
class LecturaSensor:
    temperatura: float
    backend:     str
    timestamp:   float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "temperatura": round(self.temperatura, 2),
            "backend":     self.backend,
            "timestamp":   self.timestamp,
        }


# ---------------------------------------------------------------------------
# Cliente principal
# ---------------------------------------------------------------------------
class SensorTemperatura:
    """
    Lee la temperatura ambiente del sensor configurado.

    Uso tipico (desde la API, en tarea de fondo):
        sensor = SensorTemperatura()
        while True:
            lectura = sensor.leer()
            if lectura:
                metricas.actualizar_sensor(lectura.temperatura)
            await asyncio.sleep(SENSOR_POLL_INTERVAL)
    """

    _ORDEN_AUTO = ["HTTP"]   # MOCK solo si se configura de forma explicita

    def __init__(self):
        self._backend_activo: Optional[str] = None
        self._ultima_lectura: Optional[LecturaSensor] = None

        configured = SENSOR_BACKEND.upper()
        if configured != "AUTO":
            self._backend_activo = configured
            logger.info("[Sensor] Backend configurado manualmente: %s", configured)
        else:
            logger.info("[Sensor] Modo AUTO — se detectara el backend en el primer leer()")

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------
    def leer(self) -> Optional[LecturaSensor]:
        """
        Lee la temperatura. Devuelve None si ningun backend real responde.

        En modo AUTO se usa el backend HTTP. MOCK queda fuera de esa
        autodeteccion a proposito: si nada real responde, este metodo
        devuelve None en lugar de sustituir la lectura por un valor
        simulado. El unico caso en que se sirve un valor de MOCK es cuando
        SENSOR_BACKEND se ha configurado como "MOCK" de forma explicita,
        uso pensado para desarrollo sin sensor fisico conectado.
        """
        metodos = {
            "HTTP":  self._leer_http,
            "MOCK":  self._leer_mock,
        }

        if self._backend_activo and self._backend_activo != "AUTO":
            resultado = metodos[self._backend_activo]()
            if resultado:
                self._ultima_lectura = resultado
                return resultado
            # El backend activo fallo, reintentar autodeteccion
            logger.warning(
                "[Sensor] Backend %s fallo, re-detectando...", self._backend_activo
            )
            self._backend_activo = None

        # Autodeteccion solo entre backends reales
        for nombre in self._ORDEN_AUTO:
            resultado = metodos[nombre]()
            if resultado:
                self._backend_activo = nombre
                self._ultima_lectura = resultado
                logger.info("[Sensor] Backend detectado: %s", nombre)
                return resultado

        logger.warning(
            "[Sensor] Ningun backend real disponible. No se publica ninguna "
            "lectura simulada; el llamador debe tratar esto como sin datos."
        )
        return None

    @property
    def ultima_lectura(self) -> Optional[LecturaSensor]:
        return self._ultima_lectura

    @property
    def backend(self) -> str:
        return self._backend_activo or "sin_detectar"

    def esta_disponible(self) -> bool:
        return self._ultima_lectura is not None

    # ------------------------------------------------------------------
    # Backends de lectura
    # ------------------------------------------------------------------
    def _leer_http(self) -> Optional[LecturaSensor]:
        """
        Lee de sensor_exporter.py. Respuesta JSON esperada:
            {"temperatura": 22.5, "status": "OK"}
        (tambien acepta "temperature"/"temp")
        """
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(SENSOR_HTTP_URL, timeout=3) as resp:
                body = resp.read().decode()

            data = json.loads(body)
            temp = (data.get("temperatura") or data.get("temperature")
                    or data.get("temp"))
            if temp is not None:
                logger.debug("[Sensor/HTTP] %.1fC", temp)
                return LecturaSensor(float(temp), "HTTP")

        except Exception as e:
            logger.debug("[Sensor/HTTP] No disponible en %s: %s", SENSOR_HTTP_URL, e)
        return None

    def _leer_mock(self) -> LecturaSensor:
        """
        Datos simulados para desarrollo sin sensor fisico.
        Temperatura oscila entre 20-24C en un ciclo de 5 minutos.
        """
        import math
        t    = time.time()
        temp = 22.0 + 2.0 * math.sin(t / 300)
        logger.debug("[Sensor/MOCK] %.1fC", temp)
        return LecturaSensor(round(temp, 1), "MOCK")


# ---------------------------------------------------------------------------
# CLI - diagnostico del sensor
# ---------------------------------------------------------------------------
def _configurar_logging_cli() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )


def main() -> int:
    import sys
    _configurar_logging_cli()

    sensor = SensorTemperatura()

    print(f"\n{'='*60}")
    print("  Diagnostico del Sensor de Temperatura")
    print(f"{'='*60}")
    print(f"  Backend configurado: {SENSOR_BACKEND}")
    print(f"  URL HTTP a probar:   {SENSOR_HTTP_URL}")
    print(f"{'='*60}\n")

    # Probar cada backend manualmente
    backends = {
        "HTTP":  sensor._leer_http,
        "MOCK (siempre funciona)": sensor._leer_mock,
    }

    for nombre, metodo in backends.items():
        print(f"  Probando {nombre}... ", end="", flush=True)
        try:
            res = metodo()
            if res:
                print(f"OK  →  {res.temperatura:.1f}C")
            else:
                print("no disponible")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\n  Backend auto-detectado: {sensor.leer().backend if sensor.leer() else 'ninguno'}")
    print()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
