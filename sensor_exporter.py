# Se ejecuta en el nodo del cluster al que estan conectados los sensores USB.
import os
import sys
import uvicorn
from fastapi import FastAPI, Response
from serial import Serial

app = FastAPI(title="Extractor de Temperatura USB")

# Numeros de serie de los sensores USB, para distinguir el del pasillo frio del
# del pasillo caliente. Se configuran por entorno (ver .env.example). Si no se
# definen, se asigna por orden de lectura de los puertos.
COLD_AISLE_SN = os.getenv("COLD_AISLE_SN", "")
HOT_AISLE_SN  = os.getenv("HOT_AISLE_SN", "")

def clean_response(reply: bytes) -> str:
    """Limpia los caracteres de formato del sensor tal como hace el script original."""
    r = reply.decode('utf-8', errors='ignore')
    for char in ['\r', '\n', 'C', ' ', 'O', 'K']:
        r = r.replace(char, '')
    return r

def read_physical_sensor(port: str):
    """Se conecta al puerto serie, pide el número de serie y la temperatura."""
    try:
        # Timeout de 2 segundos para evitar que se quede colgado si el USB falla
        with Serial(port, 9600, timeout=2) as ser:
            ser.flush()
            # 1. Solicitar Número de Serie (Comando RX707)
            ser.write("RX707".encode())
            sn = ser.readline().hex()
            
            # 2. Solicitar Temperatura (Comando RX201)
            ser.flush()
            ser.write("RX201".encode())
            raw_temp = ser.readline()
            
            temp_str = clean_response(raw_temp)
            temp = float(temp_str)
            
            if -24 <= temp <= 103:
                return sn, temp
    except Exception as e:
        print(f"[ERROR] Error leyendo {port}: {e}", file=sys.stderr)
    return None, None

def get_all_temperatures():
    """Escanea los puertos ttyUSB habituales de Linux y clasifica las lecturas."""
    data = {"pasillo_frio": None, "pasillo_calor": None}
    
    # Escaneamos los dos posibles nombres que asigna Linux
    for port in ['/dev/ttyUSB0', '/dev/ttyUSB1']:
        sn, temp = read_physical_sensor(port)
        if COLD_AISLE_SN and sn == COLD_AISLE_SN:
            data["pasillo_frio"] = temp
        elif HOT_AISLE_SN and sn == HOT_AISLE_SN:
            data["pasillo_calor"] = temp
        elif temp is not None:
            # Fallback por si los números de serie cambiaran en el futuro
            if data["pasillo_frio"] is None:
                data["pasillo_frio"] = temp
            else:
                data["pasillo_calor"] = temp
    return data

@app.get("/temperatura")
def get_temperatura_json():
    """
    Devuelve la del pasillo frío por ser la más representativa del ambiente general.
    """
    data = get_all_temperatures()
    temp_ambiente = data["pasillo_frio"] or data["pasillo_calor"]
    
    if temp_ambiente is None:
        return {"temperatura": None, "humedad": None, "status": "Error: Sensores no disponibles"}, 500
        
    return {"temperatura": temp_ambiente, "humedad": None, "status": "OK"}

@app.get("/metrics")
def get_prometheus_metrics():
    """
    Métricas nativas directas para Prometheus de ambos sensores.
    """
    data = get_all_temperatures()
    lines = [
        "# HELP ac_sala_temperatura_celsius Temperatura de los pasillos de la sala de servidores",
        "# TYPE ac_sala_temperatura_celsius gauge"
    ]
    
    if data["pasillo_frio"] is not None:
        lines.append(f'ac_sala_temperatura_celsius{{zone="cold_aisle"}} {data["pasillo_frio"]}')
    if data["pasillo_calor"] is not None:
        lines.append(f'ac_sala_temperatura_celsius{{zone="hot_aisle"}} {data["pasillo_calor"]}')
        
    output = "\n".join(lines) + "\n"
    return Response(content=output, media_type="text/plain")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
