"""
Step 03: Download City of Portland layers from PortlandMaps ArcGIS services
into PostGIS (schema: raw, tables prefixed city_).

Usage:
  python scripts/03_fetch_city_data.py --list            # show candidate layer names
  python scripts/03_fetch_city_data.py --list greenway   # search layer names for a keyword
  python scripts/03_fetch_city_data.py                   # download layers not yet in the database
  python scripts/03_fetch_city_data.py --only sidewalks  # (re)download specific tables
  python scripts/03_fetch_city_data.py --force           # re-download everything
  python scripts/03_fetch_city_data.py --profile sidewalks  # show category fields of a table

Layers are fetched in batches by object id, so the services' small record
limits (often 150 per request) don't truncate anything.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely
import yaml
from requests.adapters import HTTPAdapter, Retry
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
CITY = CFG["city_data"]
BASE = CITY["base_url"]

DEFAULT_KEYWORDS = [
    "speed", "tsp", "traffic", "volume", "count", "sidewalk",
    "center", "boundar", "city limit", "greenway", "habitat",
]

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST"]),   # POST isn't retried by default
    raise_on_status=False,
)))


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


def get_json(url, **params):
    params["f"] = "json"
    r = session.get(url, params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{url}: {data['error']}")
    return data


def service_layers(service):
    """Feature layers (not group layers) in a MapServer."""
    info = get_json(f"{BASE}/{service}/MapServer")
    return [l for l in info.get("layers", []) if not l.get("subLayerIds")]


def safe_service_layers(svc):
    try:
        return service_layers(svc)
    except Exception as e:
        print(f"  (skipping {svc}: {e})")
        return []


def list_layers(keywords):
    for svc in CITY["services"]:
        for lyr in safe_service_layers(svc):
            name = lyr["name"]
            if any(k.lower() in name.lower() for k in keywords):
                print(f"{svc:30s} {lyr['id']:>5}  {name}")


def find_layer(name, service=None):
    for svc in ([service] if service else CITY["services"]):
        for lyr in safe_service_layers(svc):
            if lyr["name"].strip().lower() == name.strip().lower():
                return svc, lyr["id"]
    return None, None


def fetch_chunk(query_url, ids, skipped):
    """Fetch one batch of features; if the server chokes, split the batch and try again."""
    try:
        r = session.post(
            query_url,
            data={"objectIds": ",".join(map(str, ids)), "outFields": "*",
                  "outSR": 4326, "f": "geojson"},
            timeout=180,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data.get("features", [])
    except Exception as e:
        if len(ids) == 1:
            skipped.append(ids[0])
            return []
        mid = len(ids) // 2
        return fetch_chunk(query_url, ids[:mid], skipped) + fetch_chunk(query_url, ids[mid:], skipped)


def fetch_layer(service, layer_id):
    layer_url = f"{BASE}/{service}/MapServer/{layer_id}"
    meta = get_json(layer_url)
    batch = min(meta.get("maxRecordCount") or 500, 500)

    ids = get_json(f"{layer_url}/query", where="1=1", returnIdsOnly="true").get("objectIds") or []
    ids.sort()
    print(f"    {len(ids):,} features, {batch} per request")

    frames, skipped = [], []
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        feats = fetch_chunk(f"{layer_url}/query", chunk, skipped)
        if feats:
            frames.append(gpd.GeoDataFrame.from_features(feats, crs=4326))
        done = min(i + batch, len(ids))
        print(f"\r    downloaded {done:,}/{len(ids):,}", end="", flush=True)
        time.sleep(0.1)
    print()
    if skipped:
        print(f"    WARNING: {len(skipped)} features could not be downloaded "
              f"(object ids: {skipped[:10]}{'...' if len(skipped) > 10 else ''})")

    if not frames:
        return None
    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=4326)
    geoms = gdf.geometry.values
    gdf = gdf[~(shapely.is_missing(geoms) | shapely.is_empty(geoms))]
    gdf.columns = [c.lower() for c in gdf.columns]
    return gdf


def profile(df):
    """Print category-like text columns and their most common values."""
    for col in df.columns:
        if col == "geometry":
            continue
        if not (pd.api.types.is_string_dtype(df[col]) or pd.api.types.is_object_dtype(df[col])):
            continue
        vals = df[col].dropna()
        if 1 < vals.nunique() <= 25:
            top = vals.value_counts().head(8)
            print(f"      {col}: " + ", ".join(f"{k} ({v})" for k, v in top.items()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", nargs="*", metavar="KEYWORD",
                        help="list layer names (optionally filtered by keywords) and exit")
    parser.add_argument("--only", nargs="+", metavar="TABLE",
                        help="download only these tables (names from config, e.g. sidewalks)")
    parser.add_argument("--force", action="store_true",
                        help="re-download layers that already exist in the database")
    parser.add_argument("--profile", nargs="+", metavar="TABLE",
                        help="print category fields of already-downloaded tables and exit")
    args = parser.parse_args()

    if args.profile:
        engine = get_engine()
        for table in args.profile:
            print(f"\nraw.city_{table}")
            df = pd.read_sql(f'SELECT * FROM raw."city_{table}"', engine).drop(columns=["geometry"], errors="ignore")
            print(f"    {len(df):,} rows; columns: {', '.join(df.columns)}")
            profile(df)
        return

    if args.list is not None:
        list_layers(args.list or DEFAULT_KEYWORDS)
        return

    crs = CFG["project"]["crs"]
    engine = get_engine()
    missing = []

    existing = set(inspect(engine).get_table_names(schema="raw"))

    for spec in CITY["layers"]:
        table, name = spec["table"], spec["name"]
        if args.only and table not in args.only:
            continue
        if f"city_{table}" in existing and not args.force and not args.only:
            print(f"\n{name}: raw.city_{table} already exists, skipping (use --force to redo)")
            continue
        print(f"\n{name} -> raw.city_{table}")
        svc, lid = find_layer(name, spec.get("service"))
        if svc is None:
            print("    NOT FOUND (check the name with --list)")
            missing.append(name)
            continue
        print(f"    found in {svc}, layer {lid}")
        gdf = fetch_layer(svc, lid)
        if gdf is None or gdf.empty:
            print("    no features returned")
            missing.append(name)
            continue
        gdf = gdf.to_crs(crs)
        gdf.to_postgis(f"city_{table}", engine, schema="raw", if_exists="replace", index=False)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE INDEX ON raw.city_{table} USING GIST (geometry);"))
        print(f"    wrote {len(gdf):,} rows")
        profile(gdf)

    if missing:
        print("\nNot downloaded:", *missing, sep="\n  - ")
        sys.exit(1)
    print("\nAll city layers downloaded.")


if __name__ == "__main__":
    main()
