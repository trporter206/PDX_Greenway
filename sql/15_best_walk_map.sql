-- =====================================================================
-- Step 15: map layers for "Portland's best walk" (Map 5)
--
-- Views (QGIS: pick "id" as the key when asked):
--   analysis.best_walk_edges_v     corridor edges in order, tagged with their neighborhood
--   analysis.best_walk_sections_v  the corridor split into neighborhood sections, with
--                                  per-section traits for labels and a notes column
--   analysis.best_walk_pois_v      food and shops within 400 m (a 5-minute walk) of the
--                                  corridor, banded by distance: "on the way" (<= 150 m)
--                                  or "short detour" (150-400 m)
-- Table:
--   analysis.best_walk_notes       one row per section: type your own observations into
--                                  "note" in QGIS (attribute table, edit mode); they flow
--                                  into the sections view
--
-- Run:  psql -U postgres -d pdx_greenway -f sql/15_best_walk_map.sql
-- =====================================================================

-- neighborhood of each edge (edges running along a boundary get both names)
DROP VIEW IF EXISTS analysis.best_walk_sections_v;
DROP VIEW IF EXISTS analysis.best_walk_edges_v;
CREATE VIEW analysis.best_walk_edges_v AS
SELECT w.seq AS id, w.seq, w.edge_id, w.part, w.street, w.length_m, w.geom,
       CASE WHEN w.street ILIKE '%bridge%' THEN 'Willamette River crossing'
            ELSE coalesce(
                (SELECT string_agg(initcap(n.name), ' / ' ORDER BY n.name)
                 FROM raw.city_neighborhoods n
                 WHERE ST_Intersects(n.geometry, ST_Buffer(ST_LineInterpolatePoint(w.geom, 0.5), 5))),
                'Other')
       END AS neighborhood
FROM analysis.arbutus_winner_edges w;

-- your notes, one row per neighborhood section (kept when this script is re-run)
CREATE TABLE IF NOT EXISTS analysis.best_walk_notes (
    neighborhood text PRIMARY KEY,
    note text
);
INSERT INTO analysis.best_walk_notes (neighborhood)
SELECT DISTINCT neighborhood FROM analysis.best_walk_edges_v
ON CONFLICT (neighborhood) DO NOTHING;

-- sections: consecutive edges in the same neighborhood, with traits
CREATE VIEW analysis.best_walk_sections_v AS
WITH e AS (
    SELECT *, seq - row_number() OVER (PARTITION BY neighborhood ORDER BY seq) AS grp
    FROM analysis.best_walk_edges_v
),
s AS (
    SELECT neighborhood, min(seq) AS first_seq,
           sum(length_m) AS length_m,
           sum(length_m) FILTER (WHERE part = 'existing') AS existing_m,
           ST_LineMerge(ST_Union(geom)) AS geom,
           array_agg(edge_id) AS edge_ids
    FROM e GROUP BY neighborhood, grp
)
SELECT row_number() OVER (ORDER BY s.first_seq) AS id,
       row_number() OVER (ORDER BY s.first_seq) AS section_no,
       s.neighborhood,
       round(s.length_m::numeric)                                   AS length_m,
       round((100 * coalesce(s.existing_m, 0) / s.length_m)::numeric) AS pct_existing,
       (SELECT count(*) FROM raw.osm_pois p
         WHERE p.category = 'shop' AND ST_DWithin(p.geometry, s.geom, 400))        AS shops,
       (SELECT count(*) FROM raw.osm_pois p
         WHERE p.category IN ('place_of_worship', 'school', 'library', 'community_centre')
           AND ST_DWithin(p.geometry, s.geom, 400))                                AS civic,
       (SELECT count(*) FROM raw.osm_pois p
         WHERE p.category IN ('artwork', 'public_bookcase', 'viewpoint')
           AND ST_DWithin(p.geometry, s.geom, 100))                                AS art_and_curios,
       (SELECT count(*) FROM raw.osm_pois p
         WHERE p.category = 'park' AND ST_DWithin(p.geometry, s.geom, 100))        AS parks,
       (SELECT round((100 * sum(m.canopy_frac * e2.length_m) / sum(e2.length_m))::numeric)
          FROM network.edge_metrics m JOIN network.edges e2 ON e2.id = m.edge_id
         WHERE m.edge_id = ANY(s.edge_ids))                                        AS canopy_pct,
       (SELECT round((100 * sum(m.grade_avg * e2.length_m) / sum(e2.length_m))::numeric, 1)
          FROM network.edge_metrics m JOIN network.edges e2 ON e2.id = m.edge_id
         WHERE m.edge_id = ANY(s.edge_ids))                                        AS grade_pct,
       nt.note,
       s.geom
FROM s
LEFT JOIN analysis.best_walk_notes nt USING (neighborhood);

-- food and shops within a 5-minute walk (400 m, the same radius as the analysis).
-- The corridor follows quiet streets parallel to main streets, so most places are a
-- short detour away rather than right on the path.
DROP VIEW IF EXISTS analysis.best_walk_pois_v;
CREATE VIEW analysis.best_walk_pois_v AS
WITH route AS (SELECT ST_Union(geom) AS g FROM analysis.arbutus_winner_edges)
SELECT row_number() OVER () AS id, p.name, p.kind,
       CASE WHEN p.kind IN ('restaurant', 'fast_food', 'food_court', 'ice_cream') THEN 'Food'
            WHEN p.kind IN ('cafe', 'bar', 'pub') THEN 'Coffee & drinks'
            ELSE 'Shops' END AS poi_group,
       round(ST_Distance(p.geometry, route.g)::numeric) AS distance_m,
       CASE WHEN ST_Distance(p.geometry, route.g) <= 150 THEN 'on the way'
            ELSE 'short detour' END AS band,
       p.geometry
FROM raw.osm_pois p, route
WHERE p.category = 'shop'
  AND ST_DWithin(p.geometry, route.g, 400);

SELECT poi_group, band, count(*) AS places FROM analysis.best_walk_pois_v GROUP BY 1, 2 ORDER BY 1, 2;

SELECT section_no, neighborhood, length_m, pct_existing, shops, civic, art_and_curios,
       parks, canopy_pct, grade_pct
FROM analysis.best_walk_sections_v ORDER BY section_no;
