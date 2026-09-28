"""
Step 13: OpenStreetMap context for the Arbutus comparison, fetched the same way for
Portland and Vancouver so the two can be measured identically.

  raw.osm_pois          Portland study area: shops/food, parks/gardens, transit stops
  raw.arbutus_line      the Arbutus Greenway paths (OSM ways named "Arbutus Greenway")
  raw.arbutus_pois      the same POI categories around the Arbutus Greenway
  raw.arbutus_roads     major roads around the Arbutus Greenway (for crossings)

POI categories (column `category`; column `kind` gives the finer type, e.g. restaurant,
cafe, bar or shop within "shop"):
  shop     any shop=*, or amenity cafe/restaurant/bar/pub/fast_food/ice_cream/food_court
  park     leisure park/garden/playground (polygons kept as polygons)
  transit  bus stops, rail/tram stations and stops, transit platforms
  points of interest (one category each):
    school, place_of_worship, library, community_centre, toilets, drinking_water, bench,
    public_bookcase (free little libraries), artwork (tourism=artwork, incl. murals),
    viewpoint (tourism=viewpoint), community_garden (leisure=garden + garden:type=community)
"""
import os
from pathlib import Path

for _var in ("PROJ_LIB", "PROJ_DATA"):
    os.environ.pop(_var, None)

import geopandas as gpd  # noqa: E402
import osmnx as ox  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402
import yaml  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
A = CFG["arbutus"]
CRS = CFG["project"]["crs"]   # UTM 10N covers Vancouver too

FOOD = ["cafe", "restaurant", "bar", "pub", "fast_food", "ice_cream", "food_court"]
POI_AMENITY = ["school", "place_of_worship", "library", "community_centre", "toilets",
               "drinking_water", "bench", "public_bookcase"]
TAGS = {
    "shop": True,
    "amenity": FOOD + POI_AMENITY,
    "tourism": ["artwork", "viewpoint"],
    "leisure": ["park", "garden", "playground"],
    "highway": ["bus_stop"],
    "railway": ["station", "halt", "tram_stop"],
    "public_transport": ["platform", "station"],
}


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


def col(df, name):
    return df[name] if name in df.columns else pd.Series(None, index=df.index, dtype=object)


def classify(feats):
    """One row per feature with a single category; parks keep polygons, others become points."""
    shop = col(feats, "shop").notna() | col(feats, "amenity").isin(FOOD)
    park = col(feats, "leisure").isin(["park", "garden", "playground"])
    transit = (col(feats, "highway").eq("bus_stop")
               | col(feats, "railway").isin(["station", "halt", "tram_stop"])
               | col(feats, "public_transport").isin(["platform", "station"]))
    masks = [("shop", shop), ("park", park), ("transit", transit)]
    for a in POI_AMENITY:
        masks.append((a, col(feats, "amenity").eq(a)))
    masks.append(("artwork", col(feats, "tourism").eq("artwork")))
    masks.append(("viewpoint", col(feats, "tourism").eq("viewpoint")))
    masks.append(("community_garden", col(feats, "leisure").eq("garden")
                  & col(feats, "garden:type").eq("community")))
    out = []
    for cat, mask in masks:
        sub = feats[mask].copy()
        if sub.empty:
            continue
        if cat != "park":   # everything but parks is counted as a point
            sub["geometry"] = sub.geometry.representative_point()
        sub = sub[sub.geometry.geom_type.isin(["Point", "Polygon", "MultiPolygon"])]
        sub["category"] = cat
        sub["name"] = col(sub, "name")
        # finer type within the category, e.g. restaurant / cafe / shop for "shop"
        if cat == "shop":
            amen = col(sub, "amenity")
            sub["kind"] = amen.where(amen.isin(FOOD), "shop")
        else:
            sub["kind"] = cat
        out.append(sub[["category", "kind", "name", "geometry"]])
    return gpd.GeoDataFrame(pd.concat(out, ignore_index=True), geometry="geometry", crs=feats.crs)


def write(gdf, table, engine):
    gdf = gdf.to_crs(CRS).reset_index(drop=True)
    # step 15's map views depend on these tables; drop them too (step 15 recreates them)
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS raw.{table} CASCADE"))
    gdf.to_postgis(table, engine, schema="raw", if_exists="replace", index=False)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE INDEX ON raw.{table} USING GIST (geometry)"))
    print(f"  raw.{table}: {len(gdf):,} rows")


def main():
    engine = get_engine()

    print("Portland points of interest...")
    area = gpd.read_postgis("SELECT geometry FROM raw.study_area", engine, geom_col="geometry")
    poly = area.to_crs(4326).geometry.union_all()
    pois = classify(ox.features_from_polygon(poly, TAGS))
    write(pois, "osm_pois", engine)
    print(pois["category"].value_counts().to_string())

    print("\nArbutus Greenway geometry...")
    ways = ox.features_from_place(A["place"], {"name": A["osm_name"]})
    ways = ways[ways.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    if ways.empty:
        raise SystemExit(f"No OSM ways named '{A['osm_name']}' found in {A['place']}")
    line = gpd.GeoDataFrame({"name": [A["osm_name"]]},
                            geometry=[shapely.line_merge(ways.geometry.union_all())], crs=ways.crs)
    write(line, "arbutus_line", engine)

    print("\nArbutus context...")
    ctx = line.to_crs(CRS).buffer(A["context_buffer_m"]).to_crs(4326).union_all()
    write(classify(ox.features_from_polygon(ctx, TAGS)), "arbutus_pois", engine)
    roads = ox.features_from_polygon(ctx, {"highway": A["major_roads"]})
    roads = roads[roads.geometry.geom_type.isin(["LineString", "MultiLineString"])]
    roads = gpd.GeoDataFrame({"highway": col(roads, "highway").astype(str), "name": col(roads, "name")},
                             geometry=roads.geometry.values, crs=roads.crs)
    write(roads, "arbutus_roads", engine)


if __name__ == "__main__":
    main()
