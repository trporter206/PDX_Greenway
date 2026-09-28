"""
Step 07: Turn edge attributes and metrics into penalties, impedance, and routing cost.

Stage A (weight-independent) -> network.edge_penalties
  p_slope     grade_avg: 0 at <= flat_grade, 1 at >= full_grade; stairs get steps_penalty
  p_traffic   worse of speed (free..full mph) and TSP traffic class; off-street = 0
  p_shade     1 - canopy_frac / full_canopy_frac (clipped 0..1)
  p_sidewalk  PBOT completeness -> explicit OSM sidewalk tag -> alleys/shared streets
              get shared_street_penalty -> sidewalk polygons either side (inside city)
              -> unknown_penalty; off-street = 0
  crossings   half a crossing at each end of a non-major edge that meets a major street
              (a route across an arterial pays two halves = one crossing)

Stage B (uses weights) -> network.edge_costs
  impedance = 1 + sum(weight * penalty), with a discount for off-street edges.
  With `standardize`, each per-length weight is scaled by (mean spread / factor spread),
  spread = length-weighted std of the penalty, so influence follows the ranking.
  cost      = length_m * impedance + crossings * crossing_cost_m * crossings_weight

Usage:
  python scripts/07_score_edges.py              # both stages
  python scripts/07_score_edges.py --costs-only # re-weight only (fast; used by step 08)

All parameters live in config.yaml under `scoring`.
"""
import argparse
import json
import os
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
# PDX_CONFIG lets step 11 (stress test) run this step with an alternative config
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
S = CFG["scoring"]


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


PENALTIES_SQL = """
DROP TABLE IF EXISTS network.edge_penalties CASCADE;
CREATE TABLE network.edge_penalties AS
WITH base AS (
    SELECT e.id AS edge_id, e.source, e.target, e.length_m, e.geom,
           a.offstreet, a.highway, a.speed_mph, a.tsp_traffic,
           a.sidewalk_city, a.sidewalk_osm,
           m.grade_avg, m.is_steps, m.canopy_frac,
           regexp_replace(split_part(a.highway, ';', 1), '_link$', '') AS hw1
    FROM network.edges e
    JOIN network.edge_attributes a ON a.edge_id = e.id
    JOIN network.edge_metrics    m ON m.edge_id = e.id
    WHERE e.in_main
),
filled AS (
    SELECT *,
        CASE WHEN offstreet THEN NULL
             ELSE coalesce(speed_mph, (CAST(:default_speed AS jsonb) ->> hw1)::int, 20)
        END AS speed_eff
    FROM base
)
SELECT
    edge_id, source, target, length_m, offstreet, is_steps, highway, hw1,
    tsp_traffic, speed_eff, sidewalk_city, sidewalk_osm,
    CASE WHEN is_steps THEN CAST(:steps_penalty AS float)
         ELSE least(greatest((coalesce(grade_avg, 0) - :flat) / (:full - :flat), 0), 1)
    END AS p_slope,
    CASE WHEN offstreet THEN 0.0
         ELSE greatest(
            least(greatest((speed_eff - :sfree) / (:sfull - :sfree), 0), 1),
            coalesce((CAST(:class_pen AS jsonb) ->> tsp_traffic)::float,
                     (CAST(:hw_pen AS jsonb) ->> hw1)::float, 0))
    END AS p_traffic,
    least(greatest(1 - coalesce(canopy_frac, 0) / :full_canopy, 0), 1) AS p_shade,
    CAST(NULL AS float) AS p_sidewalk,
    CAST(NULL AS text)  AS sidewalk_source,
    CAST(NULL AS boolean) AS is_major,
    CAST(0 AS float)    AS crossings,
    geom
FROM filled;

CREATE UNIQUE INDEX ON network.edge_penalties (edge_id);
CREATE INDEX ON network.edge_penalties USING GIST (geom);
"""

