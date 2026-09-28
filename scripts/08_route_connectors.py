"""
Step 08: Cross-town connectors.

1. Anchors: each adopted center (raw.city_centers) is snapped to the nearest main-
   network node from a point inside the center (ST_PointOnSurface).
2. Pairs: every pair of centers within connectors.max_pair_distance_m (straight line,
   center to center). Centers that snap farther than max_snap_m are left out.
3. Routes: pgRouting Dijkstra on network.edge_costs (the comfort-weighted cost), and a
   second run on plain length, so each route's detour can be measured.
4. Corridor frequency: how many connector routes use each edge. The heavily shared
   edges are the natural connector corridors.

Outputs (schema analysis):
  anchors              center -> snapped node
  connector_pairs      the pairs routed
  connector_routes     one line per pair: length, cost, shortest length, detour ratio
  connector_route_edges  every edge of every route, in order, with distance from each end
  connector_edge_use   edge_id -> number of routes using it
  corridor_use_v       map-ready view of edges with uses > 0
"""
import os
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
# PDX_CONFIG lets step 11 (stress test) run this step with an alternative config
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
C = CFG["connectors"]


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


SQL = """
DROP VIEW IF EXISTS analysis.corridor_use_v;

-- 1. anchors
DROP TABLE IF EXISTS analysis.anchors;
CREATE TABLE analysis.anchors AS
WITH net_nodes AS (
    SELECT n.osmid AS node, n.geometry AS geom
    FROM raw.osm_nodes n
    WHERE n.osmid IN (SELECT source FROM network.edge_costs
                      UNION SELECT target FROM network.edge_costs)
),
pts AS (
    SELECT objectid AS center_id, name, type, ST_PointOnSurface(geometry) AS pt
    FROM raw.city_centers
    WHERE NOT (name = ANY(CAST(:exclude AS text[])))
)
SELECT p.center_id, p.name, p.type, nn.node,
       round(ST_Distance(p.pt, nn.geom)::numeric) AS snap_m,
       p.pt AS center_pt,
       nn.geom
FROM pts p
CROSS JOIN LATERAL (
    SELECT node, geom FROM net_nodes ORDER BY geom <-> p.pt LIMIT 1
) nn;

-- 2. pairs
DROP TABLE IF EXISTS analysis.connector_pairs;
CREATE TABLE analysis.connector_pairs AS
SELECT row_number() OVER (ORDER BY a.center_id, b.center_id) AS pair_id,
       a.name AS from_name, b.name AS to_name,
       a.node AS from_node, b.node AS to_node,
       round(ST_Distance(a.center_pt, b.center_pt)::numeric) AS straight_m
FROM analysis.anchors a
JOIN analysis.anchors b ON a.center_id < b.center_id
WHERE ST_Distance(a.center_pt, b.center_pt) <= :max_dist
  AND a.snap_m <= :max_snap AND b.snap_m <= :max_snap   -- centers off the network are left out
  AND a.node <> b.node;

-- 3. routes: comfort-weighted and plain shortest
DROP TABLE IF EXISTS analysis._route_edges;
CREATE TABLE analysis._route_edges AS
SELECT d.start_vid, d.end_vid, d.path_seq, d.edge
FROM pgr_dijkstra(
    'SELECT edge_id AS id, source, target, cost, reverse_cost FROM network.edge_costs',
    'SELECT from_node AS source, to_node AS target FROM analysis.connector_pairs',
    directed => false
) d
WHERE d.edge <> -1;

DROP TABLE IF EXISTS analysis._shortest;
CREATE TABLE analysis._shortest AS
SELECT d.start_vid, d.end_vid, max(d.agg_cost) AS shortest_m
FROM pgr_dijkstra(
    'SELECT edge_id AS id, source, target, length_m AS cost, length_m AS reverse_cost FROM network.edge_costs',
    'SELECT from_node AS source, to_node AS target FROM analysis.connector_pairs',
    directed => false
) d
GROUP BY d.start_vid, d.end_vid;

DROP TABLE IF EXISTS analysis.connector_routes;
CREATE TABLE analysis.connector_routes AS
SELECT p.pair_id, p.from_name, p.to_name, p.straight_m,
       round(sum(c.length_m)::numeric) AS length_m,
       round(sum(c.cost)::numeric)     AS cost,
       round(s.shortest_m::numeric)    AS shortest_m,
       round((sum(c.length_m) / nullif(s.shortest_m, 0))::numeric, 2) AS detour_ratio,
       ST_LineMerge(ST_Collect(e.geom)) AS geom
FROM analysis.connector_pairs p
JOIN analysis._route_edges r ON r.start_vid = p.from_node AND r.end_vid = p.to_node
JOIN network.edge_costs c ON c.edge_id = r.edge
JOIN network.edges e ON e.id = r.edge
LEFT JOIN analysis._shortest s ON s.start_vid = p.from_node AND s.end_vid = p.to_node
GROUP BY p.pair_id, p.from_name, p.to_name, p.straight_m, s.shortest_m;

-- route edges in order, with distance from each end (used by step 09 to trim)
DROP TABLE IF EXISTS analysis.connector_route_edges;
CREATE TABLE analysis.connector_route_edges AS
SELECT p.pair_id, r.path_seq, r.edge AS edge_id, c.length_m,
       sum(c.length_m) OVER w - c.length_m               AS dist_from_start,
       sum(c.length_m) OVER (PARTITION BY p.pair_id) - sum(c.length_m) OVER w AS dist_to_end
FROM analysis.connector_pairs p
JOIN analysis._route_edges r ON r.start_vid = p.from_node AND r.end_vid = p.to_node
JOIN network.edge_costs c ON c.edge_id = r.edge
WINDOW w AS (PARTITION BY p.pair_id ORDER BY r.path_seq);
CREATE INDEX ON analysis.connector_route_edges (edge_id);

-- 4. corridor frequency
DROP TABLE IF EXISTS analysis.connector_edge_use;
CREATE TABLE analysis.connector_edge_use AS
SELECT edge AS edge_id, count(*) AS uses
FROM analysis._route_edges
GROUP BY edge;
CREATE UNIQUE INDEX ON analysis.connector_edge_use (edge_id);

DROP TABLE analysis._route_edges;
DROP TABLE analysis._shortest;

CREATE VIEW analysis.corridor_use_v AS
SELECT u.edge_id AS id, u.uses, e.name, e.highway, e.length_m,
       s.impedance, s.p_slope, s.p_traffic, s.p_shade, s.p_sidewalk, s.offstreet,
       e.geom
FROM analysis.connector_edge_use u
JOIN network.edges e ON e.id = u.edge_id
JOIN network.edges_scored_v s ON s.id = u.edge_id;
"""


