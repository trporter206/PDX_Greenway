"""
Step 12: Validation against the city's bikeway network.

Each edge of the connector network is classified by the first city category it matches
(order from config.yaml `validation.categories`):
  existing_greenway   neighborhood greenways (NG) and trails that exist today
  planned             funded projects
  recommended         in city plans but unfunded
  existing_bikeway    other existing bike facilities (lanes on busier streets; weakest match)
  new                 none of the above: the analysis's own recommendations

Two levels, like the stress test:
  street    a city line within street_match_m, running the same way, covers at least
            street_match_share of the edge
  corridor  a city line of that category runs parallel within the corridor band
            (corridors.band_half_width_m)

The reverse is also reported: how much of the city's existing greenway network lies within
the band of the connector network.

Outputs (schema analysis):
  validation_edges   edge -> street_class, corridor_class
  validation_v       map-ready view, with the stress-test robustness joined on
"""
import os
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
V, K = CFG["validation"], CFG["corridors"]


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


def category_sql():
    """One SELECT per source, unioned into (cat, rank, geom)."""
    parts = []
    for rank, (cat, sources) in enumerate(V["categories"].items(), 1):
        for src in sources:
            where = ["geometry IS NOT NULL"]
            if src.get("facilities"):
                vals = ", ".join(f"'{f}'" for f in src["facilities"])
                where.append(f"facility IN ({vals})")
            if src.get("exclude_facilities"):
                vals = ", ".join(f"'{f}'" for f in src["exclude_facilities"])
                where.append(f"coalesce(facility, '') NOT IN ({vals})")
            parts.append(
                f"SELECT '{cat}'::text AS cat, {rank} AS rank, "
                f"(ST_Dump(ST_LineMerge(geometry))).geom AS geom "
                f"FROM raw.city_{src['table']} WHERE {' AND '.join(where)}"
            )
    return "\nUNION ALL\n".join(parts)


SQL = """
DROP VIEW IF EXISTS analysis.validation_v;

-- city lines cut into pieces of at most 50 m, so each piece has a local direction
-- and midpoint (long trails and curvy segments would otherwise compare badly)
DROP TABLE IF EXISTS analysis._cat;
CREATE TABLE analysis._cat AS
WITH lines AS (
    SELECT cat, rank, geom, greatest(ceil(ST_Length(geom) / 50.0), 1)::int AS n
    FROM ({cats}) c
    WHERE GeometryType(geom) = 'LINESTRING' AND ST_Length(geom) > 0
),
pieces AS (
    SELECT cat, rank, ST_LineSubstring(geom, i::float / n, (i + 1)::float / n) AS geom
    FROM lines, generate_series(0, n - 1) AS i
)
SELECT cat, rank, geom,
       mod(degrees(ST_Azimuth(ST_StartPoint(geom), ST_EndPoint(geom)))::numeric, 180)::float AS dir
FROM pieces
WHERE NOT ST_Equals(ST_StartPoint(geom), ST_EndPoint(geom));
CREATE INDEX ON analysis._cat USING GIST (geom);

DROP TABLE IF EXISTS analysis._net;
CREATE TABLE analysis._net AS
SELECT n.edge_id, n.geom, n.length_m,
       mod(degrees(ST_Azimuth(ST_StartPoint(n.geom), ST_EndPoint(n.geom)))::numeric, 180)::float AS dir
FROM analysis.connector_network n
WHERE NOT ST_Equals(ST_StartPoint(n.geom), ST_EndPoint(n.geom));
CREATE INDEX ON analysis._net USING GIST (geom);

DROP TABLE IF EXISTS analysis.validation_edges;
CREATE TABLE analysis.validation_edges AS
WITH near AS (
    -- buffered parallel city lines of each category, merged per edge
    SELECT n.edge_id, c.cat, min(c.rank) AS rank,
           ST_Union(ST_Buffer(c.geom, :street_m)) AS zone
    FROM analysis._net n
    JOIN analysis._cat c
      ON ST_DWithin(n.geom, c.geom, :street_m)
     AND least(abs(n.dir - c.dir), 180 - abs(n.dir - c.dir)) <= :angle
    GROUP BY n.edge_id, c.cat
),
street AS (
    -- share of each edge lying inside that zone
    SELECT nr.edge_id, nr.cat, nr.rank,
           ST_Length(ST_Intersection(n.geom, nr.zone)) / n.length_m AS cover
    FROM near nr JOIN analysis._net n USING (edge_id)
),
street_best AS (
    SELECT DISTINCT ON (edge_id) edge_id, cat
    FROM street WHERE cover >= :street_share
    ORDER BY edge_id, rank
),
corridor_best AS (
    SELECT DISTINCT ON (n.edge_id) n.edge_id, c.cat
    FROM analysis._net n
    JOIN analysis._cat c
      ON ST_DWithin(n.geom, c.geom, :band)
     AND ST_DWithin(n.geom, ST_LineInterpolatePoint(c.geom, 0.5), :band)
     AND least(abs(n.dir - c.dir), 180 - abs(n.dir - c.dir)) <= :angle
    ORDER BY n.edge_id, c.rank
)
SELECT n.edge_id,
       coalesce(s.cat, 'new') AS street_class,
       coalesce(c.cat, 'new') AS corridor_class
FROM analysis._net n
LEFT JOIN street_best s USING (edge_id)
LEFT JOIN corridor_best c USING (edge_id);
CREATE UNIQUE INDEX ON analysis.validation_edges (edge_id);

-- reverse check: how much of the existing greenway network runs within the band
DROP TABLE IF EXISTS analysis._recall;
CREATE TABLE analysis._recall AS
SELECT c.cat, ST_Length(c.geom) AS len_m,
       EXISTS (
           SELECT 1 FROM analysis._net n
           WHERE ST_DWithin(n.geom, ST_LineInterpolatePoint(c.geom, 0.5), :band)
             AND least(abs(n.dir - c.dir), 180 - abs(n.dir - c.dir)) <= :angle
       ) AS near_network,
       ST_Intersects(c.geom, b.geometry) AS in_city
FROM analysis._cat c, raw.city_city_boundary_official b
WHERE c.cat = 'existing_greenway';

CREATE VIEW analysis.validation_v AS
SELECT v.edge_id AS id, v.street_class, v.corridor_class,
       s.corridor_robustness, s.corridor_share,
       n.name, n.highway, n.length_m, n.uses, n.geom
FROM analysis.validation_edges v
JOIN analysis.connector_network n USING (edge_id)
LEFT JOIN analysis.stress_v s ON s.id = v.edge_id;

DROP TABLE analysis._cat;
DROP TABLE analysis._net;
"""


