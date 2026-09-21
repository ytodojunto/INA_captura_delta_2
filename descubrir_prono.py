"""
Discovery de series de pronostico (seriesProno) del INA para las 19
estaciones que ya venimos sincronizando (data/series/*.csv en este
mismo repo), mas una prueba real de datosProno para confirmar
estructura, horizonte y granularidad.

Usa el separador "&" en vez de "?" para query params - confirmado
2026-09-21 que esta API lo requiere asi (con "?" contesta "Argumento
timeStart faltante" aunque el parametro este bien puesto).
"""

import json
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

BASE = "https://alerta.ina.gob.ar/pub/datos"
OUT_DIR = Path("data/discovery_prono")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# mismos sitecodes que sincronizar_ina.py (sin Brazo Largo)
ESTACIONES = {
    19: "corrientes", 20: "barranqueras", 23: "goya", 29: "parana_ciudad",
    30: "santa_fe", 31: "diamante", 32: "victoria", 33: "san_lorenzo_san_martin",
    34: "rosario", 35: "villa_constitucion", 36: "san_nicolas", 37: "ramallo",
    38: "san_pedro", 39: "baradero", 41: "campana", 45: "ibicuy",
    47: "martin_garcia", 52: "san_fernando", 85: "buenos_aires", 86: "la_plata",
}


def get_json(path, params, retries=2, pausa=3, timeout=150):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}/{path}&{qs}"
    ultimo_error = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "descubrir_prono/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()), url, None
        except Exception as e:
            ultimo_error = f"{type(e).__name__}: {e}"
            time.sleep(pausa)
    print(f"   [ERROR real] {url} -> {ultimo_error}")
    return None, url, ultimo_error


def guardar(nombre, contenido):
    (OUT_DIR / nombre).write_text(json.dumps(contenido, indent=2, ensure_ascii=False))
    print(f"  guardado: {OUT_DIR / nombre}")


def main():
    resumen = {"generado_en": datetime.utcnow().isoformat() + "Z", "estaciones": {}}

    print("== seriesProno: listado completo ==")
    todas, url, err = get_json("seriesProno", {"format": "json"})
    if not todas:
        print("  [ERROR] no se pudo traer seriesProno")
        guardar("_resumen_prono.json", resumen)
        return
    series = todas.get("data", [])
    print(f"   total series de pronostico en la base: {len(series)}")
    guardar("_muestra_una_serie_prono.json", series[0] if series else {})

    # filtrar local por nuestros sitecodes
    for sitecode, slug in ESTACIONES.items():
        propias = [s for s in series if s.get("sitecode") == sitecode]
        print(f"-- {slug} (sitecode={sitecode}): {len(propias)} series de pronostico")
        resumen["estaciones"][slug] = {
            "sitecode": sitecode,
            "cantidad_series_prono": len(propias),
            "series": propias[:5],  # guardamos como mucho 5 de muestra, no hace falta mas
        }
        if propias:
            guardar(f"series_prono_{sitecode}_{slug}.json", propias)

    # prueba real de datosProno - una para San Fernando (el modelo
    # marea_rdp_regre es el que mas nos interesa comparar) y otra para
    # Corrientes (representa el pronostico "tabprono_central" del tramo
    # medio). datosProno pide seriesId Y (calId o corId), no alcanza
    # con seriesId solo (confirmado 2026-09-21: sin calId/corId contesta
    # "Falta parametro calId o corId").
    print("\n== Prueba real de datosProno ==")
    hoy = datetime.utcnow().date()
    en_14_dias = hoy + timedelta(days=14)

    pruebas = [
        (52, "san_fernando"),
        (19, "corrientes"),
    ]
    for sitecode, slug in pruebas:
        propias = resumen["estaciones"].get(slug, {}).get("series", [])
        if not propias:
            print(f"-- {slug}: sin serie de prono, se omite")
            continue
        serie = propias[0]
        series_id = serie.get("seriesid")
        cal_id = serie.get("calid")
        print(f"-- probando con {slug} (seriesId={series_id}, calId={cal_id}, modelo={serie.get('cal_name')})")
        data, url, err = get_json("datosProno", {
            "timeStart": hoy.isoformat(),
            "timeEnd": en_14_dias.isoformat(),
            "seriesId": series_id,
            "calId": cal_id,
            "format": "json",
        })
        if data:
            guardar(f"prueba_datosProno_{sitecode}_{slug}.json", data)
            print(f"   OK, guardado. url usada: {url}")
        else:
            print(f"   [ERROR] no se pudo traer datosProno para {slug}")
            guardar(f"prueba_datosProno_{sitecode}_{slug}_FALLO.json",
                    {"url_intentada": url, "error_real": err})

    guardar("_resumen_prono.json", resumen)
    print("\nListo. Revisar data/discovery_prono/_resumen_prono.json")


if __name__ == "__main__":
    main()