SIDEWALK_SQL = """
-- 1. off-street paths and PBOT completeness
UPDATE network.edge_penalties SET p_sidewalk = 0, sidewalk_source = 'offstreet' WHERE offstreet;
UPDATE network.edge_penalties
SET p_sidewalk = (CAST(:sw_pen AS jsonb) ->> sidewalk_city)::float, sidewalk_source = 'city'
WHERE p_sidewalk IS NULL AND sidewalk_city IS NOT NULL;

-- 2. explicit OSM sidewalk tags (e.g. sidewalk=separate means sidewalks are mapped)
UPDATE network.edge_penalties
SET p_sidewalk = CASE
        WHEN sidewalk_osm ~ '(both|separate)' THEN 0.0
        WHEN sidewalk_osm ~ '(left|right)'    THEN 0.5
        WHEN sidewalk_osm ~ '(no|none)'       THEN 1.0
    END,
    sidewalk_source = 'osm'
WHERE p_sidewalk IS NULL AND sidewalk_osm IS NOT NULL
  AND sidewalk_osm ~ '(both|separate|left|right|no|none)';

-- 3a. alleys and other shared streets
UPDATE network.edge_penalties SET p_sidewalk = :shared, sidewalk_source = 'shared_street'
WHERE p_sidewalk IS NULL AND hw1 = ANY(CAST(:shared_types AS text[]));

-- 3. sidewalk polygons on each side (inside city limits only; no polygon data outside)
WITH todo AS (
    SELECT p.edge_id, p.geom
    FROM network.edge_penalties p, raw.city_city_boundary_official b
    WHERE p.p_sidewalk IS NULL AND p.length_m >= 5 AND ST_Intersects(p.geom, b.geometry)
),
sides AS (
    SELECT edge_id, side, ST_LineMerge(ST_OffsetCurve(geom, side * :off)) AS g
    FROM todo, (VALUES (1), (-1)) v(side)
),
pts AS (
    SELECT edge_id, side,
           (ST_DumpPoints(ST_LineInterpolatePoints(g, least(1.0, 10.0 / greatest(ST_Length(g), 10.0))))).geom AS pt
    FROM sides
    WHERE GeometryType(g) = 'LINESTRING' AND ST_Length(g) > 0
),
cov AS (
    SELECT edge_id, side,
           avg(CASE WHEN EXISTS (SELECT 1 FROM raw.city_sidewalks s
                                  WHERE ST_DWithin(s.geometry, pts.pt, :search))
                    THEN 1.0 ELSE 0.0 END) AS c
    FROM pts GROUP BY edge_id, side
),
edge_cov AS (
    SELECT edge_id, sum(c) / 2.0 AS coverage FROM cov GROUP BY edge_id
)
UPDATE network.edge_penalties p
SET p_sidewalk = 1 - ec.coverage, sidewalk_source = 'polygons'
FROM edge_cov ec
WHERE p.edge_id = ec.edge_id;

-- 4. nothing known
UPDATE network.edge_penalties SET p_sidewalk = :unknown, sidewalk_source = 'unknown'
WHERE p_sidewalk IS NULL;
"""

CROSSINGS_SQL = """
UPDATE network.edge_penalties
SET is_major = NOT offstreet AND coalesce(
        tsp_traffic = ANY(CAST(:major_classes AS text[]))
     OR speed_eff >= :major_speed
     OR (coalesce(tsp_traffic, '') NOT IN (SELECT jsonb_object_keys(CAST(:class_pen AS jsonb)))
         AND split_part(highway, ';', 1) = ANY(CAST(:major_highways AS text[]))),
    false);

WITH major_nodes AS (
    SELECT source AS node FROM network.edge_penalties WHERE is_major
    UNION
    SELECT target FROM network.edge_penalties WHERE is_major
)
UPDATE network.edge_penalties p
SET crossings = 0.5 * (p.source IN (SELECT node FROM major_nodes))::int
              + 0.5 * (p.target IN (SELECT node FROM major_nodes))::int
WHERE NOT p.is_major;
"""

COSTS_SQL = """
-- CASCADE also drops analysis.corridor_use_v, which step 08 recreates on its next run
DROP VIEW IF EXISTS network.edges_scored_v CASCADE;
DROP TABLE IF EXISTS network.edge_costs CASCADE;
CREATE TABLE network.edge_costs AS
SELECT
    edge_id, source, target, length_m,
    imp AS impedance,
    length_m * imp + crossings * :ccost * :wc AS cost
FROM (
    SELECT *,
        (1 + :ws * p_slope + :wt * p_traffic + :wsh * p_shade + :wsw * p_sidewalk)
        * CASE WHEN offstreet THEN (1 - :wo * :disc) ELSE 1 END AS imp
    FROM network.edge_penalties
) x;
ALTER TABLE network.edge_costs ADD COLUMN reverse_cost float;
UPDATE network.edge_costs SET reverse_cost = cost;
CREATE UNIQUE INDEX ON network.edge_costs (edge_id);

CREATE VIEW network.edges_scored_v AS
SELECT p.edge_id AS id, p.highway, e.name, p.length_m, p.offstreet, p.is_steps, p.is_major,
       p.tsp_traffic, p.speed_eff,
       round(p.p_slope::numeric, 2)    AS p_slope,
       round(p.p_traffic::numeric, 2)  AS p_traffic,
       round(p.p_shade::numeric, 2)    AS p_shade,
       round(p.p_sidewalk::numeric, 2) AS p_sidewalk,
       p.sidewalk_source, p.crossings,
       round(c.impedance::numeric, 2)  AS impedance,
       round(c.cost::numeric, 1)       AS cost,
       p.geom
FROM network.edge_penalties p
JOIN network.edge_costs c USING (edge_id)
JOIN network.edges e ON e.id = p.edge_id;
"""


