"""
Step 05: Download USGS 3DEP bare-earth elevation over the study area, in tiles,
and mosaic it into one GeoTIFF (config: elevation.output).

Canopy is prepared separately in QGIS from Metro's Canopy 2019 raster
(see the canopy section of config.yaml and the README).

Usage:
  python scripts/05_fetch_elevation.py
Safe to re-run: tiles already downloaded are skipped.
"""
import math
import os
import sys
import time
from pathlib import Path

# rasterio and pyproj ship their own PROJ database. A PROJ_LIB / PROJ_DATA
# environment variable left by another program (e.g. an older PostGIS install)
# points them at an incompatible copy, so ignore it for this script.
for _var in ("PROJ_LIB", "PROJ_DATA"):
    os.environ.pop(_var, None)

import geopandas as gpd  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
import yaml  # noqa: E402
from rasterio.merge import merge  # noqa: E402
from requests.adapters import HTTPAdapter, Retry  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
CRS = CFG["project"]["crs"]

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET"]))))


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


def main():
    ecfg = CFG["elevation"]
    res, tile_px = ecfg["resolution_m"], ecfg["tile_px"]
    out_path = ROOT / ecfg["output"]
    tile_dir = out_path.parent / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)

    area = gpd.read_postgis("SELECT geometry FROM raw.study_area", get_engine(), geom_col="geometry")
    minx, miny, maxx, maxy = area.total_bounds
    tile_m = res * tile_px
    nx, ny = math.ceil((maxx - minx) / tile_m), math.ceil((maxy - miny) / tile_m)
    print(f"DEM: {nx} x {ny} tiles of {tile_px}px at {res} m")

    tiles = []
    for j in range(ny):
        for i in range(nx):
            x0, y0 = minx + i * tile_m, miny + j * tile_m
            path = tile_dir / f"tile_{i:02d}_{j:02d}.tif"
            tiles.append(path)
            if path.exists() and path.stat().st_size > 0:
                continue
            params = {
                "bbox": f"{x0},{y0},{x0 + tile_m},{y0 + tile_m}",
                "bboxSR": CRS, "imageSR": CRS,
                "size": f"{tile_px},{tile_px}",
                "format": "tiff", "pixelType": "F32",
                "interpolation": "RSP_BilinearInterpolation",
                "f": "image",
            }
            r = session.get(f"{ecfg['service']}/exportImage", params=params, timeout=300)
            r.raise_for_status()
            if not r.headers.get("Content-Type", "").startswith("image"):
                sys.exit(f"Unexpected response for {path.name}: {r.text[:300]}")
            path.write_bytes(r.content)
            print(f"\r  downloaded {len(tiles)}/{nx * ny}", end="", flush=True)
            time.sleep(0.2)
    print()

    print("Mosaicking tiles...")
    sources = [rasterio.open(p) for p in tiles]
    mosaic, transform = merge(sources)
    meta = sources[0].meta.copy()
    meta.update(height=mosaic.shape[1], width=mosaic.shape[2], transform=transform,
                crs=f"EPSG:{CRS}", compress="deflate", tiled=True)
    for s in sources:
        s.close()
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(mosaic)
    print(f"Wrote {out_path} ({mosaic.shape[2]} x {mosaic.shape[1]} px)")


if __name__ == "__main__":
    main()
