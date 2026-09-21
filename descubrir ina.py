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

# Estaciones puntuales ya identificadas (sitecode: nombre)
ESTACIONES_INTERES = {
    97: "brazo_largo",
    41: "campana",
    47: "martin_garcia",
    52: "san_fernando",
    85: "buenos_aires",
    86: "la_plata",
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

    # ---------- 2. Estaciones por red de interes ----------
    print("\n== Estaciones por red ==")
    for red_id, slug in REDES_INTERES.items():
        print(f"-- redId={red_id} ({slug})")
        data, url = get_json("estaciones", {"redId": red_id, "format": "json"})
        if data:
            n = len(data.get("data", []))
            print(f"   {n} estaciones")
            guardar(f"estaciones_red_{red_id}_{slug}.json", data)
            resumen["pasos"].append({"paso": f"estaciones_red_{red_id}", "url": url,
                                      "ok": True, "cantidad": n})
        else:
            resumen["pasos"].append({"paso": f"estaciones_red_{red_id}", "url": url, "ok": False})
        time.sleep(1)

    # ---------- 3. Series disponibles por estacion de interes ----------
    print("\n== Series por estacion de interes ==")
    series_por_estacion = {}
    for sitecode, slug in ESTACIONES_INTERES.items():
        print(f"-- sitecode={sitecode} ({slug})")
        data, url = get_json("series", {"siteCode": sitecode, "format": "json"})
        if data:
            n = len(data.get("data", []))
            print(f"   {n} series")
            guardar(f"series_{sitecode}_{slug}.json", data)
            series_por_estacion[sitecode] = data.get("data", [])
            resumen["pasos"].append({"paso": f"series_{sitecode}", "url": url,
                                      "ok": True, "cantidad": n})
        else:
            resumen["pasos"].append({"paso": f"series_{sitecode}", "url": url, "ok": False})
        time.sleep(1)

    # ---------- 4. Muestra de datos + profundidad historica ----------
    # Se pide un rango bien amplio (desde 2000) para ver hasta donde
    # responde de verdad la API, y ademas una muestra reciente para
    # chequear la granularidad temporal (horaria? diaria?).
    print("\n== Muestra de datos y profundidad historica ==")
    hoy = datetime.utcnow().date()
    hace_10_dias = hoy - timedelta(days=10)

    for sitecode, slug in ESTACIONES_INTERES.items():
        series = series_por_estacion.get(sitecode, [])
        if not series:
            print(f"-- sitecode={sitecode} ({slug}): sin series, se omite")
            continue
        # usamos el primer varId de altura hidrometrica que aparezca (varId suele ser 2)
        var_id = series[0].get("varId")
        print(f"-- sitecode={sitecode} ({slug}), varId={var_id}")

        # 4a. Muestra reciente (10 dias) para ver granularidad
        data_reciente, url_r = get_json("datos", {
            "timeStart": hace_10_dias.isoformat(),
            "timeEnd": hoy.isoformat(),
            "siteCode": sitecode,
            "varId": var_id,
            "format": "json",
        })
        if data_reciente:
            n = len(data_reciente.get("data", []))
            print(f"   ultimos 10 dias: {n} registros")
            guardar(f"datos_recientes_{sitecode}_{slug}.json", data_reciente)
            resumen["pasos"].append({"paso": f"datos_recientes_{sitecode}", "url": url_r,
                                      "ok": True, "cantidad": n})
        else:
            resumen["pasos"].append({"paso": f"datos_recientes_{sitecode}", "url": url_r, "ok": False})
        time.sleep(1)

        # 4b. Rango amplio desde 2000 para ver profundidad historica real
        data_historico, url_h = get_json("datos", {
            "timeStart": "2000-01-01",
            "timeEnd": hoy.isoformat(),
            "siteCode": sitecode,
            "varId": var_id,
            "format": "json",
        })
        if data_historico:
            regs = data_historico.get("data", [])
            n = len(regs)
            primera = regs[0].get("timestart") if regs else None
            ultima = regs[-1].get("timestart") if regs else None
            print(f"   historico 2000->hoy: {n} registros, {primera} -> {ultima}")
            # no guardamos el historico completo (puede ser pesado), solo metadata + primeros/ultimos
            resumen["pasos"].append({
                "paso": f"datos_historico_{sitecode}", "url": url_h, "ok": True,
                "cantidad": n, "primer_registro": primera, "ultimo_registro": ultima,
            })
            guardar(f"historico_meta_{sitecode}_{slug}.json", {
                "cantidad": n, "primer_registro": primera, "ultimo_registro": ultima,
                "primeros_5": regs[:5], "ultimos_5": regs[-5:],
            })
        else:
            resumen["pasos"].append({"paso": f"datos_historico_{sitecode}", "url": url_h, "ok": False})
        time.sleep(1)

    guardar("_resumen_discovery.json", resumen)
    print("\nListo. Revisar data/discovery/_resumen_discovery.json para el panorama general.")


if __name__ == "__main__":
    main()
