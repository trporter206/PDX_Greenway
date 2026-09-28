"""
Step 06: Slope and canopy for every edge in the main network.

Slope (from the 3DEP DEM):
  Elevation is sampled every `metrics.slope_sample_m` along each edge.
    grade_avg  = total climb + descent / length   (hilliness, either direction)
    grade_max  = steepest single sample interval
  Bridges and tunnels: the DEM measures the ground or water underneath, so their
  grade comes from the two endpoint elevations only.

Canopy (from the prepared Metro Canopy 2019 raster):
  Sample lines are placed along the centerline and at `canopy.side_offsets_m`
  on both sides, sampled every `metrics.canopy_sample_m`.
    canopy_frac        = share of samples that are canopy
    canopy_height_mean = mean canopy value where canopy exists (raster units)

Grade clean-up (thresholds in config.yaml `metrics`):
  * bridges/tunnels are capped at bridge_max_grade. OSM often splits a bridge into
    short pieces, and a piece's endpoint can fall into the freeway trench or river
    below (e.g. the streets crossing I-405), producing huge false grades.
  * edges shorter than short_edge_m (not stairs) take the length-weighted grade of
    the edges they connect to; on a few meters, small DEM errors dominate.
  * remaining grades above max_grade (not stairs) are capped.
  Stairs keep their measured grade and are flagged; scoring treats them separately.
  `grade_note` records which rule, if any, changed an edge.

Output: network.edge_metrics, plus the map-ready view network.edges_metrics_v
"""
import os
from pathlib import Path

for _var in ("PROJ_LIB", "PROJ_DATA"):
    os.environ.pop(_var, None)

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import rasterio  # noqa: E402
import shapely  # noqa: E402
import yaml  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


class Grid:
    """A raster held in memory, sampled by coordinates."""

    def __init__(self, path):
        with rasterio.open(path) as src:
            self.arr = src.read(1)
            self.inv = ~src.transform
            self.nodata = src.nodata

    def sample_bilinear(self, xs, ys):
        """Smooth sampling for continuous surfaces like elevation (avoids stair-steps)."""
        cols, rows = self.inv * (np.asarray(xs), np.asarray(ys))
        c, r = cols - 0.5, rows - 0.5          # cell centers
        c0, r0 = np.floor(c).astype(np.int64), np.floor(r).astype(np.int64)
        fc, fr = c - c0, r - r0
        h, w = self.arr.shape
        ok = (r0 >= 0) & (r0 + 1 < h) & (c0 >= 0) & (c0 + 1 < w)
        out = np.full(len(c), np.nan)
        a = self.arr
        r0k, c0k, frk, fck = r0[ok], c0[ok], fr[ok], fc[ok]
        out[ok] = (a[r0k, c0k] * (1 - frk) * (1 - fck) + a[r0k, c0k + 1] * (1 - frk) * fck
                   + a[r0k + 1, c0k] * frk * (1 - fck) + a[r0k + 1, c0k + 1] * frk * fck)
        if self.nodata is not None:
            bad = np.zeros(len(c), bool)
            for dr in (0, 1):
                for dc in (0, 1):
                    bad[ok] |= self.arr[r0k + dr, c0k + dc] == self.nodata
            out[bad] = np.nan
        return out

    def sample(self, xs, ys):
        cols, rows = self.inv * (np.asarray(xs), np.asarray(ys))
        cols = np.floor(cols).astype(np.int64)
        rows = np.floor(rows).astype(np.int64)
        ok = (rows >= 0) & (rows < self.arr.shape[0]) & (cols >= 0) & (cols < self.arr.shape[1])
        out = np.full(len(cols), np.nan)
        out[ok] = self.arr[rows[ok], cols[ok]]
        if self.nodata is not None:
            out[out == self.nodata] = np.nan
        return out


def sample_along(geoms, spacing):
    """Points every `spacing` m along each line, always including both ends.
    Returns (edge_index, distance_along, x, y)."""
    lengths = shapely.length(geoms)
    counts = np.maximum(np.ceil(lengths / spacing).astype(int), 1) + 1
    idx = np.repeat(np.arange(len(geoms)), counts)
    # position k of n points on an edge of length L: k * L / (n - 1)
    starts = np.repeat(np.cumsum(counts) - counts, counts)
    k = np.arange(len(idx)) - starts
    n = counts[idx]
    dist = k * lengths[idx] / (n - 1)
    pts = shapely.line_interpolate_point(geoms[idx], dist)
    xy = shapely.get_coordinates(pts)
    return idx, dist, xy[:, 0], xy[:, 1]


