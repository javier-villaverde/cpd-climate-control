"""
modules/detector_led.py

Modulo de deteccion del estado del LED indicador del AC mediante vision por
computador.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import time

import cv2
import numpy as np

try:
    from config.settings import (
        CAMARA_HEIGHT,
        CAMARA_INDEX,
        CAMARA_WIDTH,
        ROI_LED,
        UMBRAL_BRILLO,
        WARMUP_FRAMES,
        UMBRAL_PIXELES,
        DEBUG_DIR,       
    )
except ImportError:
    ROI_LED            = (301,162,307,167)
    UMBRAL_BRILLO      = 100
    UMBRAL_PIXELES     = 1
    CAMARA_INDEX       = 2
    CAMARA_WIDTH       = 1280
    CAMARA_HEIGHT      = 720
    WARMUP_FRAMES      = 5
    DEBUG_DIR          = "debug_output"

logger = logging.getLogger(__name__)


class DetectorLED:
    """
    Detecta si el LED indicador del AC esta encendido o apagado.

    Estrategia multi-criterio con votacion (2 de 4):
      - Criterio 1: Rojo puro HSV -> cubre el LED en condiciones ideales
      - Criterio 2: Magenta/Cyan HSV -> cubre el LED en condiciones normales en píxeles límite
      - Criterio 3: Blanco saturado -> cubre el LED sobreexpuesto por la cámara
      - Criterio 4: Brillo en grises -> criterio de respaldo independiente del color
    """

    def __init__(
        self,
        roi: tuple = ROI_LED,
        umbral_pixeles: int = UMBRAL_PIXELES,
        umbral_brillo: float = UMBRAL_BRILLO,
        camara_index: int = CAMARA_INDEX,
        imagen_estatica: Optional[str] = None,
    ):
        self.roi            = roi
        self.umbral_pixeles = umbral_pixeles
        self.umbral_brillo  = umbral_brillo
        self.camara_index   = camara_index
        self.imagen_estatica = imagen_estatica
        self._validar_roi()

    # API publica
    def detectar(self) -> Optional[bool]:
        """
        Captura un frame de la cámara y decide si el LED esta encendido.
        Si imagen_estatica esta configurada, analiza esa imagen en lugar
        de capturar de la cámara. Permite probar el pipeline completo
        sin necesidad de cámara en tiempo real.

        Returns:
            True  -> LED encendido (AC ON)
            False -> LED apagado   (AC OFF)
            None  -> fallo de cámara o ROI invalida
        """
        if self.imagen_estatica:
            logger.info(
                "[DetectorLED] Modo imagen estatica: %s", self.imagen_estatica
            )
            return self.analizar_imagen(self.imagen_estatica)
            
        # Modo normal, captura de cámara
        frame = self._capturar_frame()
        if frame is None:
            return None
        return self._analizar_frame(frame)

    def analizar_imagen(self, ruta) -> Optional[bool]:
        """
        Analiza una imagen guardada en lugar de capturar de la cámara.
        Util para pruebas offline y calibracion.
        """
        frame = cv2.imread(str(ruta))
        if frame is None:
            logger.error("[DetectorLED] No se pudo cargar imagen: %s", ruta)
            return None
        return self._analizar_frame(frame)

    def capturar_debug(self, directorio=".") -> Optional[bool]:
        """
        Captura un frame, analiza el LED y guarda imagenes de diagnostico.
        Genera: _original, _resultado, _zoom_x100, _ampliada
        """
        frame = self._capturar_frame()
        if frame is None:
            return None

        resultado = self._analizar_frame(frame)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base      = Path(directorio) / f"led_{timestamp}"

        self._guardar_imagen(frame, f"{base}_original.png")
        self._guardar_resultado(frame.copy(), resultado, f"{base}_resultado.png")
        self._guardar_zoom(frame, f"{base}_zoom_x100.png")
        self._guardar_ampliada(frame, f"{base}_ampliada.png")

        return resultado

    def analizar_imagen_debug(self, ruta, directorio_salida=".") -> Optional[bool]:
        """
        Equivalente a capturar_debug() pero cargando una imagen desde disco.
        Pensado para calibracion offline con imagenes de referencia.

        Genera los mismos 4 ficheros de diagnostico que capturar_debug():
          - <nombre>_original.png
          - <nombre>_resultado.png
          - <nombre>_zoom_x100.png
          - <nombre>_ampliada.png

        Args:
            ruta: Ruta a la imagen PNG/JPG de entrada.
            directorio_salida: Carpeta donde guardar los ficheros de debug.

        Returns:
            True/False/None igual que detectar()
        """
        ruta = Path(ruta)

        if not ruta.exists():
            logger.error("[DetectorLED] Imagen no encontrada: %s", ruta)
            return None

        frame = cv2.imread(str(ruta))
        if frame is None:
            logger.error("[DetectorLED] OpenCV no pudo cargar la imagen: %s", ruta)
            return None

        logger.debug(
            "[DetectorLED] Imagen cargada: %s (%dx%d)",
            ruta.name, frame.shape[1], frame.shape[0]
        )

        resultado = self._analizar_frame(frame)

        directorio_salida = Path(directorio_salida)
        directorio_salida.mkdir(parents=True, exist_ok=True)
        base = directorio_salida / ruta.stem

        self._guardar_imagen(frame, f"{base}_original.png")
        self._guardar_resultado(frame.copy(), resultado, f"{base}_resultado.png")
        self._guardar_zoom(frame, f"{base}_zoom_x100.png")
        self._guardar_ampliada(frame, f"{base}_ampliada.png")

        estado = "ENCENDIDO -> AC ON" if resultado else "APAGADO -> AC OFF"
        logger.info(
            "[DetectorLED] %s -> LED %s | ficheros en: %s/",
            ruta.name, estado, directorio_salida
        )

        return resultado

    # Logica de deteccion interna
    def _analizar_frame(self, frame: np.ndarray) -> Optional[bool]:
        """
        Nucleo de la deteccion. Aplica 4 criterios sobre la ROI
        y decide por votacion mayoritaria (>= 2 de 4).
        """
        x1, y1, x2, y2 = self.roi

        if y2 > frame.shape[0] or x2 > frame.shape[1]:
            logger.error(
                "[DetectorLED] ROI %s excede dimensiones del frame %s",
                self.roi, frame.shape[:2]
            )
            return None

        roi_bgr = frame[y1:y2, x1:x2].copy()

        if roi_bgr.size == 0:
            logger.error("[DetectorLED] ROI vacia tras recorte: %s", self.roi)
            return None

        hsv  = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)

        
        # Criterio 1: Rojo puro HSV, umbrales bajados para LED oscuro/subexpuesto
        c1_mask = cv2.bitwise_or(
            cv2.inRange(hsv, np.array([0,   40, 30]), np.array([10,  255, 150])),
            cv2.inRange(hsv, np.array([170, 40, 30]), np.array([179, 255, 150]))
        )
        c1_px = cv2.countNonZero(c1_mask)
        c1_ok = c1_px >= self.umbral_pixeles

        # Criterio 2: Magenta/Cyan (rojo captado con deriva de color por la cámara)
        c2_mask = cv2.inRange(
            hsv, np.array([170, 70, 100]), np.array([179, 255, 255])
        )
        c2_px = cv2.countNonZero(c2_mask)
        c2_ok = c2_px >= self.umbral_pixeles

        # Criterio 3: Rosa/Blanco saturado (LED sobreexpuesto por luz ambiente)
        c3_mask = cv2.bitwise_or(
            cv2.inRange(hsv, np.array([0,  0,  180]), np.array([179, 50,  255])),
            cv2.inRange(hsv, np.array([0,  20, 150]), np.array([179, 120, 255]))
        )
        c3_px = cv2.countNonZero(c3_mask)
        c3_ok = c3_px >= self.umbral_pixeles

        # Criterio 4: Brillo alto en escala de grises (independiente del color)
        brillo_prom = float(np.mean(gray))
        brillo_max  = int(np.max(gray))
        c4_ok = brillo_prom > 10 or brillo_max > 40

        votos     = sum([c1_ok, c2_ok, c3_ok, c4_ok])
        encendido = votos >= 2

        logger.debug(
            "[DetectorLED] C1(rojo)=%s(%dpx) C2(magenta)=%s(%dpx) "
            "C3(rosa/blanco)=%s(%dpx) C4(brillo)=%s(%.0f/max%d) -> votos=%d/4 -> %s",
            c1_ok, c1_px,
            c2_ok, c2_px,
            c3_ok, c3_px,
            c4_ok, brillo_prom, brillo_max,
            votos, "ON" if encendido else "OFF"
        )

        return encendido


    # Cámara
    def _capturar_frame(self) -> Optional[np.ndarray]:
        """
        Abre la cámara, descarta warmup frames y devuelve el frame definitivo.
        El finally garantiza que cap.release() se llama siempre.
        """
        cap = cv2.VideoCapture(self.camara_index)
        try:
            if not cap.isOpened():
                logger.error(
                    "[DetectorLED] No se pudo abrir /dev/video%d", self.camara_index
                )
                return None

            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAMARA_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMARA_HEIGHT)

            for i in range(WARMUP_FRAMES):
                ok, _ = cap.read()
                if not ok:
                    logger.warning(
                        "[DetectorLED] Frame de warmup %d/%d falló",
                        i + 1, WARMUP_FRAMES
                    )

            ok, frame = cap.read()
            if not ok or frame is None:
                logger.error("[DetectorLED] No se pudo capturar el frame definitivo")
                return None

            logger.debug(
                "[DetectorLED] Frame capturado: %dx%d",
                frame.shape[1], frame.shape[0]
            )
            return frame

        finally:
            cap.release()

    # Generacion de imagenes de debug
    def _guardar_imagen(self, frame: np.ndarray, ruta: str) -> None:
        ok = cv2.imwrite(ruta, frame)
        if ok:
            logger.debug("[DetectorLED] Guardada: %s", ruta)
        else:
            logger.warning("[DetectorLED] No se pudo guardar: %s", ruta)

    def _guardar_resultado(self, frame: np.ndarray, encendido: Optional[bool], ruta: str) -> None:
        """Superpone ROI, estado y timestamp sobre el frame."""
        x1, y1, x2, y2 = self.roi
        color = (0, 0, 255) if encendido else (0, 255, 0)
        texto = "LED: ENCENDIDO" if encendido else "LED: APAGADO"

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 1)
        cv2.putText(
            frame, texto, (30, 60),
            cv2.FONT_HERSHEY_SIMPLEX, 2.0, color, 3, cv2.LINE_AA
        )
        cv2.putText(
            frame,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            (30, frame.shape[0] - 30),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA
        )
        self._guardar_imagen(frame, ruta)

    def _guardar_zoom(self, frame: np.ndarray, ruta: str, factor: int = 100) -> None:
        """Genera zoom x100 pixel a pixel de la ROI con grid superpuesto."""
        x1, y1, x2, y2 = self.roi
        roi_bgr = frame[y1:y2, x1:x2].copy()

        if roi_bgr.size == 0:
            logger.warning("[DetectorLED] ROI vacia, no se genera zoom")
            return

        h, w = roi_bgr.shape[:2]
        zoom = cv2.resize(
            roi_bgr, (w * factor, h * factor),
            interpolation=cv2.INTER_NEAREST
        )

        for i in range(1, w):
            cv2.line(zoom, (i * factor, 0), (i * factor, h * factor), (80, 80, 80), 1)
        for i in range(1, h):
            cv2.line(zoom, (0, i * factor), (w * factor, i * factor), (80, 80, 80), 1)

        font = cv2.FONT_HERSHEY_SIMPLEX
        for py in range(h):
            for px in range(w):
                cv2.putText(
                    zoom, f"[{px},{py}]",
                    (px * factor + 8, py * factor + 28),
                    font, 0.45, (200, 200, 200), 1
                )

        self._guardar_imagen(zoom, ruta)

    def _guardar_ampliada(self, frame: np.ndarray, ruta: str, margen: int = 50) -> None:
        """Guarda el contexto alrededor de la ROI para verificar encuadre."""
        x1, y1, x2, y2   = self.roi
        h_frame, w_frame  = frame.shape[:2]

        xa1 = max(0, x1 - margen)
        ya1 = max(0, y1 - margen)
        xa2 = min(w_frame, x2 + margen)
        ya2 = min(h_frame, y2 + margen)

        ampliada = frame[ya1:ya2, xa1:xa2].copy()
        cv2.rectangle(
            ampliada,
            (x1 - xa1, y1 - ya1), (x2 - xa1, y2 - ya1),
            (0, 255, 255), 1
        )
        self._guardar_imagen(ampliada, ruta)

    # Utilidades
    def _validar_roi(self) -> None:
        x1, y1, x2, y2 = self.roi
        if x2 <= x1 or y2 <= y1:
            raise ValueError(
                f"[DetectorLED] ROI invalida {self.roi}: "
                "x2 debe ser > x1 e y2 debe ser > y1"
            )

    def debug_pixeles(self, ruta) -> None:
        """
        Imprime valores BGR/HSV/Gray de cada pixel de la ROI.
        Solo para calibracion, no llamar desde produccion.
        """
        frame = cv2.imread(str(ruta))
        if frame is None:
            logger.error("[DetectorLED] No se pudo cargar: %s", ruta)
            return

        x1, y1, x2, y2 = self.roi
        roi_bgr = frame[y1:y2, x1:x2]
        roi_hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        roi_gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        h, w = roi_bgr.shape[:2]

        print(f"\n{'='*55}")
        print(f"[DetectorLED] Pixeles ROI {self.roi} en: {ruta}")
        print(f"{'='*55}")
        for py in range(h):
            for px in range(w):
                b, g, r = roi_bgr[py, px]
                hh, s, v = roi_hsv[py, px]
                gray = roi_gray[py, px]
                print(
                    f"  [{px},{py}]  "
                    f"BGR=({b:3d},{g:3d},{r:3d})  "
                    f"HSV=(H:{hh:3d} S:{s:3d} V:{v:3d})  "
                    f"Gray:{gray:3d}"
                )


# Ejecucion independiente
def _configurar_logging_independiente() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )


def main() -> int:
    _configurar_logging_independiente()

    detector = DetectorLED(
        roi=ROI_LED,
        umbral_pixeles=UMBRAL_PIXELES,
        umbral_brillo=UMBRAL_BRILLO,
        camara_index=CAMARA_INDEX,
    )

    # Modo imagen: python detector_led.py foto1.png foto2.png ...
    if len(sys.argv) > 1:
        imagenes = sys.argv[1:]
        print(f"\n{'='*50}")
        print(f"[DetectorLED] Modo offline - {len(imagenes)} imagen(es)")
        print(f"[DetectorLED] ROI: {ROI_LED} | Umbral brillo: {UMBRAL_BRILLO}")
        print(f"{'='*50}")

        codigo_salida = 0
        for ruta in imagenes:
            print(f"\n[DetectorLED] Analizando: {ruta}")
            resultado = detector.analizar_imagen_debug(
                ruta=ruta,
                directorio_salida= DEBUG_DIR
            )
            if resultado is None:
                print(f"[DetectorLED] ERROR: no se pudo analizar {ruta}")
                codigo_salida = 1
            else:
                estado = "ENCENDIDO -> AC ON" if resultado else "APAGADO -> AC OFF"
                print(f"[DetectorLED] RESULTADO: LED {estado}")

        print(f"\n[DetectorLED] Debug guardado en: {DEBUG_DIR}/")
        return codigo_salida

    # Modo cámara: python detector_led.py (sin argumentos)
    print(f"\n{'='*50}")
    print(f"[DetectorLED] Modo cámara")
    print(f"[DetectorLED] ROI: {ROI_LED} | Umbral brillo: {UMBRAL_BRILLO}")
    print(f"{'='*50}\n")

    resultado = detector.capturar_debug(directorio=".")

    print(f"\n{'='*50}")
    if resultado is None:
        print("[DetectorLED] ERROR: No se pudo determinar el estado del LED")
        return 1

    estado = "ENCENDIDO -> AC ON" if resultado else "APAGADO -> AC OFF"
    print(f"[DetectorLED] RESULTADO FINAL: LED {estado}")
    print(f"{'='*50}\n")

    return 0 if resultado else 1


if __name__ == "__main__":
    exit(main())
