"""
Step 09: Extract continuous corridors from the connector routes.

1. Trim: a route's use of an edge only counts if the edge is more than trim_m from
   both ends of the route (network distance). Getting out of a center isn't a corridor.
2. Band support: each edge gets the trimmed uses of every roughly parallel edge within
   band_half_width_m (itself included). Routes that stair-step between parallel
   streets add up to one band instead of being split across several streets.
3. Street runs: edges with band support >= min_band_uses AND impedance <=
   max_edge_impedance are grouped into stretches of the same street name (unnamed
   paths grouped by type). Named pieces less than gap_join_m apart are joined;
   unnamed ones only when they touch. Hostile streets
   (typically arterials) can lend support to a quiet parallel street but can't carry
   a corridor themselves; otherwise long, continuous arterials would win every band.
4. One street per band: a candidate is dropped if most of it lies within the band of
   better parallel candidates combined (better = higher support per unit impedance).
   Candidates shorter than min_length_m drop.

Outputs (schema analysis):
  edge_use_trimmed   edge_id -> trimmed uses
  edge_band_support  edge_id -> band support
  corridors          ranked corridors: name, length, support, impedance, geometry
  corridor_edges     corridor_id -> edge_id
"""
import os
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
# PDX_CONFIG lets step 11 (stress test) run this step with an alternative config
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
K = CFG["corridors"]


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


SQL = """
-- 1. trimmed uses
DROP TABLE IF EXISTS analysis.edge_use_trimmed;
CREATE TABLE analysis.edge_use_trimmed AS
SELECT edge_id, count(*) AS uses
FROM analysis.connector_route_edges
WHERE dist_from_start >= :trim AND dist_to_end >= :trim
GROUP BY edge_id;
CREATE UNIQUE INDEX ON analysis.edge_use_trimmed (edge_id);

-- helper: edge geometry, direction (0-180), impedance, and a grouping name
DROP TABLE IF EXISTS analysis._e;
CREATE TABLE analysis._e AS
SELECT e.id AS edge_id, e.geom, e.length_m,
       mod(degrees(ST_Azimuth(ST_StartPoint(e.geom), ST_EndPoint(e.geom)))::numeric, 180)::float AS dir,
       c.impedance,
       coalesce(e.name, CASE WHEN network.is_offstreet(e.highway) THEN '(path)' ELSE '(unnamed street)' END) AS run_name
FROM network.edges e
JOIN network.edge_costs c ON c.edge_id = e.id
WHERE ST_Length(e.geom) > 0
  AND NOT ST_Equals(ST_StartPoint(e.geom), ST_EndPoint(e.geom));
CREATE INDEX ON analysis._e USING GIST (geom);
CREATE UNIQUE INDEX ON analysis._e (edge_id);

-- 2. band support (only edges near used edges can have any)
DROP TABLE IF EXISTS analysis.edge_band_support;
CREATE TABLE analysis.edge_band_support AS
SELECT a.edge_id, sum(u.uses) AS support
FROM analysis._e a
JOIN analysis._e b ON ST_DWithin(a.geom, b.geom, :band)
JOIN analysis.edge_use_trimmed u ON u.edge_id = b.edge_id
WHERE least(abs(a.dir - b.dir), 180 - abs(a.dir - b.dir)) <= :angle
  -- b's midpoint must sit beside a (not just touch its end)
  AND ST_DWithin(a.geom, ST_LineInterpolatePoint(b.geom, 0.5), :band)
GROUP BY a.edge_id;
CREATE UNIQUE INDEX ON analysis.edge_band_support (edge_id);

-- 3. candidate stretches: contiguous, same street, enough support, not hostile.
--    Edge-level clustering is stored once so candidate ids and memberships match.
DROP TABLE IF EXISTS analysis._clustered;
CREATE TABLE analysis._clustered AS
WITH strong AS (
    SELECT e.*, s.support
    FROM analysis._e e
    JOIN analysis.edge_band_support s USING (edge_id)
    WHERE s.support >= :min_support
      AND e.impedance <= :max_imp
),
clustered AS (
    -- named streets: join pieces up to gap_join_m apart
    SELECT *, ST_ClusterDBSCAN(geom, eps := :gap, minpoints := 1)
                OVER (PARTITION BY run_name) AS cl
    FROM strong
    WHERE run_name NOT IN ('(path)', '(unnamed street)')
    UNION ALL
    -- unnamed streets and paths share a placeholder name, so only join pieces that
    -- actually touch; otherwise unrelated alleys or trails nearby would be merged
    SELECT *, ST_ClusterDBSCAN(geom, eps := 0.5, minpoints := 1)
                OVER (PARTITION BY run_name) AS cl
    FROM strong
    WHERE run_name IN ('(path)', '(unnamed street)')
)
SELECT dense_rank() OVER (ORDER BY run_name, cl) AS cand_id, * FROM clustered;

DROP TABLE IF EXISTS analysis._cand;
CREATE TABLE analysis._cand AS
SELECT cand_id,
       min(run_name)                                   AS name,
       sum(length_m)                                   AS length_m,
       sum(support * length_m) / sum(length_m)         AS support,
       sum(impedance * length_m) / sum(length_m)       AS impedance,
       ST_LineMerge(ST_Union(geom))                    AS geom
FROM analysis._clustered
GROUP BY cand_id
HAVING sum(length_m) >= :min_len;
CREATE INDEX ON analysis._cand USING GIST (geom);

-- 4. one street per band: drop a candidate if most of it lies within the band of a
--    better parallel candidate (better = higher support per unit impedance)
DROP TABLE IF EXISTS analysis.corridors;
CREATE TABLE analysis.corridors AS
SELECT row_number() OVER (ORDER BY c.support * c.length_m DESC) AS corridor_id,
       c.cand_id, c.name, round(c.length_m::numeric) AS length_m,
       round(c.support::numeric, 1)       AS support,
       round(c.impedance::numeric, 2)     AS impedance,
       round((c.support * c.length_m / 1000)::numeric, 1) AS route_km,
       c.geom
FROM analysis._cand c
WHERE coalesce((
    SELECT ST_Length(ST_Intersection(c.geom,
               ST_Union(ST_Buffer(d.geom, :band, 'endcap=flat'))))
    FROM analysis._cand d
    WHERE d.cand_id <> c.cand_id
      AND ST_DWithin(c.geom, d.geom, :band)
      AND (d.support / d.impedance) > (c.support / c.impedance)
), 0) <= 0.5 * c.length_m;
CREATE INDEX ON analysis.corridors USING GIST (geom);

-- which edges belong to which kept corridor (used by step 10)
DROP TABLE IF EXISTS analysis.corridor_edges;
CREATE TABLE analysis.corridor_edges AS
SELECT c.corridor_id, k.edge_id
FROM analysis.corridors c
JOIN analysis._clustered k USING (cand_id);
CREATE INDEX ON analysis.corridor_edges (edge_id);

DROP TABLE analysis._e;
DROP TABLE analysis._cand;
DROP TABLE analysis._clustered;
"""


