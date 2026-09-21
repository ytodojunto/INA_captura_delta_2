"""
Compara el pronostico historico del INA (datosProno, corridas pasadas)
contra lo que realmente paso, usando los CSV ya sincronizados por
sincronizar_ina.py (data/series/*.csv).

Para cada estacion con pronostico propio: trae todas las corridas
emitidas en la ventana de backtest (parametro all=true de seriesProno),
muestrea aprox 1 corrida por semana para no reventar de pedidos, pide
el pronostico completo de cada corrida elegida (via corId, que anula
calId), y cruza cada valor pronosticado contra el observado real en
esa misma fecha/hora.

Guarda todo en data/comparacion_prono/comparacion.json - de ahi se arma
despues el HTML con tabla + grafico.
"""

import csv
import json
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

BASE = "https://alerta.ina.gob.ar/pub/datos"
DATA_DIR = Path("data")
SERIES_DIR = DATA_DIR / "series"
OUT_DIR = DATA_DIR / "comparacion_prono"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DIAS_BACKTEST = 60
DIAS_ENTRE_CORRIDAS = 6  # muestreo: no tomar corridas mas seguido que esto

# mismos sitecodes/slugs que sincronizar_ina.py
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
            req = urllib.request.Request(url, headers={"User-Agent": "comparar_prono_ina/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read()), url, None
        except Exception as e:
            ultimo_error = f"{type(e).__name__}: {e}"
            time.sleep(pausa)
    print(f"   [ERROR] {url} -> {ultimo_error}")
    return None, url, ultimo_error


def cargar_observado(slug):
    """CSV ya sincronizado -> dict timestart(ISO string) -> valor(float)."""
    path = SERIES_DIR / f"{slug}.csv"
    obs = {}
    if not path.exists():
        return obs
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                obs[row["timestart"]] = float(row["valor"])
            except (ValueError, TypeError, KeyError):
                continue
    return obs


def elegir_corridas_muestreadas(corridas):
    """De todas las corridas de una estacion, se queda con ~1 cada
    DIAS_ENTRE_CORRIDAS dias (por corid, para no repetir)."""
    ordenadas = sorted(corridas, key=lambda c: c.get("forecastdate") or "")
    elegidas = []
    ultima_fecha = None
    vistos = set()
    for c in ordenadas:
        corid = c.get("corid")
        if corid in vistos:
            continue
        fd = c.get("forecastdate")
        if not fd:
            continue
        fd_dt = datetime.fromisoformat(fd)
        if ultima_fecha is None or (fd_dt - ultima_fecha).days >= DIAS_ENTRE_CORRIDAS:
            elegidas.append(c)
            vistos.add(corid)
            ultima_fecha = fd_dt
    return elegidas


def main():
    hoy = datetime.utcnow().date()
    desde = hoy - timedelta(days=DIAS_BACKTEST)
    resultado = {
        "generado_en": datetime.utcnow().isoformat() + "Z",
        "ventana_backtest_dias": DIAS_BACKTEST,
        "estaciones": {},
    }

    print(f"== Trayendo todas las corridas emitidas entre {desde} y {hoy} ==")
    todas, url, err = get_json("seriesProno", {
        "all": "true",
        "forecastTimeStart": desde.isoformat(),
        "forecastTimeEnd": hoy.isoformat(),
        "format": "json",
    })
    if not todas:
        print(f"[ERROR FATAL] no se pudo traer seriesProno: {err}")
        (OUT_DIR / "comparacion.json").write_text(json.dumps(
            {"error": err, "url": url}, indent=2, ensure_ascii=False))
        return

    corridas_todas = todas.get("data", [])
    print(f"   corridas totales en la ventana (todas las estaciones): {len(corridas_todas)}")

    for sitecode, slug in ESTACIONES.items():
        propias = [c for c in corridas_todas if c.get("sitecode") == sitecode]
        if not propias:
            continue

        obs = cargar_observado(slug)
        if not obs:
            print(f"-- {slug}: sin CSV observado todavia, se omite")
            continue

        elegidas = elegir_corridas_muestreadas(propias)
        print(f"-- {slug}: {len(propias)} corridas en la ventana, "
              f"probando con {len(elegidas)} muestreadas")

        comparaciones = []
        for c in elegidas:
            corid = c.get("corid")
            data, url, err = get_json("datosProno", {
                "corId": corid,
                "timeStart": desde.isoformat(),
                "timeEnd": (hoy + timedelta(days=14)).isoformat(),
                "format": "json",
            })
            if not data:
                continue
            preds = data.get("data", [])

            por_hora = {}
            for p in preds:
                por_hora.setdefault(p["timestart"], []).append(p["valor"])

            for ts, valores in por_hora.items():
                if ts not in obs:
                    continue
                valores_ord = sorted(valores)
                central = valores_ord[len(valores_ord) // 2]
                comparaciones.append({
                    "corrida": c.get("forecastdate"),
                    "timestart": ts,
                    "observado": obs[ts],
                    "pronosticado_central": central,
                    "pronosticado_min": valores_ord[0],
                    "pronosticado_max": valores_ord[-1],
                    "error": round(obs[ts] - central, 4),
                })
            time.sleep(1)

        if comparaciones:
            errores = [c["error"] for c in comparaciones]
            mae = sum(abs(e) for e in errores) / len(errores)
            rmse = (sum(e ** 2 for e in errores) / len(errores)) ** 0.5
            resultado["estaciones"][slug] = {
                "sitecode": sitecode,
                "n_comparaciones": len(comparaciones),
                "n_corridas_usadas": len(elegidas),
                "mae": round(mae, 4),
                "rmse": round(rmse, 4),
                "comparaciones": sorted(comparaciones, key=lambda x: x["timestart"]),
            }
            print(f"   {slug}: {len(comparaciones)} comparaciones, MAE={mae:.3f} RMSE={rmse:.3f}")
        else:
            print(f"   {slug}: sin comparaciones posibles (fechas de pronostico sin match en lo observado)")

    (OUT_DIR / "comparacion.json").write_text(
        json.dumps(resultado, indent=2, ensure_ascii=False)
    )
    print("\nListo. Ver data/comparacion_prono/comparacion.json")


if __name__ == "__main__":
    main()
