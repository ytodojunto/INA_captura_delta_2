"""
Discovery de la API pública de datos hidrológicos del INA
(https://alerta.ina.gob.ar/pub/datos/).

Objetivo de esta corrida (fase de descubrimiento, no de captura definitiva):
1. Listar variables observadas (para saber el varId de altura hidrométrica).
2. Listar estaciones de las redes que nos interesan:
   - redId 23: Mareógrafos SHN (candidata a reemplazar el scraper de
     hidro.gov.ar que usamos en SHN_captura_pronosticos)
   - redId 22: Estaciones mareográficas PVNyMM
   - redId 32: red INA Delta y AMBA (estaciones automáticas del delta)
   - redId 10: escalas Prefectura Nacional (donde están Brazo Largo,
     Campana, Martín García, San Fernando, etc. - ya identificados)
3. Para las estaciones puntuales que ya nos interesan (Brazo Largo,
   Campana, Martín García, San Fernando, Buenos Aires, La Plata):
   listar sus series disponibles (series -> seriesId, varId) y bajar
   una muestra de datos para ver granularidad temporal y profundidad
   histórica real (no solo lo que dice la ficha de la estación).

No decide todavía cómo estructurar la captura definitiva - solo mira y
guarda una copia cruda de las respuestas en data/discovery/ para
revisar después, igual que se hizo con CARP en su momento.
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta

BASE = "https://alerta.ina.gob.ar/pub/datos"
OUT_DIR = Path("data/discovery")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Estaciones puntuales de interes (sitecode: nombre)
ESTACIONES_INTERES = {
    97: "brazo_largo",
    41: "campana",
    47: "martin_garcia",
    52: "san_fernando",
    85: "buenos_aires",
    86: "la_plata",
    # tramo Rosario/San Lorenzo -> delta, agregadas 2026-09-21
    33: "san_lorenzo_san_martin",
    34: "rosario",
    35: "villa_constitucion",
    36: "san_nicolas",
    37: "ramallo",
    38: "san_pedro",
    39: "baradero",
    45: "ibicuy",
    # tramo medio del Parana desde Corrientes hacia el delta (senal
    # hidrologica, no mareal/eolica), agregadas 2026-09-21 a pedido:
    # Corrientes es el primer punto donde ya esta mezclada el agua del
    # Alto Parana con la del rio Paraguay, asi que no hace falta ir mas
    # arriba (se descartan Posadas, Yacyreta y Confluencia con Brasil).
    # Se propaga rio abajo con dias de retraso - insumo para el escenario
    # de largo plazo, distinto de la senal de marea/viento del RDP
    19: "corrientes",
    20: "barranqueras",
    23: "goya",
    29: "parana_ciudad",
    30: "santa_fe",
    31: "diamante",
    32: "victoria",
}

# Redes a inventariar completas
REDES_INTERES = {
    23: "mareografos_shn",
    22: "pvnymm",
    32: "ina_delta_amba",
    10: "prefectura_nacional",
}


def get_json(path, params=None, retries=3, pausa=2):
    """GET con reintentos simples. params se arma como query string estandar."""
    url = f"{BASE}/{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    ultimo_error = None
    for intento in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "descubrir_ina/0.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            return json.loads(data), url
        except Exception as e:
            ultimo_error = e
            time.sleep(pausa)
    print(f"  [ERROR] {url} -> {ultimo_error}")
    return None, url


def guardar(nombre, contenido):
    p = OUT_DIR / nombre
    with open(p, "w", encoding="utf-8") as f:
        json.dump(contenido, f, ensure_ascii=False, indent=2)
    print(f"  guardado: {p}")


def main():
    resumen = {"generado_en": datetime.utcnow().isoformat() + "Z", "pasos": []}

    # ---------- 1. Variables ----------
    print("== Variables observadas ==")
    variables, url = get_json("variables", {"format": "json"})
    if variables:
        guardar("variables.json", variables)
        resumen["pasos"].append({"paso": "variables", "url": url, "ok": True,
                                  "cantidad": len(variables.get("data", []))})
    else:
        resumen["pasos"].append({"paso": "variables", "url": url, "ok": False})

    # ---------- 2. Estaciones: bajar TODO una vez y filtrar local ----------
    # El filtro redId por query string no esta funcionando en el server
    # (devuelve el listado completo sin filtrar, se verifico empiricamente
    # el 2026-09-20 - mismo total para cualquier redId probado). Bajamos
    # el listado completo una sola vez y filtramos nosotros en Python.
    print("\n== Estaciones (listado completo, filtrado local) ==")
    todas_estaciones, url_est = get_json("estaciones", {"format": "json"})
    estaciones_data = todas_estaciones.get("data", []) if todas_estaciones else []
    print(f"   total en la base: {len(estaciones_data)}")
    resumen["pasos"].append({"paso": "estaciones_completo", "url": url_est,
                              "ok": bool(todas_estaciones), "cantidad": len(estaciones_data)})

    for red_id, slug in REDES_INTERES.items():
        filtradas = [e for e in estaciones_data if e.get("redes_id") == red_id]
        print(f"-- redId={red_id} ({slug}): {len(filtradas)} estaciones")
        guardar(f"estaciones_red_{red_id}_{slug}.json", {"data": filtradas})
        resumen["pasos"].append({"paso": f"estaciones_red_{red_id}", "ok": True,
                                  "cantidad": len(filtradas), "filtrado": "local"})

    # ---------- 3. Series: bajar TODO una vez y filtrar local ----------
    # Mismo problema: el filtro siteCode tampoco esta funcionando.
    print("\n== Series (listado completo, filtrado local) ==")
    todas_series, url_ser = get_json("series", {"format": "json"})
    series_data = todas_series.get("data", []) if todas_series else []
    print(f"   total en la base: {len(series_data)}")
    resumen["pasos"].append({"paso": "series_completo", "url": url_ser,
                              "ok": bool(todas_series), "cantidad": len(series_data)})

    series_por_estacion = {}
    for sitecode, slug in ESTACIONES_INTERES.items():
        filtradas = [s for s in series_data if s.get("sitecode") == sitecode]
        print(f"-- sitecode={sitecode} ({slug}): {len(filtradas)} series")
        guardar(f"series_{sitecode}_{slug}.json", {"data": filtradas})
        series_por_estacion[sitecode] = filtradas
        resumen["pasos"].append({"paso": f"series_{sitecode}", "ok": True,
                                  "cantidad": len(filtradas), "filtrado": "local"})

    # ---------- 4. Profundidad historica (de la metadata de la serie) ----------
    # La propia respuesta de "series" ya trae from_date/to_date/obs_count
    # por serie - no hace falta pegarle a "datos" con rango amplio para
    # saber la profundidad historica real, la metadata ya lo dice.
    print("\n== Profundidad historica (segun metadata de series) ==")
    profundidad = {}
    for sitecode, slug in ESTACIONES_INTERES.items():
        series = series_por_estacion.get(sitecode, [])
        # nos quedamos con las series de altura hidrometrica (varid=2 tipicamente)
        series_altura = [s for s in series if (s.get("var_nombre") or "").lower().startswith("altura")]
        if not series_altura:
            series_altura = series  # fallback: todas si no hay match por nombre
        info = [{
            "seriesid": s.get("seriesid"), "varid": s.get("varid"),
            "var_nombre": s.get("var_nombre"), "from_date": s.get("from_date"),
            "to_date": s.get("to_date"), "obs_count": s.get("obs_count"),
        } for s in series_altura]
        profundidad[sitecode] = info
        for s in info:
            print(f"-- sitecode={sitecode} ({slug}) varid={s['varid']} ({s['var_nombre']}): "
                  f"{s['from_date']} -> {s['to_date']} ({s['obs_count']} obs)")
        resumen["pasos"].append({"paso": f"profundidad_{sitecode}", "ok": True, "series": info})
    guardar("profundidad_historica.json", profundidad)

    # ---------- 5. Validacion puntual del endpoint "datos" ----------
    # Un solo chequeo (San Fernando, ultimos 10 dias) para confirmar si el
    # filtro siteCode+varId de "datos" funciona de verdad (a diferencia de
    # "estaciones" y "series", que ignoran el filtro) y ver la granularidad
    # temporal real (horaria, diaria, etc). Se usa San Fernando (52) en vez
    # de Brazo Largo porque Brazo Largo no tiene serie con datos (ver
    # profundidad_historica.json: from_date/to_date/obs_count en null).
    print("\n== Validacion puntual del endpoint 'datos' (San Fernando) ==")
    hoy = datetime.utcnow().date()
    hace_10_dias = hoy - timedelta(days=10)
    series_sf = profundidad.get(52, [])
    # preferimos la serie principal (varid=2) si esta, si no la primera disponible
    serie_principal = next((s for s in series_sf if s["varid"] == 2 and s["obs_count"]), None)
    var_id_sf = serie_principal["varid"] if serie_principal else (series_sf[0]["varid"] if series_sf else 2)
    data_val, url_val = get_json("datos", {
        "timeStart": hace_10_dias.isoformat(),
        "timeEnd": hoy.isoformat(),
        "siteCode": 52,
        "varId": var_id_sf,
        "format": "json",
    })
    if data_val:
        regs = data_val.get("data", [])
        n = len(regs)
        print(f"   {n} registros en los ultimos 10 dias (varId={var_id_sf})")
        guardar("datos_validacion_san_fernando.json", data_val)
        resumen["pasos"].append({"paso": "datos_validacion_san_fernando", "url": url_val,
                                  "ok": True, "cantidad": n,
                                  "filtro_parece_funcionar": n < 5000,
                                  "primeros_3": regs[:3]})
    else:
        resumen["pasos"].append({"paso": "datos_validacion_san_fernando", "url": url_val, "ok": False})

    guardar("_resumen_discovery.json", resumen)
    print("\nListo. Revisar data/discovery/_resumen_discovery.json para el panorama general.")


if __name__ == "__main__":
    main()
