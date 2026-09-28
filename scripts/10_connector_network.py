"""
Step 10: Stitch the corridors into a connected connector network.

Round 1 (steps 08-09) found which streets carry connector travel, but as separate
pieces. Round 2 re-routes every connector pair with the step 09 corridor edges made
`network.corridor_discount` cheaper. Routes pull onto the corridors and link them
through the best connecting streets; because every route is a continuous path, the
result is connected by construction.

  network edges  = edges used by >= network.min_uses second-round routes, counting only
                   the part of each route more than corridors.trim_m from either end
  components     = connected pieces of that network; pieces shorter than
                   network.min_component_m are dropped

Outputs (schema analysis):
  network_route_edges   second-round route edges, ordered, with distance from each end
  network_routes        second-round route per pair: length, detour vs shortest path
  connector_network     network edges: uses, component, impedance, geometry
  connector_network_v   map-ready view
"""
import os
from pathlib import Path

import pandas as pd
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
# PDX_CONFIG lets step 11 (stress test) run this step with an alternative config
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
N, K = CFG["network"], CFG["corridors"]


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


SQL = """
DROP VIEW IF EXISTS analysis.connector_network_v;

-- round-2 costs: corridor edges discounted
DROP TABLE IF EXISTS analysis._cost2;
CREATE TABLE analysis._cost2 AS
SELECT c.edge_id, c.source, c.target, c.length_m,
       c.cost * CASE WHEN ce.edge_id IS NOT NULL THEN 1 - :discount ELSE 1 END AS cost
FROM network.edge_costs c
LEFT JOIN (SELECT DISTINCT edge_id FROM analysis.corridor_edges) ce USING (edge_id);

-- round-2 routes
DROP TABLE IF EXISTS analysis._r2;
CREATE TABLE analysis._r2 AS
SELECT d.start_vid, d.end_vid, d.path_seq, d.edge
FROM pgr_dijkstra(
    'SELECT edge_id AS id, source, target, cost, cost AS reverse_cost FROM analysis._cost2',
    'SELECT from_node AS source, to_node AS target FROM analysis.connector_pairs',
    directed => false
) d
WHERE d.edge <> -1;

DROP TABLE IF EXISTS analysis.network_route_edges;
CREATE TABLE analysis.network_route_edges AS
SELECT p.pair_id, r.path_seq, r.edge AS edge_id, c.length_m,
       sum(c.length_m) OVER w - c.length_m AS dist_from_start,
       sum(c.length_m) OVER (PARTITION BY p.pair_id) - sum(c.length_m) OVER w AS dist_to_end
FROM analysis.connector_pairs p
JOIN analysis._r2 r ON r.start_vid = p.from_node AND r.end_vid = p.to_node
JOIN analysis._cost2 c ON c.edge_id = r.edge
WINDOW w AS (PARTITION BY p.pair_id ORDER BY r.path_seq);
CREATE INDEX ON analysis.network_route_edges (edge_id);

DROP TABLE IF EXISTS analysis.network_routes;
CREATE TABLE analysis.network_routes AS
SELECT r2.pair_id, r1.from_name, r1.to_name,
       round(sum(r2.length_m)::numeric)                           AS length_m,
       r1.length_m                                                 AS round1_length_m,
       r1.shortest_m,
       round((sum(r2.length_m) / nullif(r1.shortest_m, 0))::numeric, 2) AS detour_ratio
FROM analysis.network_route_edges r2
JOIN analysis.connector_routes r1 USING (pair_id)
GROUP BY r2.pair_id, r1.from_name, r1.to_name, r1.length_m, r1.shortest_m;

-- network edges: trimmed second-round uses
DROP TABLE IF EXISTS analysis._net;
CREATE TABLE analysis._net AS
SELECT edge_id, count(*) AS uses
FROM analysis.network_route_edges
WHERE dist_from_start >= :trim AND dist_to_end >= :trim
GROUP BY edge_id
HAVING count(*) >= :min_uses;

-- connected components (edges that share a node)
DROP TABLE IF EXISTS analysis.connector_network;
CREATE TABLE analysis.connector_network AS
WITH comp AS (
    SELECT n.edge_id, n.uses, e.geom, e.length_m, e.name, e.highway,
           ST_ClusterDBSCAN(e.geom, eps := 0.5, minpoints := 1) OVER () AS component
    FROM analysis._net n
    JOIN network.edges e ON e.id = n.edge_id
),
sizes AS (
    SELECT component, sum(length_m) AS comp_m FROM comp GROUP BY component
)
SELECT c.edge_id, c.uses, c.component, round(s.comp_m::numeric) AS component_m,
       c.name, c.highway, c.length_m,
       ec.impedance,
       (ce.edge_id IS NOT NULL) AS in_round1_corridor,
       c.geom
FROM comp c
JOIN sizes s USING (component)
JOIN network.edge_costs ec ON ec.edge_id = c.edge_id
LEFT JOIN (SELECT DISTINCT edge_id FROM analysis.corridor_edges) ce ON ce.edge_id = c.edge_id
WHERE s.comp_m >= :min_comp;
CREATE UNIQUE INDEX ON analysis.connector_network (edge_id);
CREATE INDEX ON analysis.connector_network USING GIST (geom);

CREATE VIEW analysis.connector_network_v AS
SELECT edge_id AS id, uses, component, component_m, name, highway, length_m,
       round(impedance::numeric, 2) AS impedance, in_round1_corridor, geom
FROM analysis.connector_network;

DROP TABLE analysis._cost2;
DROP TABLE analysis._r2;
DROP TABLE analysis._net;
"""


def main():
    engine = get_engine()
    with engine.begin() as conn:
        for stmt in [s.strip() for s in SQL.split(";\n") if s.strip()]:
            conn.execute(text(stmt), {
                "discount": N["corridor_discount"], "trim": K["trim_m"],
                "min_uses": N["min_uses"], "min_comp": N["min_component_m"],
            })

    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    net = q("""
        SELECT count(DISTINCT component) AS pieces,
               round((sum(length_m)/1000)::numeric, 1) AS km,
               round((100 * sum(length_m) FILTER (WHERE in_round1_corridor) / sum(length_m))::numeric) AS pct_on_round1_corridors,
               round((sum(impedance*length_m)/sum(length_m))::numeric, 2) AS mean_impedance
        FROM analysis.connector_network
    """).iloc[0]
    r1 = q("SELECT count(*) AS n, round((sum(length_m)/1000)::numeric, 1) AS km FROM analysis.corridors").iloc[0]
    print(f"Round 1 corridors: {int(r1['n'])} separate pieces, {r1['km']} km")
    print(f"Connected network: {int(net['pieces'])} connected piece(s), {net['km']} km, "
          f"mean impedance {net['mean_impedance']}")
    print(f"  {net['pct_on_round1_corridors']}% of it lies on round-1 corridors; "
          f"the rest are the linking streets")

    routes = q("SELECT * FROM analysis.network_routes")
    print(f"\nSecond-round routes: median detour {routes['detour_ratio'].median():.2f}x "
          f"(round 1 was {(routes['round1_length_m'] / routes['shortest_m']).median():.2f}x), "
          f"max {routes['detour_ratio'].max():.2f}x")

    print("\nConnected pieces:")
    print(q("""
        SELECT component, max(component_m) AS length_m, count(*) AS edges,
               max(uses) AS max_uses
        FROM analysis.connector_network GROUP BY component ORDER BY 2 DESC
    """).to_string(index=False))


if __name__ == "__main__":
    main()
