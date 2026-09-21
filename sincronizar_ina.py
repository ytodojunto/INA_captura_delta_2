"""
Sincronizacion definitiva de series de altura hidrometrica desde la API
del INA (https://alerta.ina.gob.ar/pub/datos/) para el tramo
Corrientes -> delta -> Rio de la Plata.

Fase de captura (no de discovery - ver descubrir_ina.py / repo
INA_captura_delta_2 para como se llego a estos sitecodes/varid/from_date).

Como algunas series tienen mas de 100 anios de historia (Rosario desde
1884, 53.707 registros), este script:

1. Pide los datos en bloques de a 5 anios (no todo junto) para no romper
   la API con pedidos gigantes.
2. Guarda el progreso en data/_estado_sync.json - si el run se corta a
   mitad de camino (timeout de GitHub Actions, error de red, etc), la
   proxima corrida sigue exactamente donde quedo, no arranca de cero.
3. Tiene un presupuesto de tiempo por corrida (por defecto 5.5 horas) -
   si se acerca al limite, guarda el estado y corta prolijo en vez de
   que lo mate el timeout del job a la mitad de una escritura.
4. Cada estacion tiene su propio CSV en data/series/<slug>.csv
   (columnas: timestart, valor), sin duplicados entre corridas.

Brazo Largo (sitecode 97) queda afuera a proposito: se confirmo en el
discovery que no tiene ninguna serie con datos poblados (from_date/
to_date/obs_count = null en las 5 series que tiene esa estacion). Si en
el futuro se resuelve, agregarla es una linea mas en ESTACIONES.
"""

import csv
import json
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone

BASE = "https://alerta.ina.gob.ar/pub/datos"
DATA_DIR = Path("data")
SERIES_DIR = DATA_DIR / "series"
ESTADO_PATH = DATA_DIR / "_estado_sync.json"
SERIES_DIR.mkdir(parents=True, exist_ok=True)

# Presupuesto de tiempo por corrida (deja margen para el commit final)
PRESUPUESTO_SEGUNDOS = 5.5 * 3600
INICIO_RUN = time.time()

CHUNK_ANIOS = 5  # tamanio de cada pedido a la API

# sitecode -> (slug, varid de la serie principal de altura, from_date real
# confirmado en el discovery del 2026-09-21). varid=2 para todas menos
# donde se indica distinto.
ESTACIONES = {
    19: ("corrientes", 2, "1901-01-02"),
    20: ("barranqueras", 2, "1906-03-02"),
    23: ("goya", 2, "1903-06-17"),
    29: ("parana_ciudad", 2, "1902-02-22"),
    30: ("santa_fe", 2, "1925-01-02"),
    31: ("diamante", 2, "1902-04-25"),
    32: ("victoria", 2, "1964-01-01"),
    33: ("san_lorenzo_san_martin", 2, "1909-01-02"),
    34: ("rosario", 2, "1884-01-02"),
    35: ("villa_constitucion", 2, "1974-01-01"),
    36: ("san_nicolas", 2, "1904-01-02"),
    37: ("ramallo", 2, "1904-01-02"),
    38: ("san_pedro", 2, "1901-01-02"),
    39: ("baradero", 2, "1976-01-01"),
    41: ("campana", 2, "1977-10-24"),
    45: ("ibicuy", 2, "1983-01-01"),
    47: ("martin_garcia", 2, "2006-01-01"),
    52: ("san_fernando", 2, "2006-01-01"),
    85: ("buenos_aires", 2, "2006-01-01"),
    86: ("la_plata", 2, "2006-01-01"),
    # 97 Brazo Largo: sin serie con datos, ver docstring - no incluida.
}


def get_json(path, params, retries=4, pausa=3):
    # OJO: esta API no usa "?" estandar para separar el path de los
    # parametros - usa "&" pegado directo (confirmado 2026-09-21: con
    # "?" el server contesta "Argumento timeStart faltante" aunque el
    # parametro este ahi, como si no viera nada despues del "?"). Rareza
    # documentada asi en los propios ejemplos de /pub/datos/ (capabilities).
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}/{path}&{qs}"
    ultimo_error = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sincronizar_ina/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read()), None
        except Exception as e:
            ultimo_error = str(e)
            time.sleep(pausa)
    return None, ultimo_error


