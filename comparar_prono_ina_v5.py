"""
Compara el pronóstico del INA contra lo observado (v5, corregido).

Entrada (todo del repo, sin red):
  data/historico_prono/*.json   snapshots diarios de capturar_prono_ina.py
  data/series/<slug>.csv        observados (sincronizar_ina.py)
Salida: data/comparacion_prono/comparacion.json

Qué cambia respecto de v4 (v4 inflaba/distorsionaba el error):
  1. El horizonte (lead) se cuenta desde que CORRIÓ el modelo (corrida_forecastdate),
     no desde que lo capturamos. Así un pronóstico de 6 h no se mezcla con uno de 6 días.
  2. Emparejamiento por hora exacta (±30 min). v4 aceptaba ±12 h, y las series diarias
     (una lectura a las 00:00) se comparaban contra puntos pronosticados de otra hora
     del día, metiendo el ciclo de marea como "error" (era el caso de Campana).
  3. Observados con timestamps duplicados: se deja el último.
  4. Se ignoran puntos anteriores a la corrida (no son pronóstico).
  5. Se agregan: persistencia como línea base, MAE sin sesgo (leave-one-snapshot-out,
     mide lo que quedaría si se corrigiera el sesgo fijo de la estación) y
     métricas por franja de horizonte.
  6. La hora de captura sale del nombre del archivo (UTC), que es lo confiable.

Convenciones de hora: nombres de snapshot y corrida_forecastdate en UTC; timestart del
pronóstico y de las series observadas, en la misma convención del INA (se comparan directo).
Error = observado − pronosticado (sesgo positivo = el pronóstico queda bajo).
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("INA_DATA_DIR", "data"))
HIST_DIR = DATA_DIR / "historico_prono"
SERIES_DIR = DATA_DIR / "series"
OUT_DIR = DATA_DIR / "comparacion_prono"

VENTANA_OBS_DIAS = 120
TOLERANCIA_MIN = 30                 # emparejamiento por hora
MAX_COMPARACIONES_EN_JSON = 500
FRANJAS_DIAS = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 10), (10, 400)]   # lead desde la corrida
MIN_N_FRANJA = 5

ESTACIONES = {
    19: "corrientes", 20: "barranqueras", 23: "goya", 29: "parana_ciudad",
    30: "santa_fe", 31: "diamante", 32: "victoria", 33: "san_lorenzo_san_martin",
    34: "rosario", 35: "villa_constitucion", 36: "san_nicolas", 37: "ramallo",
    38: "san_pedro", 39: "baradero", 41: "campana", 45: "ibicuy",
    47: "martin_garcia", 52: "san_fernando", 85: "buenos_aires", 86: "la_plata",
}
SITECODE_POR_SLUG = {slug: sc for sc, slug in ESTACIONES.items()}


def parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                pass
    return None


def cargar_observado(slug, desde):
    """{timestart: valor}; con duplicados se queda el último. Devuelve (ts_ordenados, valores)."""
    path = SERIES_DIR / f"{slug}.csv"
    if not path.exists():
        return [], []
    d = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = parse_ts(row.get("timestart"))
            if ts is None or ts < desde:
                continue
            try:
                d[ts] = float(row["valor"])
            except (ValueError, TypeError, KeyError):
                continue
    ts_ord = sorted(d)
    return ts_ord, [d[t] for t in ts_ord]


def mas_cercano(obs_ts, ts, tol_s):
    import bisect
    i = bisect.bisect_left(obs_ts, ts)
    mejor = None
    for j in (i - 1, i):
        if 0 <= j < len(obs_ts):
            dd = abs((obs_ts[j] - ts).total_seconds())
            if dd <= tol_s and (mejor is None or dd < mejor[0]):
                mejor = (dd, j)
    return mejor[1] if mejor else None


def ultimo_hasta(obs_ts, t):
    import bisect
    i = bisect.bisect_right(obs_ts, t)
    return i - 1 if i > 0 else None


def cargar_snapshots():
    snaps = []
    if not HIST_DIR.exists():
        return snaps
    for p in sorted(HIST_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] no se pudo leer {p.name}: {e}")
            continue
        try:
            cap = datetime.strptime(p.stem, "%Y-%m-%d_%H%M%S")
        except ValueError:
            cap = parse_ts(d.get("capturado_en") or d.get("generado_en"))
        if cap is None:
            continue
        snaps.append((cap, d))
    snaps.sort(key=lambda x: x[0])
    return snaps


def stats(errores):
    n = len(errores)
    if not n:
        return None
    return {"n": n,
            "mae": round(sum(abs(e) for e in errores) / n, 4),
            "rmse": round((sum(e * e for e in errores) / n) ** 0.5, 4),
            "sesgo": round(sum(errores) / n, 4)}


def mae_sin_sesgo(comps):
    """MAE tras restar el sesgo medio calculado SIN el snapshot evaluado (leave-one-snapshot-out)."""
    por = defaultdict(list)
    for c in comps:
        por[c["captura"]].append(c["error"])
    if len(por) < 3:
        return None
    tot, n = sum(sum(v) for v in por.values()), sum(len(v) for v in por.values())
    s = 0.0
    for v in por.values():
        otros_n = n - len(v)
        if otros_n == 0:
            return None
        b = (tot - sum(v)) / otros_n
        s += sum(abs(e - b) for e in v)
    return round(s / n, 4)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ahora = datetime.utcnow()
    desde_obs = ahora - timedelta(days=VENTANA_OBS_DIAS)
    tol_s = TOLERANCIA_MIN * 60

    snaps = cargar_snapshots()
    print(f"== {len(snaps)} snapshots en {HIST_DIR} ==")
    res = {
        "generado_en": ahora.isoformat() + "Z",
        "metodo": ("v5: lead desde la corrida del modelo; emparejamiento por hora exacta (±%d min); "
                   "error = observado − pronosticado" % TOLERANCIA_MIN),
        "n_snapshots": len(snaps),
        "rango_snapshots": [snaps[0][0].isoformat(), snaps[-1][0].isoformat()] if snaps else None,
        "tolerancia_min": TOLERANCIA_MIN,
        "estaciones": {},
    }

    for slug, sitecode in SITECODE_POR_SLUG.items():
        obs_ts, obs_val = cargar_observado(slug, desde_obs)
        if not snaps or not obs_ts:
            res["estaciones"][slug] = {"sitecode": sitecode, "n_comparaciones": 0,
                                       "nota": "sin snapshots u observaciones recientes"}
            continue
        comps, n_snap = [], 0
        for cap, snap in snaps:
            est = (snap.get("estaciones") or {}).get(slug)
            if not est or not est.get("serie"):
                continue
            n_snap += 1
            run = parse_ts(est.get("corrida_forecastdate"))
            if run is None:
                continue
            k = ultimo_hasta(obs_ts, cap - timedelta(hours=1))
            persist = obs_val[k] if k is not None else None
            for p in est["serie"]:
                ts, central = parse_ts(p.get("timestart")), p.get("central")
                if ts is None or central is None or ts < run or ts > obs_ts[-1]:
                    continue
                j = mas_cercano(obs_ts, ts, tol_s)
                if j is None:
                    continue
                comps.append({
                    "captura": cap.isoformat(), "corrida": est.get("corrida_forecastdate"),
                    "timestart": ts.isoformat(), "observado": obs_val[j],
                    "pronosticado_central": central,
                    "pronosticado_min": p.get("min"), "pronosticado_max": p.get("max"),
                    "lead_dias": round((ts - run).total_seconds() / 86400, 2),
                    "error": round(obs_val[j] - central, 4),
                    "error_persistencia": None if persist is None else round(obs_val[j] - persist, 4),
                })
        if not comps:
            res["estaciones"][slug] = {"sitecode": sitecode, "n_comparaciones": 0,
                                       "n_snapshots_con_estacion": n_snap,
                                       "ultimo_observado": obs_ts[-1].isoformat(),
                                       "nota": "0 comparaciones: pronósticos a futuro sin observado todavía"}
            print(f"-- {slug}: 0 comparaciones")
            continue

        total = stats([c["error"] for c in comps])
        pers = stats([c["error_persistencia"] for c in comps if c["error_persistencia"] is not None])
        franjas = {}
        for a, b in FRANJAS_DIAS:
            x = [c for c in comps if a <= c["lead_dias"] < b]
            if len(x) >= MIN_N_FRANJA:
                st = stats([c["error"] for c in x])
                pp = [c["error_persistencia"] for c in x if c["error_persistencia"] is not None]
                st["mae_persistencia"] = stats(pp)["mae"] if pp else None
                franjas[f"{a}-{b}d"] = st
        comps.sort(key=lambda c: c["timestart"])
        res["estaciones"][slug] = {
            "sitecode": sitecode, "n_comparaciones": len(comps), "n_snapshots_con_estacion": n_snap,
            "mae": total["mae"], "rmse": total["rmse"], "sesgo": total["sesgo"],
            "mae_sin_sesgo": mae_sin_sesgo(comps),
            "mae_persistencia": pers["mae"] if pers else None,
            "por_lead_dias": franjas,
            "comparaciones_truncadas": len(comps) > MAX_COMPARACIONES_EN_JSON,
            "comparaciones": comps[-MAX_COMPARACIONES_EN_JSON:],
        }
        print(f"-- {slug}: n={len(comps)} MAE={total['mae']} sesgo={total['sesgo']:+} sin_sesgo={res['estaciones'][slug]['mae_sin_sesgo']}")

    (OUT_DIR / "comparacion.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Listo:", OUT_DIR / "comparacion.json")


if __name__ == "__main__":
    sys.exit(main())