def main():
    engine = get_engine()
    sql = SQL.replace("{cats}", category_sql())
    params = {"street_m": V["street_match_m"], "street_share": V["street_match_share"],
              "band": K["band_half_width_m"], "angle": K["parallel_max_angle"]}
    with engine.begin() as conn:
        for stmt in [s.strip() for s in sql.split(";\n") if s.strip()]:
            conn.execute(text(stmt), params)

    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    order = list(V["categories"]) + ["new"]
    tab = q("""
        WITH x AS (
            SELECT n.length_m, 'street' AS level, v.street_class AS cls
            FROM analysis.validation_edges v JOIN analysis.connector_network n USING (edge_id)
            UNION ALL
            SELECT n.length_m, 'corridor', v.corridor_class
            FROM analysis.validation_edges v JOIN analysis.connector_network n USING (edge_id)
        )
        SELECT cls,
               round((sum(length_m) FILTER (WHERE level = 'street') / 1000)::numeric, 1) AS street_km,
               round((sum(length_m) FILTER (WHERE level = 'corridor') / 1000)::numeric, 1) AS corridor_km
        FROM x GROUP BY cls
    """).set_index("cls").reindex(order).fillna(0)
    total = tab["street_km"].sum()
    tab["street_pct"] = (100 * tab["street_km"] / total).round()
    tab["corridor_pct"] = (100 * tab["corridor_km"] / total).round()
    print("Connector network vs. the city's bikeway network:")
    print(tab[["street_km", "street_pct", "corridor_km", "corridor_pct"]].to_string())

    rec = q("""
        SELECT round((100 * sum(len_m) FILTER (WHERE near_network) / sum(len_m))::numeric) AS pct
        FROM analysis._recall WHERE in_city
    """).iloc[0]["pct"]
    print(f"\nExisting greenways and trails in the city within the network's corridors: {rec}%"
          "\n  (low is expected: the network only links centers within 7 km)")

    print("\nRobust corridors the city has NOT built, funded, or recommended (corridor level):")
    print(q("""
        SELECT coalesce(name, '(' || highway || ')') AS street,
               round((sum(length_m) / 1000)::numeric, 2) AS km
        FROM analysis.validation_v
        WHERE corridor_class = 'new' AND corridor_robustness = 'robust'
        GROUP BY 1 HAVING sum(length_m) >= 200
        ORDER BY 2 DESC LIMIT 15
    """).to_string(index=False))


if __name__ == "__main__":
    main()
