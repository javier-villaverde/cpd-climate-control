"""
main_cli.py

Interfaz de linea de comandos principal del sistema TFG Control AC.

Punto de entrada unico para operar el sistema completo sin la API REST.
Util para demostraciones, pruebas de integracion y uso directo
desde el servidor/cluster sin levantar uvicorn.

Uso rapido:
    python main_cli.py encender
    python main_cli.py apagar
    python main_cli.py temperatura 22
    python main_cli.py estado
    python main_cli.py demo
"""

import json
import logging
import sys
import time
from pathlib import Path

try:
    from config.settings import (
        LOG_BACKUP_COUNT,
        LOG_DATE_FMT,
        LOG_FILE,
        LOG_FORMAT,
        LOG_LEVEL_CONSOLE,
        LOG_LEVEL_FILE,
        LOG_MAX_BYTES,
        TEMP_MAX,
        TEMP_MIN,
    )
except ImportError:
    LOG_FILE          = Path("logs/ac_control.log")
    LOG_FORMAT        = "%(asctime)s [%(levelname)-8s] %(name)s - %(message)s"
    LOG_DATE_FMT      = "%Y-%m-%d %H:%M:%S"
    LOG_LEVEL_FILE    = "DEBUG"
    LOG_LEVEL_CONSOLE = "INFO"
    LOG_MAX_BYTES     = 5 * 1024 * 1024
    LOG_BACKUP_COUNT  = 3
    TEMP_MIN          = 16
    TEMP_MAX          = 30

from modules.controlador_ac import ControladorAC
from modules.estado_ac      import ModoAC, VelocidadFan
from modules.detector_led import DetectorLED


# Logging del sistema completo
def configurar_logging() -> None:
    """
    Configura logging con dos handlers:
      - Consola: nivel INFO - operaciones principales, limpio para el usuario
      - Fichero: nivel DEBUG - todo el detalle para diagnostico posterior

    Usa RotatingFileHandler para evitar que el log crezca indefinidamente.
    """
    import logging.handlers

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger_raiz = logging.getLogger()
    logger_raiz.setLevel(logging.DEBUG)  # capturar todo, filtrar por handler

    # Handler consola
    handler_consola = logging.StreamHandler(sys.stdout)
    handler_consola.setLevel(getattr(logging, LOG_LEVEL_CONSOLE, logging.INFO))
    handler_consola.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FMT))

    # Handler fichero con rotacion
    handler_fichero = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8"
    )
    handler_fichero.setLevel(getattr(logging, LOG_LEVEL_FILE, logging.DEBUG))
    handler_fichero.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FMT))

    logger_raiz.addHandler(handler_consola)
    logger_raiz.addHandler(handler_fichero)


logger = logging.getLogger(__name__)


# Presentacion de resultados

_LINEA = "=" * 55

def _imprimir_resultado(res: dict) -> None:
    """Imprime el resultado de un comando de forma legible."""
    exito  = res.get("exito", False)
    symbol = "OK" if exito else "NOT OK"

    print(f"\n{_LINEA}")
    print(f"  {symbol}  Comando:   {res.get('boton', '?')}")
    print(f"     Exito:     {'SI' if exito else 'NO'}")
    print(f"     Intentos:  {res.get('intentos', 1)}")
    print(f"     Validado:  {'SI (camara)' if res.get('validado') else 'NO (inferido)'}")

    if res.get("led_final") is not None:
        led = "ENCENDIDO" if res["led_final"] else "APAGADO"
        print(f"     LED AC:    {led}")

    if res.get("mensaje"):
        print(f"     Mensaje:   {res['mensaje']}")

    if res.get("error"):
        print(f"     Error:     {res['error']}")

    print(f"     Hora:      {res.get('timestamp', '?')}")
    print(f"{_LINEA}\n")


def _imprimir_estado(ctrl: ControladorAC) -> None:
    """Imprime el estado actual del AC de forma legible."""
    e = ctrl.estado
    validado = "confirmado por camara" if e.led_validado else "? estado inferido"

    print(f"\n{_LINEA}")
    print(f"  ESTADO ACTUAL DEL AC")
    print(f"{_LINEA}")
    print(f"  Encendido:    {'SI' if e.encendido else 'NO'}  [{validado}]")
    print(f"  Temperatura:  {e.temperatura if e.temperatura is not None else 'desconocida'}°C")
    print(f"  Modo:         {e.modo         or 'desconocido'}")
    print(f"  Ventilador:   {e.velocidad_fan or 'desconocido'}")
    print(f"  Ultimo cmd:   {e.ultimo_comando or 'ninguno'}")
    print(f"  Ultimo cambio:{e.ultimo_cambio}")
    print(f"  Fallos cons.: {e.intentos_fallidos}")
    print(f"{_LINEA}\n")


# Comandos CLI

