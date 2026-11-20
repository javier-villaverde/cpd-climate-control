"""
modules/controlador_ac.py

Orquestador central del sistema de control AC.

Responsabilidad: coordinar la emision IR (BroadlinkClient), la lectura
visual de estado (DetectorLED) y la persistencia de estado (EstadoAC).

Flujo de cada comando:
    1. SIEMPRE: leer camara para conocer el estado real actual del AC
    2. Decidir si hace falta emitir IR (p.ej. si ya esta encendido, no emitir POWER_ON)
    3. Emitir codigo IR via Broadlink
    4. Registrar resultado en estado y log
    5. Devolver dict de resultado estructurado

Nota sobre temperaturas:
    Los codigos IR del AC son stateful: TEMP_22 significa "pon el AC a 22C"
    (no "sube/baja desde donde estes"). Se pueden mandar de dos formas:
      - Modo DIRECTO:      envia TEMP_<objetivo> directamente (1 solo codigo)
      - Modo ESCALONADO:   envia las transiciones una a una (TEMP_20_21, TEMP_21_22...)
                          Recomendado por el tutor para garantizar que el AC lo procesa.
    Ambos modos estan disponibles via el parametro `escalonado` en set_temperatura().
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from config.settings import (
        CODIGOS_AC,
        DELAY_ENTRE_PULSACIONES,
        DELAY_REINTENTO,
        MAX_REINTENTOS,
        TEMP_MAX,
        TEMP_MIN,
    )
except ImportError:
    CODIGOS_AC = Path("config/codigos_ac.json")
    MAX_REINTENTOS = 3
    DELAY_REINTENTO = 2.0
    DELAY_ENTRE_PULSACIONES = 0.5
    TEMP_MIN = 20
    TEMP_MAX = 23

import json

from modules.broadlink_client import BroadlinkClient
from modules.detector_led     import DetectorLED
from modules.estado_ac        import EstadoAC, ModoAC, VelocidadFan

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resultado de comando
# ---------------------------------------------------------------------------
def _resultado(
    boton: str,
    exito: bool,
    intentos: int = 1,
    led_final: Optional[bool] = None,
    mensaje: str = "",
    error: str = "",
) -> dict:
    """
    Construye el dict de resultado estandarizado que devuelven todos
    los metodos publicos del controlador.
    """
    return {
        "boton":     boton,
        "exito":     exito,
        "intentos":  intentos,
        "led_final": led_final,
        "mensaje":   mensaje,
        "error":     error,
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Controlador
# ---------------------------------------------------------------------------
class ControladorAC:
    """
    Orquesta el control del AC con lectura visual de estado.

    Uso basico:
        controlador = ControladorAC()
        controlador.encender()
        controlador.set_temperatura(22)
        controlador.set_temperatura(23, escalonado=True)   # paso a paso
        print(controlador.estado)
    """

    def __init__(
        self,
        broadlink: Optional[BroadlinkClient] = None,
        detector: Optional[DetectorLED] = None,
        estado: Optional[EstadoAC] = None,
        codigos_path: Path = CODIGOS_AC,
    ):
        # Inyeccion de dependencias: permite pasar mocks en tests
        self.broadlink = broadlink or BroadlinkClient()
        self.detector  = detector  or DetectorLED()
        self.estado    = estado    or EstadoAC.cargar()
        self._codigos  = self._cargar_codigos(codigos_path)

        logger.info("[ControladorAC] Iniciado | Estado actual: %s", self.estado)

    # -----------------------------------------------------------------------
    # API publica — comandos de alto nivel
    # -----------------------------------------------------------------------
    def encender(self) -> dict:
        """
        Enciende el AC.

        Flujo:
          1. Lee la camara para saber el estado real actual del AC.
          2. Si ya esta encendido (confirmado por camara), no emite nada.
          3. Emite POWER_ON via IR.

        Returns:
            dict de resultado estandarizado.
        """
        led_actual = self._leer_camara_estado()

        if led_actual is True:
            logger.info("[ControladorAC] AC ya encendido (confirmado por camara). No se emite IR.")
            self.estado.marcar_validado(True)
            self.estado.guardar()
            return _resultado("POWER_ON", exito=True, led_final=True,
                              mensaje="AC ya estaba encendido (camara)")

        res = self._emitir_ir("POWER_ON")
        if res["exito"]:
            self.estado.encendido = True
            self.estado.registrar_comando("POWER_ON")
            self.estado.guardar()

        return res

    def apagar(self) -> dict:
        """
        Apaga el AC.

        Flujo:
          1. Lee la camara para saber el estado real actual del AC.
          2. Si ya esta apagado (confirmado por camara), no emite nada.
          3. Emite POWER_OFF via IR.
        """
        led_actual = self._leer_camara_estado()

        if led_actual is False:
            logger.info("[ControladorAC] AC ya apagado (confirmado por camara). No se emite IR.")
            self.estado.marcar_validado(False)
            self.estado.guardar()
            return _resultado("POWER_OFF", exito=True, led_final=False,
                              mensaje="AC ya estaba apagado (camara)")

        res = self._emitir_ir("POWER_OFF")
        if res["exito"]:
            self.estado.encendido = False
            self.estado.registrar_comando("POWER_OFF")
            self.estado.guardar()

        return res

    def set_temperatura(self, grados: int, escalonado: bool = False) -> dict:
        """
        Establece la temperatura del AC.

        Soporta dos modos de envio:

        Modo DIRECTO (escalonado=False, por defecto):
            Envia el codigo TEMP_<grados> directamente. El AC aplica el valor
            objetivo de una sola vez. Mas rapido, un solo codigo IR.

        Modo ESCALONADO (escalonado=True):
            Envia las transiciones una a una siguiendo la secuencia de codigos
            capturados (TEMP_20_21, TEMP_21_22...). Recomendado cuando el AC
            no responde fiablemente a codigos directos.
            Requiere conocer la temperatura actual (estado.temperatura != None).

        Args:
            grados:     temperatura objetivo (TEMP_MIN - TEMP_MAX)
            escalonado: True = modo escalonado, False = modo directo

        Returns:
            dict de resultado estandarizado.
        """
        if not self.estado.encendido:
            return _resultado("TEMP", exito=False,
                              error="El AC esta apagado, enciendelo primero")

        if not (TEMP_MIN <= grados <= TEMP_MAX):
            return _resultado("TEMP", exito=False,
                              error=f"Temperatura fuera de rango ({TEMP_MIN}-{TEMP_MAX}C): {grados}")

        if self.estado.temperatura == grados:
            return _resultado(f"TEMP_{grados}", exito=True,
                              mensaje=f"Temperatura ya es {grados}C")

        if escalonado:
            return self._set_temperatura_escalonado(grados)
        else:
            return self._set_temperatura_directo(grados)

    def set_modo(self, modo: str) -> dict:
        """
        Cambia el modo de operacion del AC (FRIO, CALOR, VENTILA, AUTO, SECO).

        Args:
            modo: nombre del modo. Debe coincidir con una clave en codigos_ac.json

        Returns:
            dict de resultado estandarizado.
        """
        if not self.estado.encendido:
            return _resultado(modo, exito=False,
                              error="El AC esta apagado, enciendelo primero")

        modo_upper = modo.upper()
        if modo_upper not in ModoAC.__members__:
            return _resultado(modo, exito=False,
                              error=f"Modo desconocido: '{modo}'. "
                                    f"Validos: {list(ModoAC.__members__.keys())}")

        if self.estado.modo == modo_upper:
            return _resultado(modo_upper, exito=True,
                              mensaje=f"Modo ya es {modo_upper}")

        res = self._emitir_ir(modo_upper)
        if res["exito"]:
            self.estado.modo = modo_upper
            self.estado.registrar_comando(modo_upper)
            self.estado.guardar()
        return res

    def set_velocidad_fan(self, velocidad: str) -> dict:
        """
        Cambia la velocidad del ventilador (AUTO, BAJA, MEDIA, ALTA).
        """
        if not self.estado.encendido:
            return _resultado(velocidad, exito=False,
                              error="El AC esta apagado, enciendelo primero")

        vel_upper = velocidad.upper()
        if vel_upper not in VelocidadFan.__members__:
            return _resultado(velocidad, exito=False,
                              error=f"Velocidad desconocida: '{velocidad}'. "
                                    f"Validas: {list(VelocidadFan.__members__.keys())}")

        res = self._emitir_ir(vel_upper)
        if res["exito"]:
            self.estado.velocidad_fan = vel_upper
            self.estado.registrar_comando(vel_upper)
            self.estado.guardar()
        return res

    def get_estado(self) -> dict:
        """
        Devuelve el estado actual del AC como dict.
        Si el estado no esta validado por camara, lo indica en el resultado.
        """
        estado_dict = self.estado.to_dict()
        estado_dict["advertencia"] = (
            None if self.estado.led_validado
            else "Estado inferido, no confirmado por camara"
        )
        return estado_dict

    def validar_estado_actual(self) -> dict:
        """
        Consulta la camara sin emitir ningun comando IR.
        Util para sincronizar el estado del sistema con la realidad fisica.
        """
        logger.info("[ControladorAC] Validando estado actual con camara...")
        led = self.detector.detectar()

        if led is None:
            return _resultado("VALIDAR", exito=False,
                              error="Camara no disponible")

        self.estado.marcar_validado(led)
        self.estado.guardar()

        return _resultado(
            "VALIDAR", exito=True, led_final=led,
            mensaje=f"AC {'ENCENDIDO' if led else 'APAGADO'} (confirmado por camara)"
        )

    # -----------------------------------------------------------------------
    # Logica interna de temperatura
    # -----------------------------------------------------------------------
    def _set_temperatura_directo(self, grados: int) -> dict:
        """
        Envia el codigo TEMP_<grados> directamente al AC.
        Un solo codigo IR con el estado completo objetivo.
        """
        boton = f"TEMP_{grados}"
        logger.info(
            "[ControladorAC] [DIRECTO] Temperatura: %s -> %dC | emitiendo [%s]",
            self.estado.temperatura or "?", grados, boton
        )

        if not self._tiene_codigo(boton):
            return _resultado(boton, exito=False,
                              error=f"Codigo '{boton}' no encontrado en codigos_ac.json. "
                                    f"Disponibles: {list(self._codigos.keys())}")

        res = self._emitir_ir(boton)
        if res["exito"]:
            self.estado.temperatura = grados
            self.estado.registrar_comando(boton)
            self.estado.guardar()
            res["mensaje"] = f"Temperatura establecida a {grados}C (modo directo)"

        return res

    def _set_temperatura_escalonado(self, grados: int) -> dict:
        """
        Envia la temperatura paso a paso usando los codigos de transicion
        (TEMP_20_21, TEMP_21_22, TEMP_22_23 para subir;
         TEMP_23_22, TEMP_22_21, TEMP_21_20 para bajar).

        Requiere conocer la temperatura actual en self.estado.temperatura.
        Si es None, intenta usar primero el modo directo para inicializar.
        """
        temp_actual = self.estado.temperatura

        if temp_actual is None:
            logger.warning(
                "[ControladorAC] [ESCALONADO] Temperatura actual desconocida. "
                "Inicializando con modo directo primero."
            )
            res_init = self._set_temperatura_directo(grados)
            if res_init["exito"]:
                res_init["mensaje"] = (
                    f"Temperatura inicial establecida a {grados}C "
                    "(modo directo — temp. anterior desconocida)"
                )
            return res_init

        if temp_actual == grados:
            return _resultado(f"TEMP_{grados}", exito=True,
                              mensaje=f"Temperatura ya es {grados}C")

        # Determinar la secuencia de pasos
        paso = 1 if grados > temp_actual else -1
        pasos = list(range(temp_actual, grados, paso))  # ej: [20, 21] para 20->22

        logger.info(
            "[ControladorAC] [ESCALONADO] Temperatura: %dC -> %dC | %d paso(s)",
            temp_actual, grados, len(pasos)
        )

        ultimo_res = None
        temp_iter  = temp_actual

        for origen in pasos:
            destino = origen + paso
            boton   = f"TEMP_{origen}_{destino}"

            logger.info(
                "[ControladorAC] [ESCALONADO] Paso %dC -> %dC | [%s]",
                origen, destino, boton
            )

            res = self._emitir_ir(boton)
            if not res["exito"]:
                # Fallo en un paso intermedio: guardar temperatura parcial y reportar
                self.estado.temperatura = temp_iter
                self.estado.registrar_comando(boton)
                self.estado.guardar()
                res["error"] = (
                    f"Fallo en paso {origen}->{destino}: {res['error']} "
                    f"(temperatura parcial: {temp_iter}C)"
                )
                return res

            temp_iter = destino
            self.estado.temperatura = temp_iter
            self.estado.registrar_comando(boton)
            self.estado.guardar()
            ultimo_res = res

            # Pausa entre pulsaciones para que el AC las procese
            if origen != pasos[-1]:
                time.sleep(DELAY_ENTRE_PULSACIONES)

        if ultimo_res:
            ultimo_res["boton"]   = f"TEMP_{temp_actual}_{grados}"
            ultimo_res["mensaje"] = (
                f"Temperatura establecida a {grados}C "
                f"(modo escalonado, {len(pasos)} paso(s))"
            )
        return ultimo_res or _resultado(f"TEMP_{grados}", exito=True,
                                        mensaje=f"Temperatura ya es {grados}C")

    # -----------------------------------------------------------------------
    # Logica interna de emision y lectura de camara
    # -----------------------------------------------------------------------
    def _leer_camara_estado(self) -> Optional[bool]:
        """
        Lee el estado actual del LED con la camara y actualiza el estado interno.
        Siempre se llama antes de decidir si emitir IR.

        Returns:
            True  -> LED encendido (AC ON)
            False -> LED apagado   (AC OFF)
            None  -> camara no disponible
        """
        led = self.detector.detectar()
        if led is not None:
            self.estado.marcar_validado(led)
            self.estado.guardar()
            logger.info(
                "[ControladorAC] Estado leido por camara: LED=%s",
                "ON" if led else "OFF"
            )
        else:
            logger.warning(
                "[ControladorAC] Camara no disponible - se usara estado interno: "
                "encendido=%s", self.estado.encendido
            )
        return led

    def _emitir_ir(self, boton: str) -> dict:
        """
        Emite un codigo IR via Broadlink con reintentos en caso de error de red.

        Args:
            boton: clave del codigo en codigos_ac.json

        Returns:
            dict de resultado estandarizado.
        """
        if not self._tiene_codigo(boton):
            return _resultado(boton, exito=False,
                              error=f"Boton '{boton}' no encontrado en codigos_ac.json")

        ultimo_error = ""
        for intento in range(1, MAX_REINTENTOS + 1):
            try:
                self.broadlink.emitir(self._codigos[boton])
                logger.info(
                    "[ControladorAC] [%s] emitido (intento %d/%d)",
                    boton, intento, MAX_REINTENTOS
                )
                return _resultado(boton, exito=True, intentos=intento)
            except Exception as e:
                ultimo_error = str(e)
                logger.error(
                    "[ControladorAC] Error al emitir [%s] (intento %d/%d): %s",
                    boton, intento, MAX_REINTENTOS, e
                )
                if intento < MAX_REINTENTOS:
                    time.sleep(DELAY_REINTENTO)

        return _resultado(boton, exito=False, intentos=MAX_REINTENTOS, error=ultimo_error)

    # -----------------------------------------------------------------------
    # Utilidades internas
    # -----------------------------------------------------------------------
    def _tiene_codigo(self, boton: str) -> bool:
        if boton not in self._codigos:
            logger.error(
                "[ControladorAC] Boton '%s' no en codigos_ac.json | "
                "Disponibles: %s", boton, list(self._codigos.keys())
            )
            return False
        return True

    @staticmethod
    def _cargar_codigos(path: Path) -> dict:
        """
        Carga codigos_ac.json. Si no existe o esta corrupto, devuelve dict
        vacio. El error real aparecera cuando se intente emitir un boton.
        """
        try:
            with open(path, encoding="utf-8") as f:
                codigos = json.load(f)
            logger.info(
                "[ControladorAC] Codigos cargados: %s", list(codigos.keys())
            )
            return codigos
        except FileNotFoundError:
            logger.warning(
                "[ControladorAC] %s no encontrado, "
                "captura los codigos con broadlink_client.py learn", path
            )
            return {}
        except json.JSONDecodeError as e:
            logger.error("[ControladorAC] codigos_ac.json corrupto: %s", e)
            return {}


# ---------------------------------------------------------------------------
# Ejecucion independiente — CLI de control directo
# ---------------------------------------------------------------------------
def _configurar_logging_independiente() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )


def _imprimir_resultado(res: dict) -> None:
    print(f"\n{'='*55}")
    print(f"  Boton:    {res['boton']}")
    print(f"  Exito:    {'SI' if res['exito'] else 'NO'}")
    print(f"  Intentos: {res['intentos']}")
    if res['led_final'] is not None:
        print(f"  LED AC:   {'ENCENDIDO' if res['led_final'] else 'APAGADO'}")
    if res['mensaje']:
        print(f"  Mensaje:  {res['mensaje']}")
    if res['error']:
        print(f"  Error:    {res['error']}")
    print(f"  Hora:     {res['timestamp']}")
    print(f"{'='*55}\n")


def main() -> int:
    import sys
    _configurar_logging_independiente()

    AYUDA = f"""