def slope_metrics(geoms, is_bridge, dem, spacing):
    idx, dist, xs, ys = sample_along(geoms, spacing)
    z = dem.sample_bilinear(xs, ys)
    df = pd.DataFrame({"i": idx, "d": dist, "z": z})

    # consecutive pairs on the same edge
    same = df["i"].values[1:] == df["i"].values[:-1]
    dz = np.abs(np.diff(df["z"].values))[same]
    dd = np.diff(df["d"].values)[same]
    pair_i = df["i"].values[1:][same]
    pairs = pd.DataFrame({"i": pair_i, "dz": dz, "dd": dd})
    pairs["grade"] = np.where(pairs["dd"] > 0, pairs["dz"] / pairs["dd"], np.nan)

    agg = pairs.groupby("i").agg(climb=("dz", "sum"), length=("dd", "sum"), grade_max=("grade", "max"))
    agg["grade_avg"] = agg["climb"] / agg["length"]

    ends = df.groupby("i")["z"].agg(elev_start="first", elev_end="last")
    out = ends.join(agg[["grade_avg", "grade_max"]], how="left")

    # bridges and tunnels: endpoint grade only
    lengths = shapely.length(geoms)
    end_grade = (out["elev_end"] - out["elev_start"]).abs() / np.maximum(lengths[out.index], 1)
    b = is_bridge[out.index]
    out.loc[b, "grade_avg"] = end_grade[b]
    out.loc[b, "grade_max"] = end_grade[b]
    return out


def clean_grades(df, mcfg):
    """df columns: edge_id, source, target, length_m, is_bridge, is_steps, grade_avg, grade_max."""
    df = df.copy()
    df["grade_note"] = None

    # 1. bridges and tunnels
    b = df["is_bridge"]
    cap = mcfg["bridge_max_grade"]
    over = b & (df["grade_avg"] > cap)
    df.loc[b, ["grade_avg", "grade_max"]] = df.loc[b, ["grade_avg", "grade_max"]].clip(upper=cap)
    df.loc[over, "grade_note"] = "bridge_capped"

    # 2. short edges borrow from their neighbours
    short = (df["length_m"] < mcfg["short_edge_m"]) & ~df["is_steps"] & ~b
    good = df[~short & ~df["is_steps"]]
    ends = pd.concat([
        good[["edge_id", "source", "length_m", "grade_avg"]].rename(columns={"source": "node"}),
        good[["edge_id", "target", "length_m", "grade_avg"]].rename(columns={"target": "node"}),
    ])
    s = df.loc[short, ["edge_id", "source", "target"]]
    s_nodes = pd.concat([
        s[["edge_id", "source"]].rename(columns={"source": "node"}),
        s[["edge_id", "target"]].rename(columns={"target": "node"}),
    ])
    nb = s_nodes.merge(ends, on="node", suffixes=("", "_nb"))
    nb = nb[nb["edge_id"] != nb["edge_id_nb"]].drop_duplicates(["edge_id", "edge_id_nb"])
    nb["w"] = nb["grade_avg"] * nb["length_m"]
    borrowed = nb.groupby("edge_id").apply(
        lambda g: g["w"].sum() / g["length_m"].sum(), include_groups=False)
    hit = df["edge_id"].isin(borrowed.index)
    df.loc[hit, "grade_avg"] = df.loc[hit, "edge_id"].map(borrowed)
    df.loc[hit, "grade_max"] = df.loc[hit, "grade_avg"]
    df.loc[hit, "grade_note"] = "short_borrowed"

    # 3. implausible leftovers (stairs excepted)
    mx = mcfg["max_grade"]
    over = ~df["is_steps"] & (df["grade_avg"] > mx)
    df.loc[over, "grade_note"] = "capped"
    df.loc[~df["is_steps"], ["grade_avg", "grade_max"]] = (
        df.loc[~df["is_steps"], ["grade_avg", "grade_max"]].clip(upper=mx))
    return df