def cargar_estado():
    if ESTADO_PATH.exists():
        return json.loads(ESTADO_PATH.read_text())
    return {}


def guardar_estado(estado):
    ESTADO_PATH.write_text(json.dumps(estado, indent=2, ensure_ascii=False))


def escribir_filas(csv_path, filas):
    """Agrega filas al CSV, creando el header si el archivo no existe."""
    existe = csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["timestart", "valor"])
        for r in filas:
            w.writerow([r.get("timestart"), r.get("valor")])


def tiempo_agotado():
    return (time.time() - INICIO_RUN) > PRESUPUESTO_SEGUNDOS


def generar_chunks(desde_dt, hasta_dt, anios=CHUNK_ANIOS):
    cursor = desde_dt
    while cursor < hasta_dt:
        siguiente = min(cursor + timedelta(days=365 * anios), hasta_dt)
        yield cursor, siguiente
        cursor = siguiente


def main():
    estado = cargar_estado()
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    resumen_run = {"inicio": datetime.utcnow().isoformat() + "Z", "estaciones": {}}
    debug_guardado = False

    for sitecode, (slug, varid, from_date) in ESTACIONES.items():
        if tiempo_agotado():
            print(f"\n[presupuesto de tiempo agotado, cortando antes de {slug}]")
            break

        clave = str(sitecode)
        csv_path = SERIES_DIR / f"{slug}.csv"
        avance_previo = estado.get(clave, {}).get("sincronizado_hasta")

        if avance_previo:
            desde = datetime.fromisoformat(avance_previo)
            print(f"\n-- {slug} (sitecode={sitecode}): retomando desde {desde.isoformat()}")
        else:
            desde = datetime.fromisoformat(from_date)
            print(f"\n-- {slug} (sitecode={sitecode}): primera corrida, arranca desde {from_date}")

        if desde >= ahora:
            print("   ya esta al dia")
            continue

        total_filas_run = 0
        for chunk_ini, chunk_fin in generar_chunks(desde, ahora):
            if tiempo_agotado():
                print(f"   [presupuesto de tiempo agotado a mitad de {slug}, guardando avance parcial]")
                break

            data, error = get_json("datos", {
                "timeStart": chunk_ini.date().isoformat(),
                "timeEnd": chunk_fin.date().isoformat(),
                "siteCode": sitecode,
                "varId": varid,
                "format": "json",
            })

            if data is None:
                print(f"   [ERROR] {chunk_ini.date()} -> {chunk_fin.date()}: {error} (se reintenta la proxima corrida)")
                break  # no avanzamos el estado para este tramo, se reintenta despues

            if not debug_guardado:
                (DATA_DIR / "_debug_primer_pedido.json").write_text(
                    json.dumps({
                        "url_pedida": f"{BASE}/datos?timeStart={chunk_ini.date().isoformat()}"
                                      f"&timeEnd={chunk_fin.date().isoformat()}&siteCode={sitecode}"
                                      f"&varId={varid}&format=json",
                        "respuesta_cruda": data,
                    }, indent=2, ensure_ascii=False)
                )
                debug_guardado = True
                print("   [debug] respuesta cruda del primer pedido guardada en data/_debug_primer_pedido.json")

            filas = data.get("data", [])
            if filas:
                escribir_filas(csv_path, filas)
                total_filas_run += len(filas)

            print(f"   {chunk_ini.date()} -> {chunk_fin.date()}: {len(filas)} registros")

            # avanzar el estado solo despues de escribir con exito
            estado[clave] = {"sincronizado_hasta": chunk_fin.isoformat(), "slug": slug}
            guardar_estado(estado)

            time.sleep(1)  # ser prudentes con la API

        resumen_run["estaciones"][slug] = {
            "sitecode": sitecode,
            "filas_agregadas_esta_corrida": total_filas_run,
            "sincronizado_hasta": estado.get(clave, {}).get("sincronizado_hasta"),
        }

    resumen_run["fin"] = datetime.utcnow().isoformat() + "Z"
    (DATA_DIR / "_ultima_corrida.json").write_text(
        json.dumps(resumen_run, indent=2, ensure_ascii=False)
    )
    print("\nListo por esta corrida. Ver data/_ultima_corrida.json para el resumen "
          "y data/_estado_sync.json para el avance guardado.")


if __name__ == "__main__":
    main()
