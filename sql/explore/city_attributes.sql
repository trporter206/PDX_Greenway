-- Exploratory: what values do the city attribute fields hold?
-- Run: psql -U postgres -d pdx_greenway -f sql\explore\city_attributes.sql > city_attributes.txt

\echo '=== Shared segment id (tranplanid): rows vs distinct ids ==='
SELECT 'speed_limits' AS tbl, count(*) AS rows, count(DISTINCT tranplanid) AS ids FROM raw.city_speed_limits
UNION ALL SELECT 'traffic_classes', count(*), count(DISTINCT tranplanid) FROM raw.city_traffic_classes
UNION ALL SELECT 'sidewalk_completeness', count(*), count(DISTINCT tranplanid) FROM raw.city_sidewalk_completeness;

\echo '=== How many ids are shared between tables ==='
SELECT
  (SELECT count(DISTINCT s.tranplanid) FROM raw.city_speed_limits s
     JOIN raw.city_traffic_classes t USING (tranplanid)) AS speed_and_classes,
  (SELECT count(DISTINCT s.tranplanid) FROM raw.city_speed_limits s
     JOIN raw.city_sidewalk_completeness c USING (tranplanid)) AS speed_and_sidewalk;

\echo '=== Geometry types ==='
SELECT 'speed_limits' AS tbl, GeometryType(geometry) AS geom, count(*) FROM raw.city_speed_limits GROUP BY 2
UNION ALL SELECT 'traffic_classes', GeometryType(geometry), count(*) FROM raw.city_traffic_classes GROUP BY 2
UNION ALL SELECT 'sidewalk_completeness', GeometryType(geometry), count(*) FROM raw.city_sidewalk_completeness GROUP BY 2
UNION ALL SELECT 'traffic_volume', GeometryType(geometry), count(*) FROM raw.city_traffic_volume GROUP BY 2
UNION ALL SELECT 'traffic_speed', GeometryType(geometry), count(*) FROM raw.city_traffic_speed GROUP BY 2
UNION ALL SELECT 'sidewalks', GeometryType(geometry), count(*) FROM raw.city_sidewalks GROUP BY 2;

\echo '=== Speed limits ==='
SELECT speedlimit, count(*) FROM raw.city_speed_limits GROUP BY 1 ORDER BY 1;

\echo '=== TSP traffic classes ==='
SELECT traffic, count(*) FROM raw.city_traffic_classes GROUP BY 1 ORDER BY 2 DESC;

\echo '=== TSP pedestrian classes ==='
SELECT pedestrian, count(*) FROM raw.city_traffic_classes GROUP BY 1 ORDER BY 2 DESC;

\echo '=== TSP greenscape classes ==='
SELECT greenscape, count(*) FROM raw.city_traffic_classes GROUP BY 1 ORDER BY 2 DESC;

\echo '=== Sidewalk completeness ==='
SELECT sidewalkcompleteness, count(*) FROM raw.city_sidewalk_completeness GROUP BY 1 ORDER BY 2 DESC LIMIT 15;

\echo '=== Crossing gaps ==='
SELECT crossinggap, count(*) FROM raw.city_sidewalk_completeness GROUP BY 1 ORDER BY 2 DESC LIMIT 10;

\echo '=== Traffic counts: date range and locations ==='
SELECT 'volume' AS tbl, min(startdate) AS first, max(startdate) AS last,
       count(*) AS counts, count(DISTINCT locationid) AS locations FROM raw.city_traffic_volume
UNION ALL
SELECT 'speed', min(startdate), max(startdate), count(*), count(DISTINCT locationid) FROM raw.city_traffic_speed;
