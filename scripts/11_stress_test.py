"""
Step 11: Stress test.

Re-runs steps 07 (costs only) -> 08 -> 09 -> 10 under alternative settings and records
which streets end up in the connector network each time:

  * named scenarios from config.yaml `stress.scenarios` (equal weights, reversed
    ranking, no standardization, different discounts, ...)
  * `stress.n_random` runs with random weights (each 0.1-1.0), i.e. someone else's
    preferences entirely

The baseline (config.yaml as is) runs LAST, so every analysis table is left exactly as
the baseline produces it.

Two robustness measures per baseline network edge:
  street-level    share of runs in which this exact edge is in the network
  corridor-level  share of runs in which the network has this edge OR a roughly
                  parallel edge within corridors.band_half_width_m (the same band).
                  Different weightings often shift a route one block over; this
                  counts that as the same corridor.

Outputs (schema analysis):
  stress_runs         one row per run: settings, network km, overlap with baseline
  stress_edges        run -> network edges
  stress_edge_freq    edge -> street-level share (all edges seen in any run)
  stress_band_freq    baseline edge -> corridor-level share
  stress_v            map-ready view of the baseline network with both measures

Usage:
  python scripts/11_stress_test.py                 # all runs (can take a while)
  python scripts/11_stress_test.py --only equal_weights reversed_ranking
  python scripts/11_stress_test.py --summary-only  # recompute summaries from stored runs
"""
import argparse
import copy
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
BASE = yaml.safe_load((ROOT / "config.yaml").read_text())
ST = BASE["stress"]
STEPS = [
    ["scripts/07_score_edges.py", "--costs-only"],
    ["scripts/08_route_connectors.py"],
    ["scripts/09_extract_corridors.py"],
    ["scripts/10_connector_network.py"],
]


