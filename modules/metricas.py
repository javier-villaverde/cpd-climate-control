"""
modules/metricas.py

Registro centralizado de metricas Prometheus del sistema TFG Control AC.

Metricas expuestas:
  ac_encendido                  Gauge   Estado del AC (1=ON, 0=OFF)
  ac_temperatura_objetivo_celsius Gauge Temperatura configurada en el AC
  ac_led_detectado              Gauge   Resultado ultimo de deteccion por camara
                                        (1=ON, 0=OFF, -1=sin datos)
  ac_temperatura_ambiente_celsius Gauge Temperatura de la sala (sensor externo)
  ac_comandos_total             Counter Comandos IR enviados (labels: boton, exito)

Uso:
    import modules.metricas as metricas
    metricas.actualizar_desde_estado(controlador.estado)
    metricas.registrar_comando("POWER_ON", exito=True)
    metricas.actualizar_led(True)
    metricas.actualizar_sensor(22.5)
"""

import logging
from typing import Optional

from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gauges - estado actual del AC
# ---------------------------------------------------------------------------
g_ac_encendido = Gauge(
    "ac_encendido",
    "Estado del AC: 1 encendido, 0 apagado",
)

g_ac_temperatura_objetivo = Gauge(
    "ac_temperatura_objetivo_celsius",
    "Temperatura objetivo configurada en el AC (grados Celsius)",
)

g_ac_led_detectado = Gauge(
    "ac_led_detectado",
    "Resultado de la ultima deteccion del LED por camara: 1 ON, 0 OFF, -1 sin datos",
)

# ---------------------------------------------------------------------------
# Gauges - sensor de temperatura ambiente
# ---------------------------------------------------------------------------
g_temperatura_ambiente = Gauge(
    "ac_temperatura_ambiente_celsius",
    "Temperatura ambiente de la sala medida por el sensor (grados Celsius)",
)

# ---------------------------------------------------------------------------
# Counters - actividad IR
# ---------------------------------------------------------------------------
c_comandos = Counter(
    "ac_comandos_total",
    "Numero total de comandos IR enviados al AC",
    ["boton", "exito"],   # exito = 'true' | 'false'
)


# ---------------------------------------------------------------------------
# Funciones de actualizacion
# ---------------------------------------------------------------------------
def actualizar_desde_estado(estado) -> None:
    """
    Sincroniza las metricas de Prometheus con el EstadoAC actual.
    Llamar tras cualquier cambio de estado exitoso.

    Args:
        estado: instancia de EstadoAC
    """
    g_ac_encendido.set(1 if estado.encendido else 0)

    if estado.temperatura is not None:
        g_ac_temperatura_objetivo.set(estado.temperatura)

    logger.debug(
        "[metricas] Estado actualizado: encendido=%s temp=%s",
        estado.encendido, estado.temperatura,
    )


def registrar_comando(boton: str, exito: bool) -> None:
    """
    Incrementa el contador de comandos IR enviados.

    Args:
        boton:  nombre del boton (ej: "POWER_ON", "TEMP_22")
        exito:  True si Broadlink confirmo la emision
    """
    c_comandos.labels(boton=boton, exito=str(exito).lower()).inc()
    logger.debug("[metricas] Comando registrado: %s exito=%s", boton, exito)


def actualizar_led(detectado: Optional[bool]) -> None:
    """
    Actualiza la metrica del LED tras una lectura de camara.

    Args:
        detectado: True=ON, False=OFF, None=camara no disponible
    """
    if detectado is None:
        g_ac_led_detectado.set(-1)
    else:
        g_ac_led_detectado.set(1 if detectado else 0)


def actualizar_sensor(temperatura: float) -> None:
    """
    Actualiza la metrica de temperatura ambiente.

    Args:
        temperatura: grados Celsius
    """
    g_temperatura_ambiente.set(temperatura)
    logger.debug("[metricas] Sensor actualizado: %.1fC", temperatura)
