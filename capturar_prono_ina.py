"""
Captura el pronostico ACTUAL (ultima corrida) del INA para las 13
estaciones del Paraná que tienen modelo propio, dos veces:

1. Guarda un snapshot fechado en data/historico_prono/<fecha>_<hora>.json
   SOLO con las estaciones que tienen una corrida nueva (corid) respecto de
   lo ya guardado; si no hay ninguna corrida nueva no escribe nada. Asi se
   puede correr seguido (el workflow lo dispara cada ~30 min) sin duplicar
   corridas y se registra la hora real en que cada una estuvo publicada
   (de aca sale la historia para el backtest de precision).

2. Sobreescribe data/snapshot_actual.json con el pronostico mas fresco
   de cada estacion (para armar el HTML de hoy sin esperar nada).

Usa "&" en vez de "?" para query params (bug de la API, ver
descubrir_ina.py / sincronizar_ina.py).
"""

import json
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

BASE = "https://alerta.ina.gob.ar/pub/datos"
DATA_DIR = Path("data")
HIST_DIR = DATA_DIR / "historico_prono"
HIST_DIR.mkdir(parents=True, exist_ok=True)

# sitecode -> slug, mismas 19 de siempre (se descartan las que no
# tienen ninguna serie de prono al armar el snapshot)
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
            req = urllib.request.Request(url, headers={"User-Agent": "capturar_prono_ina/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()), url, None
        except Exception as e:
            ultimo_error = f"{type(e).__name__}: {e}"
            time.sleep(pausa)
    print(f"   [ERROR] {url} -> {ultimo_error}")
    return None, url, ultimo_error


def elegir_mejor_serie(propias):
    """De las series de prono de una estacion, preferir varid=2 (modelo
    principal) y quedarse con la de forecastdate mas reciente."""
    candidatas = [s for s in propias if s.get("varid") == 2] or propias
    return max(candidatas, key=lambda s: s.get("forecastdate") or "")


def _clave_corrida(entrada):
    """Identifica una corrida: corid, o la fecha de la corrida si no hay corid."""
    return entrada.get("corid") if entrada.get("corid") is not None else entrada.get("corrida_forecastdate")


def corridas_vistas():
    """{(slug, clave)} de todo lo ya guardado en historico_prono/."""
    vistas = set()
    for p in sorted(HIST_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for slug, e in (d.get("estaciones") or {}).items():
            vistas.add((slug, _clave_corrida(e)))
    return vistas


def solo_corridas_nuevas(estaciones, vistas):
    return {slug: e for slug, e in estaciones.items() if (slug, _clave_corrida(e)) not in vistas}


def main():
    ahora = datetime.utcnow()
    hoy = ahora.date()
    en_14_dias = hoy + timedelta(days=14)

    print("== Trayendo la ultima corrida de cada estacion ==")
    todas, url, err = get_json("seriesProno", {"format": "json"})
    if not todas:
        print(f"[ERROR FATAL] {err}")
        return
    series = todas.get("data", [])
    print(f"   series de prono en la base: {len(series)}")

    snapshot = {"generado_en": ahora.isoformat() + "Z", "estaciones": {}}
    historico_del_dia = {"capturado_en": ahora.isoformat() + "Z", "estaciones": {}}

    for sitecode, slug in ESTACIONES.items():
        propias = [s for s in series if s.get("sitecode") == sitecode]
        if not propias:
            continue

        serie = elegir_mejor_serie(propias)
        series_id = serie.get("seriesid")
        cal_id = serie.get("calid")
        forecastdate = serie.get("forecastdate")
        corid = serie.get("corid")

        print(f"-- {slug}: seriesId={series_id} calId={cal_id} "
              f"modelo={serie.get('cal_model') or serie.get('cal_name')} corrida={forecastdate}")

        data, u, e = get_json("datosProno", {
            "timeStart": hoy.isoformat(),
            "timeEnd": en_14_dias.isoformat(),
            "seriesId": series_id,
            "calId": cal_id,
            "format": "json",
        })
        if not data:
            print(f"   [ERROR trayendo datosProno para {slug}]")
            continue

        preds = data.get("data", [])
        por_hora = {}
        for p in preds:
            por_hora.setdefault(p["timestart"], []).append(p["valor"])

        serie_ordenada = []
        for ts in sorted(por_hora.keys()):
            vals = sorted(por_hora[ts])
            serie_ordenada.append({
                "timestart": ts,
                "min": vals[0],
                "central": vals[len(vals) // 2],
                "max": vals[-1],
            })

        entrada = {
            "sitecode": sitecode,
            "modelo": serie.get("cal_model") or serie.get("cal_name"),
            "corrida_forecastdate": forecastdate,
            "corid": corid,
            "n_puntos": len(serie_ordenada),
            "serie": serie_ordenada,
        }
        snapshot["estaciones"][slug] = entrada
        historico_del_dia["estaciones"][slug] = {
            "corrida_forecastdate": forecastdate,
            "corid": corid,
            "serie": serie_ordenada,
        }
        time.sleep(1)

    nuevas = solo_corridas_nuevas(historico_del_dia["estaciones"], corridas_vistas())
    if not nuevas:
        print("\nSin corridas nuevas respecto de lo ya guardado; no se escribe nada.")
        return
    print(f"\nCorridas nuevas en: {', '.join(sorted(nuevas))}")
    historico_del_dia["estaciones"] = nuevas
    (DATA_DIR / "snapshot_actual.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False)
    )
    nombre_archivo = f"{hoy.isoformat()}_{ahora.strftime('%H%M%S')}.json"
    (HIST_DIR / nombre_archivo).write_text(
        json.dumps(historico_del_dia, indent=2, ensure_ascii=False)
    )
    print(f"\nListo. snapshot_actual.json actualizado, "
          f"historico_prono/{nombre_archivo} guardado.")


if __name__ == "__main__":
    main()
