"""
Step 01: Fetch the Portland city boundary and the OpenStreetMap walk network,
then load both into PostGIS (schema: raw).

Outputs:
  raw.city_boundary  - city limits (from OSM/Nominatim for now)
  raw.study_area     - city limits + buffer from config
  raw.osm_edges      - walkable OSM segments, with source/target node ids
  raw.osm_nodes      - OSM intersections / endpoints
"""
import os
from pathlib import Path

import osmnx as ox
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())

# Extra OSM tags worth keeping for pedestrian scoring
EXTRA_TAGS = [
    "sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right",
    "footway", "foot", "surface", "lit", "maxspeed", "lanes", "crossing", "incline", "service",
]


def walk_filter():
    """
    Custom version of OSMnx's built-in "walk" filter, with two changes:
      1. Streets tagged sidewalk=separate are KEPT. OSMnx drops them because their
         sidewalks are mapped as separate lines, but this project routes on
         centerlines and drops those sidewalk lines instead (step 02).
      2. Cycleways are KEPT. Many Portland multi-use trails are tagged
         highway=cycleway and are open to walkers. Anything tagged foot=no is
         still excluded.
    """
    return (
        f'["highway"]["area"!~"yes"]{ox.settings.default_access}'
        '["highway"!~"abandoned|bus_guideway|construction|motor|no|planned|'
        'platform|proposed|raceway|razed|rest_area|services"]'
        '["foot"!~"no"]["service"!~"private"]'
    )


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


def flatten_lists(gdf):
    """OSMnx merges tags into lists when it simplifies edges; PostGIS can't store those as-is."""
    for col in gdf.columns:
        if col == "geometry":
            continue
        if gdf[col].apply(lambda v: isinstance(v, list)).any():
            gdf[col] = gdf[col].apply(
                lambda v: ";".join(map(str, v)) if isinstance(v, list) else v
            )
    return gdf


def main():
    crs = CFG["project"]["crs"]
    buf = CFG["project"]["boundary_buffer_m"]

    print("Fetching city boundary...")
    boundary = ox.geocode_to_gdf(CFG["project"]["place_name"])
    boundary_proj = boundary.to_crs(crs)[["geometry"]]

    study_proj = boundary_proj.copy()
    study_proj["geometry"] = study_proj.buffer(buf)
    study_poly_wgs84 = study_proj.to_crs(4326).geometry.union_all()

    print("Downloading OSM walk network (this can take a few minutes)...")
    ox.settings.useful_tags_way = list(set(ox.settings.useful_tags_way + EXTRA_TAGS))
    G = ox.graph_from_polygon(study_poly_wgs84, custom_filter=walk_filter(), simplify=True)
    # OSMnx stores walk networks as two directed edges per street (A->B and B->A).
    # pgRouting handles both directions via reverse_cost, so keep one edge per street.
    G = ox.convert.to_undirected(G)

    nodes, edges = ox.graph_to_gdfs(G)
    nodes = flatten_lists(nodes.reset_index().to_crs(crs))
    edges = edges.reset_index().rename(columns={"u": "source", "v": "target"}).to_crs(crs)
    edges["length_m"] = edges.geometry.length
    edges = flatten_lists(edges)

    engine = get_engine()
    print("Writing to PostGIS...")
    boundary_proj.to_postgis("city_boundary", engine, schema="raw", if_exists="replace", index=False)
    study_proj.to_postgis("study_area", engine, schema="raw", if_exists="replace", index=False)
    nodes.to_postgis("osm_nodes", engine, schema="raw", if_exists="replace", index=False)
    edges.to_postgis("osm_edges", engine, schema="raw", if_exists="replace", index=False)

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE raw.osm_edges ADD COLUMN id bigserial PRIMARY KEY;"))
        conn.execute(text("CREATE INDEX ON raw.osm_edges USING GIST (geometry);"))
        conn.execute(text("CREATE INDEX ON raw.osm_nodes USING GIST (geometry);"))
        n_edges = conn.execute(text("SELECT count(*) FROM raw.osm_edges")).scalar()
        n_nodes = conn.execute(text("SELECT count(*) FROM raw.osm_nodes")).scalar()
        km = conn.execute(text("SELECT round(sum(length_m)/1000) FROM raw.osm_edges")).scalar()

    print(f"Done: {n_edges:,} edges, {n_nodes:,} nodes, ~{km:,.0f} km of walkable network.")


if __name__ == "__main__":
    main()
