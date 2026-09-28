-- =====================================================================
-- Step 04: Match City of Portland attributes onto the OSM network
--
-- The three PBOT line layers use different segment ids, so each one is
-- matched to OSM spatially with the same rules (network.conflate):
--   * candidates: city segments within `buf` meters of an OSM street edge
--   * overlap:    length of the OSM edge lying inside the city segment's buffer
--   * direction:  angle between the OSM edge and the nearby piece of city line
--   * a match needs a parallel direction (<= max_angle) plus meaningful overlap,
--     OR, for long curvy edges (>= 50 m), at least 60% of the edge covered.
--     Short cross streets near a city line are rejected by the angle test.
--   * best candidate = most overlap, then smallest angle
-- Off-street paths are not matched: they carry no car traffic by definition.
--
-- Input:  network.edges, raw.city_speed_limits, raw.city_traffic_classes,
--         raw.city_sidewalk_completeness
-- Output: network.match_*          one row per matched OSM edge, per layer
--         network.edge_attributes  combined attributes for every edge
--         network.unmatched_streets  street edges with no speed/class match (QA)
--
-- Run:  psql -U postgres -d pdx_greenway -f sql\04_conflate_attributes.sql
-- =====================================================================

SET client_min_messages = warning;

-- Off-street = every part of the highway tag is a non-car type.
-- OSMnx merges tags when it simplifies edges (e.g. 'steps;footway'), so an
-- exact-match list would treat those merged paths as streets.
CREATE OR REPLACE FUNCTION network.is_offstreet(hw text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS
$$
    SELECT coalesce(bool_and(part IN ('footway','path','steps','pedestrian','cycleway',
                                      'track','bridleway','corridor','elevator')), false)
    FROM unnest(string_to_array(hw, ';')) AS part
$$;

CREATE OR REPLACE FUNCTION network.conflate(
    src        text,                         -- source table, e.g. 'raw.city_speed_limits'
    dst        text,                         -- output table name in schema network
    buf        double precision DEFAULT 12,  -- meters
    max_angle  double precision DEFAULT 30   -- degrees
) RETURNS void
LANGUAGE plpgsql AS
$f$
BEGIN
    -- Buffered copy of the source layer (buffer once, not once per pair)
    DROP TABLE IF EXISTS _src;
    EXECUTE format(
        'CREATE TEMP TABLE _src AS
         SELECT objectid AS src_id, geometry AS geom, ST_Buffer(geometry, %s) AS buf
         FROM %s WHERE geometry IS NOT NULL', buf, src);
    CREATE INDEX ON _src USING GIST (geom);
    ANALYZE _src;

    EXECUTE format('DROP TABLE IF EXISTS network.%I', dst);
    EXECUTE format($q$
        CREATE TABLE network.%I AS
        WITH streets AS (
            SELECT id, geom, length_m
            FROM network.edges
            WHERE in_main
              AND NOT network.is_offstreet(highway)
              AND length_m > 0
        ),
        cand AS (
            SELECT
                e.id AS edge_id,
                s.src_id,
                e.length_m,
                ST_Length(ST_Intersection(e.geom, s.buf)) AS overlap_m,
                e.geom AS e_geom,
                -- longest piece of the city line near this OSM edge
                (SELECT d.geom
                   FROM ST_Dump(ST_CollectionExtract(
                          ST_Intersection(s.geom, ST_Buffer(e.geom, %s)), 2)) d
                  ORDER BY ST_Length(d.geom) DESC LIMIT 1) AS c_local
            FROM streets e
            JOIN _src s ON ST_DWithin(e.geom, s.geom, %s)
        ),
        scored AS (
            SELECT
                edge_id, src_id, length_m, overlap_m,
                CASE
                    WHEN c_local IS NULL OR ST_Length(c_local) < 1
                      OR ST_Equals(ST_StartPoint(e_geom), ST_EndPoint(e_geom))
                    THEN 90.0
                    ELSE (
                        SELECT least(d, 180 - d) FROM (
                            SELECT mod((degrees(ST_Azimuth(ST_StartPoint(e_geom), ST_EndPoint(e_geom)))
                                      - degrees(ST_Azimuth(ST_StartPoint(c_local), ST_EndPoint(c_local)))
                                      + 360)::numeric, 180)::float AS d
                        ) a
                    )
                END AS angle
            FROM cand
        ),
        valid AS (
            SELECT * FROM scored
            WHERE (angle <= %s AND overlap_m >= least(20, 0.5 * length_m))
               -- long, curvy edges: overall direction is unreliable, so trust coverage
               OR (length_m >= 50 AND overlap_m / length_m >= 0.6)
        )
        SELECT DISTINCT ON (edge_id)
            edge_id, src_id,
            round((overlap_m / length_m)::numeric, 2) AS match_frac,
            round(angle::numeric, 1)                  AS angle_deg
        FROM valid
        ORDER BY edge_id, overlap_m DESC, angle ASC
    $q$, dst, buf, buf, max_angle);

    EXECUTE format('CREATE UNIQUE INDEX ON network.%I (edge_id)', dst);
    DROP TABLE _src;
END;
$f$;

-- ---------------------------------------------------------------------
-- 1. Match each city layer
-- ---------------------------------------------------------------------
\echo 'Matching speed limits...'
SELECT network.conflate('raw.city_speed_limits',        'match_speed');
\echo 'Matching TSP traffic classes...'
SELECT network.conflate('raw.city_traffic_classes',     'match_classes');
\echo 'Matching sidewalk completeness...'
SELECT network.conflate('raw.city_sidewalk_completeness', 'match_sidewalk');

-- ---------------------------------------------------------------------
-- 2. Combine into one attribute table
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS network.edge_attributes;
CREATE TABLE network.edge_attributes AS
SELECT
    e.id AS edge_id,
    e.highway,
    network.is_offstreet(e.highway) AS offstreet,

    -- speed: city first, then OSM maxspeed, then null (filled in scoring)
    coalesce(
        nullif(regexp_replace(sl.speedlimit::text, '[^0-9]', '', 'g'), '')::int,
        nullif(substring(e.maxspeed FROM '([0-9]+)'), '')::int
    ) AS speed_mph,
    CASE WHEN sl.speedlimit IS NOT NULL THEN 'city'
         WHEN e.maxspeed ~ '[0-9]' THEN 'osm' END AS speed_source,

    tc.traffic      AS tsp_traffic,
    tc.pedestrian   AS tsp_pedestrian,
    (tc.greenscape = 'Y') AS tsp_greenscape,

    CASE upper(replace(sc.sidewalkcompleteness, ' ', ''))
        WHEN 'BOTHSIDES'          THEN 'both'
        WHEN 'CONSIDEREDCOMPLETE' THEN 'both'
        WHEN 'ONESIDE'            THEN 'one'
        WHEN 'NEITHERSIDE'        THEN 'none'
    END AS sidewalk_city,
    e.sidewalk AS sidewalk_osm,
    sc.crossinggap,

    ms.match_frac AS speed_match_frac,
    mc.match_frac AS class_match_frac,
    mw.match_frac AS sidewalk_match_frac
FROM network.edges e
LEFT JOIN network.match_speed    ms ON ms.edge_id = e.id
LEFT JOIN raw.city_speed_limits  sl ON sl.objectid = ms.src_id
LEFT JOIN network.match_classes  mc ON mc.edge_id = e.id
LEFT JOIN raw.city_traffic_classes tc ON tc.objectid = mc.src_id
LEFT JOIN network.match_sidewalk mw ON mw.edge_id = e.id
LEFT JOIN raw.city_sidewalk_completeness sc ON sc.objectid = mw.src_id
WHERE e.in_main;

CREATE UNIQUE INDEX ON network.edge_attributes (edge_id);

-- Streets that matched neither speed nor class: review these in QGIS.
-- in_city = false means the edge is in the buffer past city limits, where
-- no city data exists, so a miss there is expected.
DROP TABLE IF EXISTS network.unmatched_streets;
CREATE TABLE network.unmatched_streets AS
SELECT e.id, e.highway, e.name, e.length_m,
       ST_Intersects(e.geom, b.geometry) AS in_city,
       e.geom
FROM network.edges e
JOIN network.edge_attributes a ON a.edge_id = e.id
CROSS JOIN raw.city_city_boundary_official b
WHERE NOT a.offstreet
  AND a.speed_match_frac IS NULL
  AND a.class_match_frac IS NULL;

-- ---------------------------------------------------------------------
-- 3. Summary
-- ---------------------------------------------------------------------
\echo '--- Match rates on street edges, by OSM street type (share of km) ---'
SELECT
    a.highway,
    round(sum(e.length_m)::numeric / 1000)                                           AS km,
    round(100.0 * sum(e.length_m) FILTER (WHERE a.speed_match_frac IS NOT NULL)    / sum(e.length_m)) AS pct_speed,
    round(100.0 * sum(e.length_m) FILTER (WHERE a.class_match_frac IS NOT NULL)    / sum(e.length_m)) AS pct_class,
    round(100.0 * sum(e.length_m) FILTER (WHERE a.sidewalk_match_frac IS NOT NULL) / sum(e.length_m)) AS pct_sidewalk
FROM network.edge_attributes a
JOIN network.edges e ON e.id = a.edge_id
WHERE NOT a.offstreet
GROUP BY a.highway
HAVING sum(e.length_m) > 5000
ORDER BY km DESC;

\echo '--- Speed source ---'
SELECT coalesce(speed_source, 'none') AS speed_source, count(*) AS edges
FROM network.edge_attributes WHERE NOT offstreet GROUP BY 1 ORDER BY 2 DESC;

\echo '--- Unmatched streets, inside vs outside city limits ---'
SELECT
    CASE WHEN in_city THEN 'inside city' ELSE 'buffer (outside city)' END AS location,
    CASE WHEN highway LIKE '%service%' THEN 'service/alley' ELSE 'other streets' END AS kind,
    count(*) AS edges,
    round(sum(length_m)::numeric / 1000) AS km
FROM network.unmatched_streets
GROUP BY 1, 2
ORDER BY 1, 2;

\echo '--- Unmatched non-service streets inside the city, by type ---'
SELECT highway, count(*) AS edges, round(sum(length_m)::numeric / 1000) AS km
FROM network.unmatched_streets
WHERE in_city AND highway NOT LIKE '%service%'
GROUP BY 1 ORDER BY km DESC LIMIT 10;