def run(engine, sql, params):
    with engine.begin() as conn:
        for stmt in [s.strip() for s in sql.split(";\n") if s.strip()]:
            conn.execute(text(stmt), params)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--costs-only", action="store_true",
                        help="skip penalties; recompute costs from current weights")
    args = parser.parse_args()
    engine = get_engine()
    tr, cr = S["traffic"], S["crossings"]

    if not args.costs_only:
        print("Penalties...")
        run(engine, PENALTIES_SQL, {
            "default_speed": json.dumps(tr["default_speed_mph"]),
            "steps_penalty": S["slope"]["steps_penalty"],
            "flat": S["slope"]["flat_grade"], "full": S["slope"]["full_grade"],
            "sfree": float(tr["speed_free_mph"]), "sfull": float(tr["speed_full_mph"]),
            "class_pen": json.dumps(tr["class_penalty"]),
            "hw_pen": json.dumps(tr["highway_penalty"]),
            "full_canopy": S["shade"]["full_canopy_frac"],
        })
        print("Sidewalks...")
        sw = S["sidewalk"]
        run(engine, SIDEWALK_SQL, {
            "sw_pen": json.dumps(sw["penalty"]), "unknown": sw["unknown_penalty"],
            "shared": sw["shared_street_penalty"], "shared_types": sw["shared_street_types"],
            "off": sw["polygon_offset_m"], "search": sw["polygon_search_m"],
        })
        print("Crossings...")
        run(engine, CROSSINGS_SQL, {
            "major_classes": cr["major_classes"], "major_speed": cr["major_speed_mph"],
            "major_highways": cr["major_highways"],
            "class_pen": json.dumps(tr["class_penalty"]),
        })

    print("Costs...")
    w = dict(S["weights"])
    factors = ["slope", "traffic", "shade", "sidewalk"]
    spread = pd.read_sql("""
        WITH m AS (
            SELECT sum(length_m) AS L,
                   sum(p_slope*length_m)/sum(length_m)    AS m_slope,
                   sum(p_traffic*length_m)/sum(length_m)  AS m_traffic,
                   sum(p_shade*length_m)/sum(length_m)    AS m_shade,
                   sum(p_sidewalk*length_m)/sum(length_m) AS m_sidewalk
            FROM network.edge_penalties)
        SELECT sqrt(sum(length_m*(p_slope-m_slope)^2)/max(L))       AS slope,
               sqrt(sum(length_m*(p_traffic-m_traffic)^2)/max(L))   AS traffic,
               sqrt(sum(length_m*(p_shade-m_shade)^2)/max(L))       AS shade,
               sqrt(sum(length_m*(p_sidewalk-m_sidewalk)^2)/max(L)) AS sidewalk
        FROM network.edge_penalties, m
    """, engine).iloc[0]
    eff = dict(w)
    if S.get("standardize"):
        mean_spread = spread[factors].mean()
        for f in factors:
            eff[f] = float(w[f] * mean_spread / spread[f])
    table = pd.DataFrame({
        "weight": [w[f] for f in factors],
        "spread": [spread[f] for f in factors],
        "effective_weight": [eff[f] for f in factors],
    }, index=factors)
    table["influence"] = table["effective_weight"] * table["spread"]
    print("\nWeights (influence = effective weight x spread; should follow your ranking):")
    print(table.round(3).to_string())

    run(engine, COSTS_SQL, {
        "ws": eff["slope"], "wt": eff["traffic"], "wsh": eff["shade"], "wsw": eff["sidewalk"],
        "wo": w["offstreet"], "disc": S["offstreet_discount"],
        "wc": w["crossings"], "ccost": cr["crossing_cost_m"],
    })

    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    print("\nImpedance distribution (length-weighted share of network):")
    print(q("""
        SELECT CASE WHEN impedance < 1.25 THEN '1.00-1.25 pleasant'
                    WHEN impedance < 1.50 THEN '1.25-1.50'
                    WHEN impedance < 2.00 THEN '1.50-2.00'
                    ELSE '2.00+ hostile' END AS band,
               round(100 * sum(length_m) / (SELECT sum(length_m) FROM network.edge_costs)) AS pct_km
        FROM network.edge_costs GROUP BY 1 ORDER BY 1
    """).to_string(index=False))
    print("\nAverage penalty by factor (length-weighted):")
    print(q("""
        SELECT round((sum(p_slope*length_m)/sum(length_m))::numeric, 2)    AS slope,
               round((sum(p_traffic*length_m)/sum(length_m))::numeric, 2)  AS traffic,
               round((sum(p_shade*length_m)/sum(length_m))::numeric, 2)    AS shade,
               round((sum(p_sidewalk*length_m)/sum(length_m))::numeric, 2) AS sidewalk
        FROM network.edge_penalties
    """).to_string(index=False))
    print("\nSidewalk sources:")
    print(q("SELECT sidewalk_source, count(*) AS edges FROM network.edge_penalties "
            "GROUP BY 1 ORDER BY 2 DESC").to_string(index=False))
    print("\nMajor-street edges and crossing points:")
    print(q("SELECT count(*) FILTER (WHERE is_major) AS major_edges, "
            "sum(crossings) AS crossings FROM network.edge_penalties").to_string(index=False))


if __name__ == "__main__":
    main()
