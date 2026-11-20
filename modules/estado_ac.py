"""
modules/estado_ac.py

Modelo de estado del AC y su persistencia en disco.

Responsabilidad unica: representar y persistir el ultimo estado conocido
del AC. No emite IR ni accede a la camara, eso es responsabilidad de
controlador_ac.py.

'Conocido' significa confirmado visualmente por la camara. Si led_validado
es False, el estado es inferido (se envio el comando IR pero no se confirmo).
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    from config.settings import ESTADO_AC
except ImportError:
    ESTADO_AC = Path("config/estado_ac.json")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerados
# ---------------------------------------------------------------------------
class ModoAC(str, Enum):
    """Modos de operacion del AC."""
    FRIO    = "FRIO"
    CALOR   = "CALOR"
    VENTILA = "VENTILA"
    AUTO    = "AUTO"
    SECO    = "SECO"


class VelocidadFan(str, Enum):
    """Velocidades del ventilador disponibles en la mayoria de AC."""
    AUTO  = "AUTO"
    BAJA  = "BAJA"
    MEDIA = "MEDIA"
    ALTA  = "ALTA"


# ---------------------------------------------------------------------------
# Modelo de estado
# ---------------------------------------------------------------------------
@dataclass
class EstadoAC:
    """
    Representa el estado conocido del AC en un momento dado.

    Campos:
        encendido:      True si el AC esta encendido segun el ultimo comando.
        temperatura:    Temperatura configurada en grados Celsius. None si
                        no se ha establecido desde este sistema.
        modo:           Modo de operacion (FRIO, CALOR, etc.). None si desconocido.
        velocidad_fan:  Velocidad del ventilador. None si desconocida.
        led_validado:   True si la camara confirmo el estado actual. False si
                        solo se infiere por el comando enviado (sin confirmacion visual).
        ultimo_comando: Nombre del ultimo boton IR enviado (ej: "POWER_ON", "TEMP_22").
        ultimo_cambio:  ISO timestamp del ultimo cambio de estado.
    """
    encendido:      bool = False
    temperatura:    Optional[int] = None
    modo:           Optional[str] = None
    velocidad_fan:  Optional[str] = None
    led_validado:   bool = False
    ultimo_comando: Optional[str] = None
    ultimo_cambio:  str = field(default_factory=lambda: datetime.now().isoformat())

    # -----------------------------------------------------------------------
    # Serializacion
    # -----------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Convierte el estado a dict serializable a JSON."""
        return asdict(self)

    def to_json(self) -> str:
        """Convierte el estado a JSON string formateado."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    # -----------------------------------------------------------------------
    # Persistencia
    # -----------------------------------------------------------------------
    def guardar(self, path: Path = ESTADO_AC) -> None:
        """
        Persiste el estado en disco de forma atomica (write + rename)
        para evitar ficheros corruptos si el proceso muere durante la escritura.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            tmp.replace(path)
            logger.debug(
                "[EstadoAC] Estado guardado: encendido=%s validado=%s temp=%s",
                self.encendido, self.led_validado, self.temperatura
            )
        except Exception as e:
            logger.error("[EstadoAC] Error al guardar estado: %s", e)
            if tmp.exists():
                tmp.unlink()
            raise

    @classmethod
    def cargar(cls, path: Path = ESTADO_AC) -> "EstadoAC":
        """
        Carga el ultimo estado conocido desde disco.
        Si el fichero no existe o esta corrupto, devuelve estado inicial
        (apagado, sin validar) para no bloquear el arranque del sistema.
        """
        path = Path(path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            # Filtrar campos desconocidos para tolerar versiones antiguas del JSON
            campos_validos = {f.name for f in cls.__dataclass_fields__.values()}
            data_limpio    = {k: v for k, v in data.items() if k in campos_validos}

            estado = cls(**data_limpio)
            logger.info(
                "[EstadoAC] Estado cargado: encendido=%s validado=%s temp=%s modo=%s",
                estado.encendido, estado.led_validado,
                estado.temperatura, estado.modo
            )
            return estado

        except FileNotFoundError:
            logger.info("[EstadoAC] %s no encontrado. Usando estado inicial", path)
            return cls()

        except (json.JSONDecodeError, TypeError) as e:
            logger.error(
                "[EstadoAC] Estado corrupto en %s (%s). Usando estado inicial", path, e
            )
            return cls()

    # -----------------------------------------------------------------------
    # Mutaciones de estado
    # -----------------------------------------------------------------------
    def registrar_comando(self, boton: str) -> None:
        """
        Actualiza el estado tras enviar un comando IR.

        Infiere el cambio de estado segun el boton enviado.
        La validacion visual se hace unicamente al leer la camara
        (marcar_validado), no en este metodo.

        Args:
            boton: Nombre del boton enviado (ej: "POWER_ON", "TEMP_22",
                   "TEMP_21_22").
        """
        self.ultimo_comando = boton
        self.ultimo_cambio  = datetime.now().isoformat()
        self.led_validado   = False  # pendiente de confirmacion visual

        if boton == "POWER_ON":
            self.encendido = True

        elif boton == "POWER_OFF":
            self.encendido = False

        elif boton.startswith("TEMP_"):
            partes = boton.split("_")   # ["TEMP", "22"] o ["TEMP", "21", "22"]
            if len(partes) == 2:
                # Codigo directo: TEMP_22
                try:
                    self.temperatura = int(partes[1])
                except ValueError:
                    pass
            elif len(partes) == 3:
                # Codigo de transicion: TEMP_21_22 -> destino es partes[2]
                try:
                    self.temperatura = int(partes[2])
                except ValueError:
                    pass

        elif boton in ModoAC.__members__:
            self.modo = boton

        elif boton in VelocidadFan.__members__:
            self.velocidad_fan = boton

        logger.debug(
            "[EstadoAC] Comando registrado: %s | encendido=%s temp=%s",
            boton, self.encendido, self.temperatura
        )

    def marcar_validado(self, encendido: bool) -> None:
        """
        Actualiza el estado con la confirmacion visual de la camara.
        Llamar tras una deteccion exitosa del LED.

        Args:
            encendido: resultado de DetectorLED.detectar()
        """
        self.encendido    = encendido
        self.led_validado = True
        self.ultimo_cambio = datetime.now().isoformat()

        logger.info(
            "[EstadoAC] Estado validado por camara: LED %s",
            "ON" if encendido else "OFF"
        )

    def reset(self) -> None:
        """
        Resetea el estado a valores iniciales.
        Util si se pierde la referencia del estado real del AC.
        """
        self.encendido      = False
        self.temperatura    = None
        self.modo           = None
        self.velocidad_fan  = None
        self.led_validado   = False
        self.ultimo_comando = None
        self.ultimo_cambio  = datetime.now().isoformat()
        logger.info("[EstadoAC] Estado reseteado a valores iniciales")

    # -----------------------------------------------------------------------
    # Representacion
    # -----------------------------------------------------------------------
    def __str__(self) -> str:
        validado = "OK camara" if self.led_validado else "? inferido"
        return (
            f"AC {'ENCENDIDO' if self.encendido else 'APAGADO'} [{validado}] | "
            f"Temp: {self.temperatura or '?'}C | "
            f"Modo: {self.modo or '?'} | "
            f"Fan: {self.velocidad_fan or '?'} | "
            f"Ultimo: {self.ultimo_comando or 'ninguno'}"
        )


# ---------------------------------------------------------------------------
# Ejecucion independiente
# ---------------------------------------------------------------------------
def _configurar_logging_independiente() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-8s] %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )


def main() -> int:
    import sys
    _configurar_logging_independiente()

    print(f"\n{'='*55}")
    print(f"[EstadoAC] Gestion de estado del AC")
    print(f"{'='*55}")

    if len(sys.argv) == 1:
        estado = EstadoAC.cargar()
        print(f"\n{estado}")
        print(f"\nJSON completo:")
        print(estado.to_json())
        return 0

    if sys.argv[1] == "reset":
        estado = EstadoAC.cargar()
        estado.reset()
        estado.guardar()
        print(f"\n[EstadoAC] Estado reseteado y guardado")
        print(f"{estado}")
        return 0

    return 1


if __name__ == "__main__":
    exit(main())
