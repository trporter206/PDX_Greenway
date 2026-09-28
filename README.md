# Portland Walking Greenway

Siting an Arbutus Greenway–style pedestrian network in Portland, OR:
a river-hugging spine plus cross-town connectors linking the city's designated centers.

Stack: PostgreSQL / PostGIS / pgRouting, Python, QGIS.

## Setup
1. `createdb pdx_greenway`
2. `psql -d pdx_greenway -f sql/00_setup.sql`
3. `pip install -r requirements.txt`
4. Set `PGPASSWORD` in your environment
5. `python scripts/01_fetch_osm.py`
6. `psql -U postgres -d pdx_greenway -f sql/02_build_network.sql`

## Pipeline
| Step | Script | Output |
|---|---|---|
| 01 | scripts/01_fetch_osm.py | OSM walk network + city boundary in `raw` |
| 02 | sql/02_build_network.sql | Centerline routing network in `network.edges`; dropped edges and islands for review |
| 03 | scripts/03_fetch_city_data.py | City of Portland layers in `raw.city_*` (use `--list` to find layer names) |
| 04 | sql/04_conflate_attributes.sql | City speed, TSP class, and sidewalk data matched onto OSM edges in `network.edge_attributes` |
| 05 | scripts/05_fetch_elevation.py | USGS 3DEP elevation mosaic in `data/dem/dem_3m.tif` |
| 05b | QGIS (manual) | Metro Canopy 2019 warped to `data/canopy/canopy2019_3m.tif` (see Canopy below) |
| 06 | scripts/06_segment_metrics.py | Slope and canopy per edge in `network.edge_metrics` |
| 07 | scripts/07_score_edges.py | Penalties, impedance, and routing cost in `network.edge_penalties` / `network.edge_costs`; map view `network.edges_scored_v` |
| 08 | scripts/08_route_connectors.py | Center-to-center connector routes and corridor frequency in `analysis.*`; map view `analysis.corridor_use_v` |
| 09 | scripts/09_extract_corridors.py | Continuous connector corridors in `analysis.corridors` (route ends trimmed, parallel streets banded) |
| 10 | scripts/10_connector_network.py | Second routing round with corridors discounted: connected connector network in `analysis.connector_network`; map view `analysis.connector_network_v` |
| 11 | scripts/11_stress_test.py | Re-runs 07-10 under alternative weightings; robustness per street in `analysis.stress_edge_freq`; map view `analysis.stress_v` |
| 12 | scripts/12_validate.py | Network compared with PBOT existing/planned/recommended bikeways and trails; map view `analysis.validation_v` |
| 13 | scripts/13_fetch_osm_context.py | OSM shops, parks, transit for Portland and around Vancouver's Arbutus Greenway (plus its geometry and major roads) |
| 14 | scripts/14_arbutus_compare.py | "Portland's Arbutus": ranked candidate corridors (`analysis.arbutus_candidates`), the winner's existing vs gap sections (`analysis.arbutus_winner_edges`), side-by-side with the Arbutus |
| 15 | sql/15_best_walk_map.sql | Map layers for the best walk: neighborhood sections with traits (`analysis.best_walk_sections_v`), nearby food/shops (`analysis.best_walk_pois_v`), editable notes (`analysis.best_walk_notes`) |

## Canopy preparation (manual step)
Metro's Canopy 2019 is distributed as a very large raster inside a File Geodatabase, so it is
prepared once in QGIS rather than by script:

1. Download **Canopy 2019** (File Geodatabase) from RLIS Discovery and extract it to `data/canopy/`.
2. Raster > Projections > Warp (Reproject) with: target CRS EPSG:26910, nearest neighbour,
   resolution 3 m, extent calculated from `dem_3m.tif`, output type Byte, High Compression.
3. Save as `data/canopy/canopy2019_3m.tif`.

Values are canopy height (0 = no canopy).

*(More steps added as the project progresses.)*