def main():
    engine = get_engine()
    params = {"trim": K["trim_m"], "band": K["band_half_width_m"],
              "angle": K["parallel_max_angle"], "min_support": K["min_band_uses"],
              "min_len": K["min_length_m"], "max_imp": K["max_edge_impedance"],
              "gap": K["gap_join_m"]}
    with engine.begin() as conn:
        for stmt in [s.strip() for s in SQL.split(";\n") if s.strip()]:
            conn.execute(text(stmt), params)

    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    n = q("SELECT count(*) AS n, round(sum(length_m)/1000, 1) AS km FROM analysis.corridors").iloc[0]
    # sanity check: corridor memberships must add up to the corridor lengths
    chk = q("""
        SELECT round(sum(c.length_m)) AS corridor_m,
               round((SELECT sum(e.length_m) FROM analysis.corridor_edges ce
                      JOIN network.edges e ON e.id = ce.edge_id)::numeric) AS member_m
        FROM analysis.corridors c
    """).iloc[0]
    # corridor lengths are rounded to the meter, so allow up to half a meter each
    if int(n["n"]) == 0:
        print("No corridors met the thresholds.")
    elif abs(float(chk["corridor_m"]) - float(chk["member_m"])) > 0.5 * int(n["n"]) + 1:
        print(f"WARNING: corridor membership mismatch ({chk['member_m']} m of edges "
              f"vs {chk['corridor_m']} m of corridors)")
    print(f"Corridors: {int(n['n'])} totalling {n['km']} km")
    print("\nTop corridors (route_km = support x length, i.e. how much connector travel they carry):")
    print(q("""
        SELECT corridor_id AS id, name, length_m, support, impedance, route_km
        FROM analysis.corridors ORDER BY corridor_id LIMIT 20
    """).to_string(index=False))


if __name__ == "__main__":
    main()