def canopy_metrics(geoms, canopy, offsets, spacing, min_value):
    variants = [geoms]
    for off in offsets:
        for side in (off, -off):
            variants.append(shapely.offset_curve(geoms, side, quad_segs=2))
    frames = []
    for g in variants:
        g = np.where(shapely.is_empty(g) | shapely.is_missing(g), geoms, g)
        # offset_curve can return MultiLineStrings; sample their vertices after densifying
        dense = shapely.segmentize(g, spacing)
        xy, idx = shapely.get_coordinates(dense, return_index=True)
        v = canopy.sample(xy[:, 0], xy[:, 1])
        frames.append(pd.DataFrame({"i": idx, "v": v}))
    s = pd.concat(frames, ignore_index=True).dropna()
    s["is_canopy"] = s["v"] >= min_value
    frac = s.groupby("i")["is_canopy"].mean().rename("canopy_frac")
    height = s[s["is_canopy"]].groupby("i")["v"].mean().rename("canopy_height_mean")
    return pd.concat([frac, height], axis=1)


def main():
    engine = get_engine()
    with engine.connect() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'raw' AND table_name = 'osm_edges'"))}
    bridge = "r.bridge" if "bridge" in cols else "NULL"
    tunnel = "r.tunnel" if "tunnel" in cols else "NULL"

    print("Loading network...")
    edges = gpd.read_postgis(f"""
        SELECT e.id AS edge_id, e.source, e.target, e.length_m, e.highway,
               e.geom, {bridge}::text AS bridge, {tunnel}::text AS tunnel
        FROM network.edges e
        JOIN raw.osm_edges r ON r.id = e.osm_edge_id
        WHERE e.in_main
    """, engine, geom_col="geom")
    geoms = edges.geometry.values
    flag = lambda s: s.notna() & ~s.fillna("").str.lower().isin(["no", ""])  # noqa: E731
    is_bridge = (flag(edges["bridge"]) | flag(edges["tunnel"])).values
    print(f"  {len(edges):,} edges ({is_bridge.sum():,} bridges/tunnels)")

    print("Slope...")
    dem = Grid(ROOT / CFG["elevation"]["output"])
    slope = slope_metrics(geoms, is_bridge, dem, CFG["metrics"]["slope_sample_m"])
    del dem

    print("Canopy...")
    ccfg = CFG["canopy"]
    canopy = Grid(ROOT / ccfg["raster"])
    shade = canopy_metrics(geoms, canopy, ccfg["side_offsets_m"],
                           CFG["metrics"]["canopy_sample_m"], ccfg["min_value"])
    del canopy

    out = pd.DataFrame({
        "edge_id": edges["edge_id"].values,
        "source": edges["source"].values,
        "target": edges["target"].values,
        "length_m": edges["length_m"].values,
        "is_bridge": is_bridge,
        "is_steps": edges["highway"].fillna("").str.contains("steps").values,
    })
    out = out.join(slope)
    raw_max = out["grade_avg"].max()
    out = clean_grades(out, CFG["metrics"])
    out = out.join(shade).drop(columns=["source", "target", "length_m"])
    out["canopy_frac"] = out["canopy_frac"].fillna(0)

    with engine.begin() as conn:
        # the QGIS view depends on the table, so drop it before replacing the table
        conn.execute(text("DROP VIEW IF EXISTS network.edges_metrics_v"))
    out.to_sql("edge_metrics", engine, schema="network", if_exists="replace", index=False)
    with engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX ON network.edge_metrics (edge_id)"))
        # map-ready view for QGIS (Postgres does the join, so it draws quickly)
        conn.execute(text("""
            CREATE VIEW network.edges_metrics_v AS
            SELECT e.id, e.highway, e.name, e.length_m,
                   m.grade_avg, m.grade_max, m.grade_note, m.is_bridge, m.is_steps,
                   m.canopy_frac, m.canopy_height_mean, e.geom
            FROM network.edges e
            JOIN network.edge_metrics m ON m.edge_id = e.id
        """))

    print("\nSummary")
    print(out[["grade_avg", "grade_max", "canopy_frac", "canopy_height_mean"]]
          .describe(percentiles=[0.5, 0.9, 0.99]).round(3).to_string())
    print(f"\nGrade clean-up (raw max was {raw_max:.2f}):")
    print(out["grade_note"].value_counts().to_string())
    steep = (out["grade_avg"] > 0.08).mean()
    print(f"\nEdges averaging over 8% grade: {steep:.1%}")
    print(f"Edges with no canopy samples at all: {(out['canopy_frac'] == 0).mean():.1%}")


if __name__ == "__main__":
    main()