Uso: python controlador_ac.py <comando> [args]

Flujo de cada comando:
  1. Lee camara para conocer estado real del AC  (siempre)
  2. Emite el codigo IR si hace falta

Comandos disponibles:
  encender                         Lee camara -> emite POWER_ON si hace falta
  apagar                           Lee camara -> emite POWER_OFF si hace falta
  temperatura <grados>             Emite TEMP_<grados> directo ({TEMP_MIN}-{TEMP_MAX}C)
  temperatura <grados> escalonado  Envia paso a paso con codigos de transicion
  modo <modo>                      Cambia modo (FRIO | CALOR | VENTILA | AUTO | SECO)
  fan <velocidad>                  Cambia ventilador (AUTO | BAJA | MEDIA | ALTA)
  estado                           Muestra estado actual
  validar                          Consulta camara sin emitir IR
"""

    if len(sys.argv) < 2:
        print(AYUDA)
        return 1

    ctrl    = ControladorAC()
    comando = sys.argv[1].lower()

    if comando == "encender":
        res = ctrl.encender()

    elif comando == "apagar":
        res = ctrl.apagar()

    elif comando == "temperatura":
        if len(sys.argv) < 3:
            print("ERROR: indica la temperatura. Ej: temperatura 22")
            return 1
        try:
            grados = int(sys.argv[2])
        except ValueError:
            print(f"ERROR: '{sys.argv[2]}' no es un numero valido")
            return 1
        escalonado = len(sys.argv) > 3 and sys.argv[3].lower() == "escalonado"
        res = ctrl.set_temperatura(grados, escalonado=escalonado)

    elif comando == "modo":
        if len(sys.argv) < 3:
            print(f"ERROR: indica el modo. Validos: {list(ModoAC.__members__.keys())}")
            return 1
        res = ctrl.set_modo(sys.argv[2])

    elif comando == "fan":
        if len(sys.argv) < 3:
            print(f"ERROR: indica la velocidad. Validas: {list(VelocidadFan.__members__.keys())}")
            return 1
        res = ctrl.set_velocidad_fan(sys.argv[2])

    elif comando == "estado":
        estado = ctrl.get_estado()
        print(f"\n{ctrl.estado}")
        print(f"\nJSON completo:\n{json.dumps(estado, indent=2, ensure_ascii=False)}")
        return 0

    elif comando == "validar":
        res = ctrl.validar_estado_actual()

    else:
        print(f"ERROR: comando desconocido '{comando}'")
        print(AYUDA)
        return 1

    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


if __name__ == "__main__":
    exit(main())