def get_engine():
    db = BASE["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_runs(only):
    runs = []
    for name, override in (ST.get("scenarios") or {}).items():
        runs.append((name, override))
    rng = random.Random(ST.get("seed", 0))
    factors = ["slope", "traffic", "offstreet", "crossings", "shade", "sidewalk"]
    for i in range(ST.get("n_random", 0)):
        w = {f: round(rng.uniform(0.1, 1.0), 2) for f in factors}
        runs.append((f"random_{i + 1:02d}", {"scoring": {"weights": w}}))
    if only:
        runs = [r for r in runs if r[0] in only]
    runs.append(("baseline", {}))  # last, so tables end in the baseline state
    return runs


def run_pipeline(cfg_path):
    env = dict(os.environ, PDX_CONFIG=str(cfg_path))
    for step in STEPS:
        res = subprocess.run([sys.executable, *step], cwd=ROOT, env=env,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"{step[0]} failed:\n{res.stderr[-2000:]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="SCENARIO")
    parser.add_argument("--summary-only", action="store_true",
                        help="skip the runs; rebuild robustness tables from stored results")
    args = parser.parse_args()

    engine = get_engine()
    if not args.summary_only:
        run_all(engine, args.only)
    summarize(engine)


def run_all(engine, only):
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS analysis.stress_v"))
        conn.execute(text("DROP TABLE IF EXISTS analysis.stress_edges"))
        conn.execute(text("DROP TABLE IF EXISTS analysis.stress_runs"))
        conn.execute(text("CREATE TABLE analysis.stress_edges (run text, edge_id bigint, uses int)"))
        conn.execute(text("""CREATE TABLE analysis.stress_runs (
            run text PRIMARY KEY, settings jsonb, network_km float, pieces int,
            median_detour float, max_detour float, minutes float)"""))

    cfg_dir = ROOT / "data" / "stress"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    runs = build_runs(only)
    print(f"{len(runs)} runs (baseline last)")

    for i, (name, override) in enumerate(runs, 1):
        cfg = deep_merge(BASE, override)
        path = cfg_dir / f"{name}.yaml"
        path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        t0 = time.time()
        print(f"[{i}/{len(runs)}] {name}...", end="", flush=True)
        run_pipeline(path)
        mins = (time.time() - t0) / 60
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO analysis.stress_edges
                SELECT :run, edge_id, uses FROM analysis.connector_network
            """), {"run": name})
            conn.execute(text("""
                INSERT INTO analysis.stress_runs
                SELECT :run, CAST(:settings AS jsonb),
                       (SELECT sum(length_m)/1000 FROM analysis.connector_network),
                       (SELECT count(DISTINCT component) FROM analysis.connector_network),
                       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY detour_ratio)
                          FROM analysis.network_routes),
                       (SELECT max(detour_ratio) FROM analysis.network_routes),
                       :mins
            """), {"run": name, "settings": json.dumps(override), "mins": mins})
        print(f" done ({mins:.1f} min)")


def summarize(engine):
    band = BASE["corridors"]["band_half_width_m"]
    angle = BASE["corridors"]["parallel_max_angle"]
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS analysis.stress_v"))
        conn.execute(text("DROP TABLE IF EXISTS analysis.stress_edge_freq"))
        conn.execute(text("""
            CREATE TABLE analysis.stress_edge_freq AS
            SELECT s.edge_id,
                   count(*) AS runs,
                   count(*)::float / (SELECT count(*) FROM analysis.stress_runs) AS share,
                   bool_or(s.run = 'baseline') AS in_baseline
            FROM analysis.stress_edges s
            GROUP BY s.edge_id
        """))
        conn.execute(text("CREATE UNIQUE INDEX ON analysis.stress_edge_freq (edge_id)"))

        # corridor-level: for each baseline edge, in how many runs does the network
        # contain it or a parallel edge in the same band?
        conn.execute(text("DROP TABLE IF EXISTS analysis.stress_band_freq"))
        conn.execute(text("""
            CREATE TABLE analysis.stress_band_freq AS
            WITH base AS (
                SELECT e.id AS edge_id, e.geom,
                       mod(degrees(ST_Azimuth(ST_StartPoint(e.geom), ST_EndPoint(e.geom)))::numeric, 180)::float AS dir
                FROM analysis.stress_edges s
                JOIN network.edges e ON e.id = s.edge_id
                WHERE s.run = 'baseline'
                  AND NOT ST_Equals(ST_StartPoint(e.geom), ST_EndPoint(e.geom))
            ),
            other AS (
                SELECT s.run, e.id AS edge_id, e.geom,
                       mod(degrees(ST_Azimuth(ST_StartPoint(e.geom), ST_EndPoint(e.geom)))::numeric, 180)::float AS dir
                FROM analysis.stress_edges s
                JOIN network.edges e ON e.id = s.edge_id
                WHERE NOT ST_Equals(ST_StartPoint(e.geom), ST_EndPoint(e.geom))
            )
            SELECT b.edge_id,
                   count(DISTINCT o.run) AS band_runs,
                   count(DISTINCT o.run)::float / (SELECT count(*) FROM analysis.stress_runs) AS band_share
            FROM base b
            JOIN other o
              ON o.edge_id = b.edge_id
              OR (ST_DWithin(b.geom, o.geom, :band)
                  AND ST_DWithin(b.geom, ST_LineInterpolatePoint(o.geom, 0.5), :band)
                  AND least(abs(b.dir - o.dir), 180 - abs(b.dir - o.dir)) <= :angle)
            GROUP BY b.edge_id
        """), {"band": band, "angle": angle})
        conn.execute(text("CREATE UNIQUE INDEX ON analysis.stress_band_freq (edge_id)"))

        conn.execute(text("""
            CREATE VIEW analysis.stress_v AS
            SELECT f.edge_id AS id,
                   round(f.share::numeric, 2)      AS street_share,
                   round(b.band_share::numeric, 2) AS corridor_share,
                   CASE WHEN f.share >= 0.8 THEN 'robust'
                        WHEN f.share >= 0.5 THEN 'likely'
                        ELSE 'sensitive' END AS street_robustness,
                   CASE WHEN b.band_share >= 0.8 THEN 'robust'
                        WHEN b.band_share >= 0.5 THEN 'likely'
                        ELSE 'sensitive' END AS corridor_robustness,
                   e.name, e.highway, e.length_m, e.geom
            FROM analysis.stress_edge_freq f
            JOIN analysis.stress_band_freq b USING (edge_id)
            JOIN network.edges e ON e.id = f.edge_id
            WHERE f.in_baseline
        """))

    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    print("\nRuns (overlap = share of the baseline network's length also in this run):")
    print(q("""
        SELECT r.run, round(r.network_km::numeric, 1) AS km, r.pieces,
               round(r.median_detour::numeric, 2) AS med_detour,
               round(r.max_detour::numeric, 2) AS max_detour,
               round((100 * (SELECT sum(e.length_m) FROM analysis.stress_edges a
                             JOIN analysis.stress_edges b ON b.edge_id = a.edge_id AND b.run = 'baseline'
                             JOIN network.edges e ON e.id = a.edge_id
                             WHERE a.run = r.run)
                     / (SELECT sum(e.length_m) FROM analysis.stress_edges b
                        JOIN network.edges e ON e.id = b.edge_id WHERE b.run = 'baseline'))::numeric)
                     AS pct_of_baseline
        FROM analysis.stress_runs r ORDER BY r.run
    """).to_string(index=False))

    print("\nBaseline network by robustness (share of its length):")
    print(q("""
        WITH classes AS (
            SELECT length_m, 'street' AS level, street_robustness AS cls FROM analysis.stress_v
            UNION ALL
            SELECT length_m, 'corridor', corridor_robustness FROM analysis.stress_v
        )
        SELECT cls AS robustness,
               round((sum(length_m) FILTER (WHERE level = 'street')/1000)::numeric, 1)   AS street_km,
               round((100 * sum(length_m) FILTER (WHERE level = 'street')
                      / (SELECT sum(length_m) FROM analysis.stress_v))::numeric)        AS street_pct,
               round((sum(length_m) FILTER (WHERE level = 'corridor')/1000)::numeric, 1) AS corridor_km,
               round((100 * sum(length_m) FILTER (WHERE level = 'corridor')
                      / (SELECT sum(length_m) FROM analysis.stress_v))::numeric)        AS corridor_pct
        FROM classes
        GROUP BY cls
        ORDER BY CASE cls WHEN 'robust' THEN 1 WHEN 'likely' THEN 2 ELSE 3 END
    """).fillna(0).to_string(index=False))

    print("\nMost robust named streets (km of network in 80+ pct of runs):")
    print(q("""
        SELECT coalesce(e.name, '(' || e.highway || ')') AS street,
               round((sum(e.length_m)/1000)::numeric, 2) AS robust_km
        FROM analysis.stress_edge_freq f JOIN network.edges e ON e.id = f.edge_id
        WHERE f.share >= 0.8 AND e.name IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).to_string(index=False))

    print("\nMost robust corridors by street (km in the same band in 80+ pct of runs):")
    print(q("""
        SELECT coalesce(name, '(' || highway || ')') AS street,
               round((sum(length_m)/1000)::numeric, 2) AS robust_km
        FROM analysis.stress_v
        WHERE corridor_share >= 0.8 AND name IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).to_string(index=False))


if __name__ == "__main__":
    main()