def cmd_encender(ctrl: ControladorAC) -> int:
    print("\n[CLI] Encendiendo AC...")
    res = ctrl.encender()
    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


def cmd_apagar(ctrl: ControladorAC) -> int:
    print("\n[CLI] Apagando AC...")
    res = ctrl.apagar()
    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


def cmd_temperatura(ctrl: ControladorAC, args: list) -> int:
    if not args:
        print("ERROR: indica la temperatura. Ej: temperatura 22")
        return 1
    try:
        grados = int(args[0])
    except ValueError:
        print(f"ERROR: '{args[0]}' no es un numero valido")
        return 1

    if not (TEMP_MIN <= grados <= TEMP_MAX):
        print(f"ERROR: temperatura fuera de rango ({TEMP_MIN}-{TEMP_MAX}°C)")
        return 1

    print(f"\n[CLI] Estableciendo temperatura a {grados}°C...")
    res = ctrl.set_temperatura(grados)
    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


def cmd_modo(ctrl: ControladorAC, args: list) -> int:
    if not args:
        modos = [m.value for m in ModoAC]
        print(f"ERROR: indica el modo. Validos: {modos}")
        return 1

    print(f"\n[CLI] Cambiando modo a {args[0].upper()}...")
    res = ctrl.set_modo(args[0])
    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


def cmd_fan(ctrl: ControladorAC, args: list) -> int:
    if not args:
        velocidades = [v.value for v in VelocidadFan]
        print(f"ERROR: indica la velocidad. Validas: {velocidades}")
        return 1

    print(f"\n[CLI] Cambiando velocidad ventilador a {args[0].upper()}...")
    res = ctrl.set_velocidad_fan(args[0])
    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


def cmd_estado(ctrl: ControladorAC) -> int:
    _imprimir_estado(ctrl)
    return 0


def cmd_validar(ctrl: ControladorAC) -> int:
    print("\n[CLI] Consultando camara sin emitir IR...")
    res = ctrl.validar_estado_actual()
    _imprimir_resultado(res)
    return 0 if res["exito"] else 1


def cmd_reset_estado(ctrl: ControladorAC) -> int:
    """
    Resetea el estado persistido a valores iniciales.
    Util si se pierde la referencia real (corte de luz, reinicio del AC).
    """
    confirmar = input("\n¿Resetear estado a valores iniciales? [s/N]: ").strip().lower()
    if confirmar != "s":
        print("[CLI] Reset cancelado")
        return 0

    ctrl.estado.reset()
    ctrl.estado.guardar()
    print("[CLI] Estado reseteado correctamente")
    _imprimir_estado(ctrl)
    return 0


def cmd_aprender(ctrl: ControladorAC, args: list) -> int:
    """
    Captura un nuevo codigo IR del mando fisico y lo guarda en codigos_ac.json.
    Permite añadir botones nuevos sin salir del CLI principal.
    """
    if not args:
        print("ERROR: indica el nombre del boton. Ej: aprender POWER")
        return 1

    boton        = args[0].upper()
    codigos_path = Path("config/codigos_ac.json")

    print(f"\n{_LINEA}")
    print(f"  Aprendizaje IR: [{boton}]")
    print(f"  Apunta el mando al Broadlink y pulsa el boton")
    print(f"{_LINEA}\n")

    codigo = ctrl.broadlink.aprender_codigo()

    if codigo is None:
        print(f"[CLI] ERROR: timeout sin recibir codigo para [{boton}]")
        return 1

    # Cargar codigos existentes y añadir el nuevo
    codigos = {}
    if codigos_path.exists():
        with open(codigos_path, encoding="utf-8") as f:
            codigos = json.load(f)

    codigos[boton] = codigo

    with open(codigos_path, "w", encoding="utf-8") as f:
        json.dump(codigos, f, indent=2, ensure_ascii=False)

    # Recargar los codigos en el controlador para que el nuevo este disponible
    ctrl._codigos = codigos

    print(f"\n[CLI] Codigo [{boton}] guardado en {codigos_path}")
    print(f"[CLI] Botones disponibles: {list(codigos.keys())}")
    return 0


def cmd_demo(ctrl: ControladorAC) -> int:
    """
    Secuencia de demostracion completa del sistema.

    Ejecuta: estado → validar → encender → esperar → apagar
    Muestra el bucle cerrado IR + validacion visual en accion.
    """
    print(f"\n{'#'*55}")
    print(f"  DEMO - Sistema de Control AC con Validacion Visual")
    print(f"{'#'*55}\n")

    pasos = [
        ("1/5 - Estado inicial",          lambda: _demo_paso(ctrl, "estado")),
        ("2/5 - Sincronizar con camara",  lambda: _demo_paso(ctrl, "validar")),
        ("3/5 - Encender AC",             lambda: _demo_paso(ctrl, "encender")),
        ("4/5 - Verificar estado final",  lambda: _demo_paso(ctrl, "estado")),
        ("5/5 - Apagar AC",               lambda: _demo_paso(ctrl, "apagar")),
    ]

    for titulo, paso in pasos:
        print(f"\n  >>> {titulo}")
        time.sleep(1)
        paso()
        time.sleep(2)

    print(f"\n{'#'*55}")
    print(f"  DEMO COMPLETADA")
    print(f"  Log completo en: logs/ac_control.log")
    print(f"{'#'*55}\n")
    return 0


