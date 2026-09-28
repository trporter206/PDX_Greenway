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

## Canopy preparation (manual step)
Metro's Canopy 2019 is distributed as a very large raster inside a File Geodatabase, so it is
prepared once in QGIS rather than by script:

1. Download **Canopy 2019** (File Geodatabase) from RLIS Discovery and extract it to `data/canopy/`.
2. Raster > Projections > Warp (Reproject) with: target CRS EPSG:26910, nearest neighbour,
   resolution 3 m, extent calculated from `dem_3m.tif`, output type Byte, High Compression.
3. Save as `data/canopy/canopy2019_3m.tif`.

Values are canopy height (0 = no canopy).

*(More steps added as the project progresses.)*
