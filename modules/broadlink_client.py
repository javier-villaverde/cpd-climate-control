"""
modules/broadlink_client.py

Wrapper sobre python-broadlink para emision y aprendizaje de codigos IR.
Encapsula toda la lógica de conexión, autenticación y reconexión automática.

Responsabilidad única: comunicarse con el dispositivo Broadlink.
No conoce ni el estado del AC ni la lógica de reintentos, eso es
responsabilidad de controlador_ac.py.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import broadlink

try:
    from config.settings import (
        BROADLINK_CONFIG,
        BROADLINK_LEARN_TIMEOUT,
        BROADLINK_TIMEOUT,
    )
except ImportError:
    BROADLINK_CONFIG        = Path("config/broadlink_config.json")
    BROADLINK_TIMEOUT       = 5
    BROADLINK_LEARN_TIMEOUT = 15

logger = logging.getLogger(__name__)


class BroadlinkClient:
    """
    Cliente del dispositivo Broadlink RM.

    Gestiona:
        - Carga de configuración (IP/MAC) desde broadlink_config.json
        - Conexión lazy: solo conecta cuando hace falta, no en el constructor
        - Reconexión automatica si la sesion caduca (el dispositivo cierra sesion tras un periodo de inactividad)
        - Emisión de códigos IR (send_data)
        - Aprendizaje de códigos IR (enter_learning + check_data)
    """

    def __init__(self, config_path: Path = BROADLINK_CONFIG):
        self._dev         = None   # conexión activa, None = sin conectar
        self._config_path = config_path
        self._cfg         = self._cargar_config()

    # API publica
    def emitir(self, codigo_hex: str) -> bool:
        """
        Emite un codigo IR al AC.

        Intenta la emision con la sesion actual. Si falla por sesion caducada,
        reconecta una sola vez y reintenta. Si vuelve a fallar, propaga la
        excepcion para que controlador_ac.py decida que hacer.

        Args:
            codigo_hex: codigo IR en formato hex string (de codigos_ac.json)

        Returns:
            True si la emision fue exitosa.

        Raises:
            ConnectionError: si no se puede conectar al dispositivo.
            ValueError: si codigo_hex no es un hex string valido.
            RuntimeError: si la emision falla tras reconexion.
        """
        if not codigo_hex or not isinstance(codigo_hex, str):
            raise ValueError(
                f"[BroadlinkClient] codigo_hex invalido: {repr(codigo_hex)}"
            )

        # Validar que es hex valido antes de intentar la emision
        try:
            payload = bytes.fromhex(codigo_hex)
        except ValueError:
            raise ValueError(
                f"[BroadlinkClient] codigo_hex no es hex valido: {codigo_hex[:20]}..."
            )

        self._asegurar_conexion()

        try:
            self._dev.send_data(payload)
            logger.debug("[BroadlinkClient] IR emitido: %s...", codigo_hex[:20])
            return True

        except Exception as e:
            # La sesion puede caducar si el dispositivo lleva tiempo sin usarse.
            # Intentamos reconectar una sola vez antes de rendirse.
            logger.warning(
                "[BroadlinkClient] Fallo al emitir (%s), reconectando...", e
            )
            self._dev = None
            self._asegurar_conexion()

            try:
                self._dev.send_data(payload)
                logger.debug(
                    "[BroadlinkClient] IR emitido tras reconexion: %s...",
                    codigo_hex[:20]
                )
                return True
            except Exception as e2:
                raise RuntimeError(
                    f"[BroadlinkClient] Fallo al emitir tras reconexion: {e2}"
                ) from e2

    def aprender_codigo(self, timeout: int = BROADLINK_LEARN_TIMEOUT) -> Optional[str]:
        """
        Activa el modo aprendizaje IR y espera una pulsacion del mando.

        El dispositivo escucha la senal IR del mando fisico y devuelve
        el codigo capturado. El usuario tiene 'timeout' segundos para pulsar.

        Args:
            timeout: segundos maximos de espera (por defecto BROADLINK_LEARN_TIMEOUT)

        Returns:
            Codigo IR en hex string si se capturo correctamente.
            None si expiro el timeout sin recibir ninguna senal.

        Raises:
            ConnectionError: si no se puede conectar al dispositivo.
        """
        self._asegurar_conexion()

        try:
            self._dev.enter_learning()
            logger.info(
                "[BroadlinkClient] Modo aprendizaje activo, esperando %ds...", timeout
            )
        except Exception as e:
            raise RuntimeError(
                f"[BroadlinkClient] No se pudo activar modo aprendizaje: {e}"
            ) from e

        # Poll cada 500ms hasta timeout
        intentos = timeout * 2
        for i in range(intentos):
            time.sleep(0.5)
            try:
                packet = self._dev.check_data()
                if packet:
                    codigo = packet.hex()
                    logger.info(
                        "[BroadlinkClient] Codigo capturado (%d bytes): %s...",
                        len(packet), codigo[:20]
                    )
                    return codigo
            except Exception:
                # check_data lanza excepcion si aun no hay datos
                pass

            # Log de progreso cada 5 segundos para feedback visual
            if (i + 1) % 10 == 0:
                segundos_restantes = timeout - ((i + 1) // 2)
                logger.info(
                    "[BroadlinkClient] Esperando pulsacion... %ds restantes",
                    segundos_restantes
                )

        logger.warning(
            "[BroadlinkClient] Timeout (%ds) sin recibir codigo IR", timeout
        )
        return None

    def verificar_conexion(self) -> bool:
        """
        Verifica que el dispositivo sea accesible y se pueda autenticar.
        Se usa para comprobar el estado de la conexion.

        Returns:
            True si la conexion funciona, False si hay algun problema.
        """
        try:
            self._dev = None  # forzar reconexion limpia
            self._asegurar_conexion()
            logger.info(
                "[BroadlinkClient] Conexion verificada: %s", self._cfg.get("ip")
            )
            return True
        except Exception as e:
            logger.error("[BroadlinkClient] Verificacion fallida: %s", e)
            return False

    @property
    def ip(self) -> str:
        """IP del dispositivo segun broadlink_config.json."""
        return self._cfg.get("ip", "desconocida")

    @property
    def modelo(self) -> str:
        """Nombre de clase del dispositivo Broadlink."""
        if self._dev is not None:
            return type(self._dev).__name__
        return "no conectado"

    # Conexion interna
    def _asegurar_conexion(self) -> None:
        """
        Conecta y autentica si no hay sesion activa.
        No hace nada si ya hay sesion válida.

        Raises:
            ConnectionError: si no se puede establecer la conexion.
        """
        if self._dev is not None:
            return

        ip = self._cfg.get("ip")
        if not ip:
            raise ConnectionError(
                "[BroadlinkClient] No hay IP en broadlink_config.json. "
                "Ejecuta primero el script de configuracion."
            )

        try:
            logger.debug("[BroadlinkClient] Conectando a %s...", ip)
            self._dev = broadlink.hello(ip, timeout=BROADLINK_TIMEOUT)
            self._dev.auth()
            logger.info(
                "[BroadlinkClient] Conectado: %s en %s",
                type(self._dev).__name__, ip
            )
        except Exception as e:
            logger.warning(
                "[BroadlinkClient] IP guardada %s no responde (%s), lanzando discovery...", ip, e
            )
            self._dev = None
            try:
                self._descubrir_y_guardar()
            except Exception as e2:
                raise ConnectionError(
                    f"[BroadlinkClient] No se pudo conectar a {ip} ni via discovery: {e2}"
                ) from e2

    def _descubrir_y_guardar(self) -> None:
        """
        Hace broadcast en la red buscando dispositivos Broadlink.
        Conecta al primero que encuentre y guarda su IP en broadlink_config.json
        para que la proxima conexion sea directa sin discovery.
        """
        logger.info("[BroadlinkClient] Discovery en red local (timeout=5s)...")
        dispositivos = broadlink.discover(timeout=5)

        if not dispositivos:
            raise RuntimeError(
                "No se encontro ningun dispositivo Broadlink en la red. "
                "Verifica que esta encendido y en la misma subred."
            )

        device = dispositivos[0]
        device.auth()

        cfg_nueva = {
            "ip":      device.host[0],
            "port":    device.host[1],
            "devtype": device.devtype,
            "mac":     ":".join(format(x, "02x") for x in device.mac),
        }

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(cfg_nueva, f, indent=2)

        self._cfg = cfg_nueva
        self._dev = device

        logger.info(
            "[BroadlinkClient] Dispositivo encontrado: IP=%s MAC=%s — guardado en %s",
            cfg_nueva["ip"], cfg_nueva["mac"], self._config_path
        )

    def _cargar_config(self) -> dict:
        """
        Carga broadlink_config.json.
        Si no existe, devuelve dict vacio. El error real se producira
        cuando se intente conectar, con un mensaje mas claro.
        """
        try:
            with open(self._config_path) as f:
                cfg = json.load(f)
            logger.debug(
                "[BroadlinkClient] Config cargada: IP=%s MAC=%s",
                cfg.get("ip"), cfg.get("mac")
            )
            return cfg
        except FileNotFoundError:
            logger.warning(
                "[BroadlinkClient] %s no encontrado. "
                "Ejecuta el script de configuracion primero.",
                self._config_path
            )
            return {}
        except json.JSONDecodeError as e:
            logger.error(
                "[BroadlinkClient] Error al parsear %s: %s",
                self._config_path, e
            )
            return {}


# Ejecucion independiente: test rapido de conexion y aprendizaje
def _configurar_logging_independiente() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )


def main() -> int:
    import sys
    _configurar_logging_independiente()

    client = BroadlinkClient()

    # Modo test de conexion: python broadlink_client.py
    if len(sys.argv) == 1:
        print(f"\n{'='*50}")
        print(f"[BroadlinkClient] Test de conexion")
        print(f"{'='*50}")

        ok = client.verificar_conexion()
        if ok:
            print(f"[BroadlinkClient] Conectado: {client.modelo} en {client.ip}")
        else:
            print(f"[BroadlinkClient] ERROR: no se pudo conectar a {client.ip}")
        return 0 if ok else 1

    # Modo aprendizaje: python broadlink_client.py learn NOMBRE_BOTON
    if len(sys.argv) == 3 and sys.argv[1] == "learn":
        boton = sys.argv[2]
        codigos_path = Path("config/codigos_ac.json")

        print(f"\n{'='*50}")
        print(f"[BroadlinkClient] Aprendizaje IR: [{boton}]")
        print(f"[BroadlinkClient] Apunta el mando al Broadlink y pulsa el boton")
        print(f"{'='*50}\n")

        codigo = client.aprender_codigo()

        if codigo is None:
            print(f"[BroadlinkClient] ERROR: timeout sin recibir codigo")
            return 1

        # Cargar codigos existentes y añadir/sobreescribir el nuevo
        codigos = {}
        if codigos_path.exists():
            with open(codigos_path) as f:
                codigos = json.load(f)

        codigos[boton] = codigo

        with open(codigos_path, "w") as f:
            json.dump(codigos, f, indent=2)

        print(f"\n[BroadlinkClient] Codigo [{boton}] guardado en {codigos_path}")
        print(f"[BroadlinkClient] Total botones: {list(codigos.keys())}")
        return 0

    # Modo emision: python broadlink_client.py send NOMBRE_BOTON
    if len(sys.argv) == 3 and sys.argv[1] == "send":
        boton = sys.argv[2]
        codigos_path = Path("config/codigos_ac.json")

        if not codigos_path.exists():
            print(f"[BroadlinkClient] ERROR: {codigos_path} no encontrado")
            return 1

        with open(codigos_path) as f:
            codigos = json.load(f)

        if boton not in codigos:
            print(f"[BroadlinkClient] ERROR: boton '{boton}' no esta en {codigos_path}")
            print(f"[BroadlinkClient] Botones disponibles: {list(codigos.keys())}")
            return 1

        print(f"\n{'='*50}")
        print(f"[BroadlinkClient] Emitiendo: [{boton}]")
        print(f"{'='*50}\n")

        try:
            client.emitir(codigos[boton])
            print(f"[BroadlinkClient] [{boton}] emitido correctamente")
            return 0
        except Exception as e:
            print(f"[BroadlinkClient] ERROR al emitir [{boton}]: {e}")
            return 1

    # Modo discovery manual: python broadlink_client.py discover
    if len(sys.argv) == 2 and sys.argv[1] == "discover":
        print(f"\n{'='*50}")
        print("[BroadlinkClient] Buscando dispositivos Broadlink en la red...")
        print(f"{'='*50}\n")
        try:
            client._descubrir_y_guardar()
            print(f"[BroadlinkClient] Encontrado: {client.modelo} en {client.ip}")
            print(f"[BroadlinkClient] IP guardada en {client._config_path}")
            return 0
        except Exception as e:
            print(f"[BroadlinkClient] ERROR: {e}")
            return 1

    return 1


if __name__ == "__main__":
    exit(main())