def _demo_paso(ctrl: ControladorAC, comando: str) -> None:
    """Ejecuta un paso de la demo e imprime el resultado."""
    if comando == "estado":
        _imprimir_estado(ctrl)
    elif comando == "validar":
        res = ctrl.validar_estado_actual()
        _imprimir_resultado(res)
    elif comando == "encender":
        res = ctrl.encender()
        _imprimir_resultado(res)
    elif comando == "apagar":
        res = ctrl.apagar()
        _imprimir_resultado(res)


# Ayuda

AYUDA = f"""
{_LINEA}
  TFG - Control AC con Validacion Visual
  Uso: python main_cli.py <comando> [args]
{_LINEA}

  COMANDOS DE CONTROL:
    encender                Enciende el AC con validacion visual
    apagar                  Apaga el AC con validacion visual
    temperatura <grados>    Establece temperatura ({TEMP_MIN}-{TEMP_MAX}°C)
    modo <modo>             Cambia modo de operacion
                            Modos: {[m.value for m in ModoAC]}
    fan <velocidad>         Cambia velocidad del ventilador
                            Velocidades: {[v.value for v in VelocidadFan]}

  INFORMACION Y HERRAMIENTAS:
    estado                  Muestra estado actual del AC
    validar                 Consulta camara sin emitir IR
    aprender <BOTON>        Captura un nuevo codigo IR del mando
    reset                   Resetea el estado a valores iniciales

  DEMOSTRACION:
    demo                    Secuencia completa para demostracion

  EJEMPLOS:
    python main_cli.py encender
    python main_cli.py temperatura 22
    python main_cli.py modo FRIO
    python main_cli.py aprender TEMP_UP
    python main_cli.py demo

  Log del sistema: logs/ac_control.log
{_LINEA}
"""


# Main

def main() -> int:
    configurar_logging()

    if len(sys.argv) < 2:
        print(AYUDA)
        return 0

    # Extraer --imagen si se pasa como argumento opcional
    imagen_estatica = None
    args_filtrados  = []
    i = 0
    while i < len(sys.argv[1:]):
        arg = sys.argv[1:][i]
        if arg == "--imagen" and i + 1 < len(sys.argv[1:]):
            imagen_estatica = sys.argv[1:][i + 1]
            i += 2
        else:
            args_filtrados.append(arg)
            i += 1

    comando = args_filtrados[0].lower() if args_filtrados else ""
    args = args_filtrados[1:]

    logger.info("[CLI] Comando: %s %s", comando, " ".join(args))
    if imagen_estatica:
        logger.info("[CLI] Modo imagen estatica: %s", imagen_estatica)

    # Inicializar controlador con DetectorLED configurado
    try:
        detector = DetectorLED(imagen_estatica=imagen_estatica)
        ctrl = ControladorAC(detector=detector)
    except Exception as e:
        logger.error("[CLI] Error al inicializar el sistema: %s", e)
        print(f"\nERROR: No se pudo inicializar el sistema: {e}")
        print("Verifica que broadlink_config.json y codigos_ac.json existen.")
        return 1

    # Despachar comando
    comandos = {
        "encender":    lambda: cmd_encender(ctrl),
        "apagar":      lambda: cmd_apagar(ctrl),
        "temperatura": lambda: cmd_temperatura(ctrl, args),
        "modo":        lambda: cmd_modo(ctrl, args),
        "fan":         lambda: cmd_fan(ctrl, args),
        "estado":      lambda: cmd_estado(ctrl),
        "validar":     lambda: cmd_validar(ctrl),
        "aprender":    lambda: cmd_aprender(ctrl, args),
        "reset":       lambda: cmd_reset_estado(ctrl),
        "demo":        lambda: cmd_demo(ctrl),
        "--help":      lambda: (print(AYUDA), 0)[1],
        "-h":          lambda: (print(AYUDA), 0)[1],
    }

    if comando not in comandos:
        print(f"\nERROR: comando desconocido '{comando}'")
        print(AYUDA)
        return 1

    try:
        return comandos[comando]()
    except KeyboardInterrupt:
        print("\n[CLI] Interrumpido por el usuario")
        return 0
    except Exception as e:
        logger.exception("[CLI] Error inesperado ejecutando '%s': %s", comando, e)
        print(f"\nERROR inesperado: {e}")
        print("Consulta logs/ac_control.log para mas detalle")
        return 1


if __name__ == "__main__":
    exit(main())
