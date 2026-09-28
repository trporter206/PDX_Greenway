-- =====================================================================
-- Step 02: Build the routing network (centerline model)
--
-- Input:  raw.osm_edges (from scripts/01_fetch_osm.py)
-- Output: network.edges          routing network, largest connected piece flagged
--         network.dropped_edges  everything filtered out, with a reason
--         network.islands        disconnected pieces, for review in QGIS
--
-- Run:  psql -U postgres -d pdx_greenway -f sql\02_build_network.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Classify every OSM edge: keep or drop, and why
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS network.edge_class;
CREATE TABLE network.edge_class AS
WITH base AS (
    SELECT
        e.*,
        coalesce(e.footway, '') AS fw,
        coalesce(e.service, '') AS svc
    FROM raw.osm_edges e
),
-- Nodes touched by kept off-street paths (trails, park paths, steps, cut-throughs).
-- Crossings that connect to these must survive, or trails get cut off from streets.
path_nodes AS (
    SELECT source AS node FROM base
    WHERE highway IN ('footway', 'path', 'steps', 'pedestrian', 'cycleway')
      AND fw NOT LIKE '%sidewalk%' AND fw NOT LIKE '%crossing%'
    UNION
    SELECT target FROM base
    WHERE highway IN ('footway', 'path', 'steps', 'pedestrian', 'cycleway')
      AND fw NOT LIKE '%sidewalk%' AND fw NOT LIKE '%crossing%'
)
SELECT
    b.id,
    CASE
        -- separately mapped sidewalks: represented as a street attribute instead
        WHEN b.highway = 'footway' AND b.fw LIKE '%sidewalk%'
            THEN 'drop: sidewalk'
        -- crossings between sidewalks: the street intersection already covers these,
        -- unless the crossing connects an off-street path to the network
        WHEN b.highway = 'footway' AND b.fw LIKE '%crossing%'
             AND b.source NOT IN (SELECT node FROM path_nodes)
             AND b.target NOT IN (SELECT node FROM path_nodes)
            THEN 'drop: sidewalk crossing'
        -- driveways, parking aisles, drive-throughs (alleys are kept)
        WHEN b.highway = 'service'
             AND b.svc IN ('driveway', 'parking_aisle', 'drive-through')
            THEN 'drop: ' || b.svc
        ELSE 'keep'
    END AS decision
FROM base b;

-- ---------------------------------------------------------------------
-- 2. Split into kept network and dropped edges
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS network.dropped_edges;
CREATE TABLE network.dropped_edges AS
SELECT e.id AS osm_edge_id, c.decision, e.highway, e.footway, e.service, e.name,
       e.length_m, e.geometry AS geom
FROM raw.osm_edges e
JOIN network.edge_class c USING (id)
WHERE c.decision <> 'keep';

DROP TABLE IF EXISTS network.edges;
CREATE TABLE network.edges AS
SELECT
    e.id AS osm_edge_id,
    e.source,
    e.target,
    e.osmid,
    e.name,
    e.highway,
    e.service,
    e.footway,
    e.sidewalk,
    e.surface,
    e.lit,
    e.maxspeed,
    e.lanes,
    e.length_m,
    e.geometry AS geom
FROM raw.osm_edges e
JOIN network.edge_class c USING (id)
WHERE c.decision = 'keep';

ALTER TABLE network.edges ADD COLUMN id bigserial PRIMARY KEY;
CREATE INDEX ON network.edges USING GIST (geom);
CREATE INDEX ON network.edges (source);
CREATE INDEX ON network.edges (target);

-- ---------------------------------------------------------------------
-- 3. Connectivity check with pgRouting
--    Removing edges can strand small pieces. Find them.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS network.node_components;
CREATE TABLE network.node_components AS
SELECT node, component
FROM pgr_connectedComponents(
    'SELECT id, source, target, length_m AS cost, length_m AS reverse_cost
     FROM network.edges'
);
CREATE INDEX ON network.node_components (node);

ALTER TABLE network.edges ADD COLUMN component bigint;
UPDATE network.edges e
SET component = nc.component
FROM network.node_components nc
WHERE nc.node = e.source;

-- Flag the main network (the largest component by length)
ALTER TABLE network.edges ADD COLUMN in_main boolean DEFAULT false;
WITH main AS (
    SELECT component
    FROM network.edges
    GROUP BY component
    ORDER BY sum(length_m) DESC
    LIMIT 1
)
UPDATE network.edges SET in_main = true
WHERE component = (SELECT component FROM main);

-- Islands: every disconnected piece, dissolved, for visual review
DROP TABLE IF EXISTS network.islands;
CREATE TABLE network.islands AS
SELECT
    component,
    count(*)                         AS edges,
    round(sum(length_m)::numeric)    AS length_m,
    string_agg(DISTINCT highway, ', ') AS highway_types,
    ST_Union(geom)                   AS geom
FROM network.edges
WHERE NOT in_main
GROUP BY component;

-- ---------------------------------------------------------------------
-- 4. Summary
-- ---------------------------------------------------------------------
\echo '--- What was dropped ---'
SELECT decision, count(*) AS edges, round(sum(length_m)::numeric / 1000) AS km
FROM network.dropped_edges
GROUP BY decision
ORDER BY km DESC;

\echo '--- Kept network ---'
SELECT
    count(*) FILTER (WHERE in_main)                                   AS main_edges,
    round(sum(length_m) FILTER (WHERE in_main)::numeric / 1000)       AS main_km,
    count(DISTINCT component) FILTER (WHERE NOT in_main)              AS islands,
    round(sum(length_m) FILTER (WHERE NOT in_main)::numeric / 1000)   AS island_km
FROM network.edges;

\echo '--- Largest islands ---'
SELECT component, edges, length_m, highway_types
FROM network.islands
ORDER BY length_m DESC
LIMIT 10;
