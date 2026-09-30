"""
Compara el pronostico del INA contra lo realmente observado, usando los
snapshots que acumula capturar_prono_ina.py en data/historico_prono/*.json
(no depende de all=true, que devuelve 0 corridas) y los CSV observados de
data/series/<slug>.csv (los baja sincronizar_ina.py).

Para cada snapshot (una corrida por dia) y cada estacion, cruza cada punto
pronosticado contra el observado mas cercano en el tiempo y calcula el error
(observado - pronosticado_central), ademas del horizonte (lead) en dias.

Salida: data/comparacion_prono/comparacion.json
  - mismo esquema que v3 (estaciones -> slug -> n_comparaciones, mae, rmse,
    comparaciones) + por_lead (MAE/RMSE por dias de anticipacion) +
    resumen de diagnostico (cuantos snapshots hay, rango de fechas).

No hace pedidos de red: todo sale de archivos del repo.
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

# Solo se cargan observaciones recientes (los CSV llegan hasta 1884).
VENTANA_OBS_DIAS = 120
# Tolerancia para emparejar un punto pronosticado con un observado.
# Series diarias (p.ej. Rosario 07:00) -> 12h; horarias -> entran igual.
TOLERANCIA_HORAS = 12
MAX_COMPARACIONES_EN_JSON = 500  # las mas recientes, para no inflar el repo

ESTACIONES = {
    19: "corrientes", 20: "barranqueras", 23: "goya", 29: "parana_ciudad",
    30: "santa_fe", 31: "diamante", 32: "victoria", 33: "san_lorenzo_san_martin",
    34: "rosario", 35: "villa_constitucion", 36: "san_nicolas", 37: "ramallo",
    38: "san_pedro", 39: "baradero", 41: "campana", 45: "ibicuy",
    47: "martin_garcia", 52: "san_fernando", 85: "buenos_aires", 86: "la_plata",
}
SITECODE_POR_SLUG = {slug: sc for sc, slug in ESTACIONES.items()}


def parse_ts(s):
    """ISO con o sin offset/'Z' -> datetime naive (se descarta el offset;
    INA y el capturador usan la misma convencion en ambos lados)."""
    if not s:
        return None
    try:
        s = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(s)[:19], fmt)
            except ValueError:
                continue
    return None


def cargar_observado(slug, desde):
    """CSV -> lista ordenada [(datetime, valor)] desde 'desde'."""
    path = SERIES_DIR / f"{slug}.csv"
    if not path.exists():
        return []
    obs = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = parse_ts(row.get("timestart"))
            if ts is None or ts < desde:
                continue
            try:
                obs.append((ts, float(row["valor"])))
            except (ValueError, TypeError, KeyError):
                continue
    obs.sort(key=lambda x: x[0])
    return obs


def observado_mas_cercano(obs_ts, obs_val, ts, tol):
    """Busqueda binaria del observado mas cercano a ts dentro de tol."""
    import bisect
    i = bisect.bisect_left(obs_ts, ts)
    mejor = None
    for j in (i - 1, i):
        if 0 <= j < len(obs_ts):
            d = abs((obs_ts[j] - ts).total_seconds())
            if d <= tol.total_seconds() and (mejor is None or d < mejor[0]):
                mejor = (d, obs_ts[j], obs_val[j])
    return mejor


def cargar_snapshots():
    """Devuelve lista de (capturado_en datetime, dict snapshot) ordenada."""
    snaps = []
    if not HIST_DIR.exists():
        return snaps
    for p in sorted(HIST_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] no se pudo leer {p.name}: {e}")
            continue
        cap = parse_ts(d.get("capturado_en")) or parse_ts(p.stem[:10])
        if cap is None:
            continue
        snaps.append((cap, d))
    snaps.sort(key=lambda x: x[0])
    return snaps


def stats(errores):
    n = len(errores)
    if not n:
        return None
    mae = sum(abs(e) for e in errores) / n
    rmse = (sum(e * e for e in errores) / n) ** 0.5
    sesgo = sum(errores) / n
    return {"n": n, "mae": round(mae, 4), "rmse": round(rmse, 4), "sesgo": round(sesgo, 4)}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ahora = datetime.utcnow()
    desde_obs = ahora - timedelta(days=VENTANA_OBS_DIAS)
    tol = timedelta(hours=TOLERANCIA_HORAS)

    snaps = cargar_snapshots()
    print(f"== {len(snaps)} snapshots en {HIST_DIR} ==")

    resultado = {
        "generado_en": ahora.isoformat() + "Z",
        "metodo": "v4: snapshots de data/historico_prono vs observado en data/series",
        "n_snapshots": len(snaps),
        "rango_snapshots": [snaps[0][0].isoformat(), snaps[-1][0].isoformat()] if snaps else None,
        "tolerancia_horas": TOLERANCIA_HORAS,
        "estaciones": {},
    }

    if not snaps:
        for slug, sc in SITECODE_POR_SLUG.items():
            resultado["estaciones"][slug] = {
                "sitecode": sc, "n_comparaciones": 0,
                "nota": "no hay snapshots en data/historico_prono/ todavia",
            }
        (OUT_DIR / "comparacion.json").write_text(
            json.dumps(resultado, indent=2, ensure_ascii=False))
        print("Sin snapshots: nada que comparar.")
        return

    for slug, sitecode in SITECODE_POR_SLUG.items():
        obs = cargar_observado(slug, desde_obs)
        if not obs:
            resultado["estaciones"][slug] = {
                "sitecode": sitecode, "n_comparaciones": 0,
                "nota": "sin observaciones recientes en data/series/%s.csv" % slug,
            }
            continue
        obs_ts = [o[0] for o in obs]
        obs_val = [o[1] for o in obs]

        comparaciones = []
        snaps_con_estacion = 0
        for cap, snap in snaps:
            est = (snap.get("estaciones") or {}).get(slug)
            if not est:
                continue
            snaps_con_estacion += 1
            corrida = est.get("corrida_forecastdate")
            for p in est.get("serie", []):
                ts = parse_ts(p.get("timestart"))
                central = p.get("central")
                if ts is None or central is None:
                    continue
                # solo puntos ya ocurridos y con observado disponible
                if ts > obs_ts[-1] + tol:
                    continue
                m = observado_mas_cercano(obs_ts, obs_val, ts, tol)
                if not m:
                    continue
                lead_dias = round((ts - cap).total_seconds() / 86400.0, 2)
                if lead_dias < -1:  # pronostico "hacia atras": se ignora
                    continue
                comparaciones.append({
                    "captura": cap.isoformat(),
                    "corrida": corrida,
                    "timestart": ts.isoformat(),
                    "observado": m[2],
                    "pronosticado_central": central,
                    "pronosticado_min": p.get("min"),
                    "pronosticado_max": p.get("max"),
                    "lead_dias": lead_dias,
                    "error": round(m[2] - central, 4),
                })

        if not comparaciones:
            resultado["estaciones"][slug] = {
                "sitecode": sitecode, "n_comparaciones": 0,
                "n_snapshots_con_estacion": snaps_con_estacion,
                "n_observaciones_locales": len(obs),
                "ultimo_observado": obs_ts[-1].isoformat(),
                "nota": ("0 comparaciones: los puntos pronosticados todavia no tienen "
                         "observado (pronostico a futuro) o no hay snapshots de esta estacion"),
            }
            print(f"-- {slug}: 0 comparaciones (snapshots={snaps_con_estacion}, obs={len(obs)})")
            continue

        por_lead = defaultdict(list)
        for c in comparaciones:
            por_lead[str(max(0, int(round(c["lead_dias"]))))].append(c["error"])

        comparaciones.sort(key=lambda c: c["timestart"])
        total = stats([c["error"] for c in comparaciones])
        resultado["estaciones"][slug] = {
            "sitecode": sitecode,
            "n_comparaciones": len(comparaciones),
            "n_snapshots_con_estacion": snaps_con_estacion,
            "mae": total["mae"], "rmse": total["rmse"], "sesgo": total["sesgo"],
            "por_lead_dias": {k: stats(v) for k, v in sorted(por_lead.items(), key=lambda kv: int(kv[0]))},
            "comparaciones_truncadas": len(comparaciones) > MAX_COMPARACIONES_EN_JSON,
            "comparaciones": comparaciones[-MAX_COMPARACIONES_EN_JSON:],
        }
        print(f"-- {slug}: {len(comparaciones)} comparaciones, MAE={total['mae']} RMSE={total['rmse']}")

    (OUT_DIR / "comparacion.json").write_text(
        json.dumps(resultado, indent=2, ensure_ascii=False))
    print("\nListo. Ver", OUT_DIR / "comparacion.json")


if __name__ == "__main__":
    sys.exit(main())