def main():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS analysis"))
        for stmt in [s.strip() for s in SQL.split(";\n") if s.strip()]:
            conn.execute(text(stmt), {"exclude": C["exclude_centers"],
                                      "max_dist": C["max_pair_distance_m"],
                                      "max_snap": C["max_snap_m"]})

    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    anchors = q("SELECT name, type, snap_m FROM analysis.anchors ORDER BY snap_m DESC")
    far = anchors[anchors["snap_m"] > C["max_snap_m"]]
    print(f"Anchors: {len(anchors)} centers snapped to the network")
    if len(far):
        print(f"  WARNING - more than {C['max_snap_m']} m from the network, left out of pairs:")
        print(far.to_string(index=False))

    r = q("SELECT * FROM analysis.connector_routes")
    print(f"\nPairs within {C['max_pair_distance_m']:,} m: {len(r)}")
    print(f"  median route length {r['length_m'].median():,.0f} m, "
          f"median detour vs shortest path {r['detour_ratio'].median():.2f}x, "
          f"max detour {r['detour_ratio'].max():.2f}x")

    print("\nMost-shared streets (by number of connector routes):")
    print(q("""
        SELECT coalesce(e.name, '(' || e.highway || ')') AS street,
               max(u.uses) AS max_uses,
               round(sum(e.length_m)::numeric) AS shared_m
        FROM analysis.connector_edge_use u
        JOIN network.edges e ON e.id = u.edge_id
        GROUP BY 1
        ORDER BY max(u.uses) DESC, sum(e.length_m) DESC
        LIMIT 15
    """).to_string(index=False))

    print("\nBiggest detours (comfort route much longer than the shortest):")
    print(r.sort_values("detour_ratio", ascending=False)
           [["from_name", "to_name", "length_m", "shortest_m", "detour_ratio"]]
           .head(5).to_string(index=False))


if __name__ == "__main__":
    main()
