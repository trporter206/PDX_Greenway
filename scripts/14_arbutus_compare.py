"""
Step 14: Portland's Arbutus.

1. Candidates: routes through the connector network (step 10) between pairs of centers,
   6-12 km long, spanning at least min_span_km end to end, and not too winding.
2. Traits, measured along each candidate:
     shops_per_km      OSM shops and food places within shop_radius_m
     neighborhoods     Portland neighborhoods the corridor runs through (>= min_neighborhood_m)
     centers           adopted centers within center_radius_m
     grade_avg         length-weighted average grade
     existing_share    share already an existing greenway or trail (step 12, street level)
     gap_km            total length of the stretches that are not (the "work" needed)
     robust_share      share robust at corridor level (step 11)
     parks             parks and gardens within park_radius_m
     transit_per_km    transit stops within transit_radius_m (near-duplicates merged)
     crossings_per_km  crossings of major roads (OSM primary/secondary/trunk)
3. Score: each trait ranked 0-1 across candidates, weighted (config `arbutus.score_weights`).
   "buildable" peaks when existing_share is near buildable_target: a corridor that is part
   built and part gap, i.e. existing sections that some work would join up.
4. Overlapping candidates: the lower-scoring one is dropped.
5. The Arbutus Greenway is measured the same way from OSM (step 13) for the side-by-side.

Outputs (schema analysis):
  arbutus_candidates     top candidates with traits and score
  arbutus_winner_edges   the winning corridor's edges, flagged existing / gap
  arbutus_benchmark      the Arbutus Greenway geometry (for reference maps)
"""
import os
from pathlib import Path

for _var in ("PROJ_LIB", "PROJ_DATA"):
    os.environ.pop(_var, None)

import geopandas as gpd  # noqa: E402
import networkx as nx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402
import yaml  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load(Path(os.environ.get("PDX_CONFIG", ROOT / "config.yaml")).read_text())
A = CFG["arbutus"]
CRS = CFG["project"]["crs"]


def get_engine():
    db = CFG["database"]
    pw = os.environ.get("PGPASSWORD", "")
    return create_engine(
        f"postgresql+psycopg2://{db['user']}:{pw}@{db['host']}:{db['port']}/{db['dbname']}"
    )


# ---------------------------------------------------------------------------
# shared measures (used for Portland candidates and the Arbutus alike)
# ---------------------------------------------------------------------------
def count_within(geom, pts, radius):
    if pts is None or pts.empty:
        return 0
    return int(pts.intersects(geom.buffer(radius)).sum())


def distinct_stops(geom, stops, radius):
    """Transit stops near the line, merging near-duplicates (platform + stop pole etc.)."""
    if stops is None or stops.empty:
        return 0
    near = stops[stops.intersects(geom.buffer(radius))]
    if near.empty:
        return 0
    merged = shapely.union_all(near.geometry.buffer(15).values)
    return len(getattr(merged, "geoms", [merged]))


def crossings(geom, roads):
    """Distinct points where the line crosses a major road (divided roads count once)."""
    if roads is None or roads.empty:
        return 0
    near = roads[roads.intersects(geom.buffer(5))]
    pts = []
    for r in near.geometry:
        inter = geom.intersection(r)
        pts += [g for g in getattr(inter, "geoms", [inter]) if g.geom_type == "Point"]
    if not pts:
        return 0
    merged = shapely.union_all([p.buffer(40) for p in pts])
    return len(getattr(merged, "geoms", [merged]))


# ---------------------------------------------------------------------------
def poi_counts(geom, pois, km):
    """Points of interest per km: passing (right on the path) and nearby (5-minute walk)."""
    P = A["poi"]
    out = {}
    for group, radius in (("passing", P["passing_radius_m"]), ("nearby", P["nearby_radius_m"])):
        zone = geom.buffer(radius)
        for cat in P[group]:
            sub = pois[pois["category"] == cat]
            out[cat] = (int(sub.intersects(zone).sum()) / km) if not sub.empty else 0.0
    return out


def load(engine):
    q = lambda sql: pd.read_sql(sql, engine)  # noqa: E731
    net = gpd.read_postgis("""
        SELECT n.edge_id, e.source, e.target, n.length_m, n.geom, e.name AS street,
               coalesce(v.street_class, 'new') AS street_class,
               coalesce(b.band_share, 0) AS band_share,
               m.grade_avg, m.grade_max, m.canopy_frac
        FROM analysis.connector_network n
        JOIN network.edges e ON e.id = n.edge_id
        LEFT JOIN analysis.validation_edges v ON v.edge_id = n.edge_id
        LEFT JOIN analysis.stress_band_freq b ON b.edge_id = n.edge_id
        LEFT JOIN network.edge_metrics m ON m.edge_id = n.edge_id
    """, engine, geom_col="geom")
    nodes = gpd.read_postgis("""
        SELECT osmid AS node, geometry FROM raw.osm_nodes
        WHERE osmid IN (SELECT e.source FROM analysis.connector_network n JOIN network.edges e ON e.id = n.edge_id
                        UNION SELECT e.target FROM analysis.connector_network n JOIN network.edges e ON e.id = n.edge_id)
    """, engine, geom_col="geometry")
    anchors = gpd.read_postgis("SELECT name, type, center_pt FROM analysis.anchors",
                               engine, geom_col="center_pt")
    pois = gpd.read_postgis("SELECT category, name, geometry FROM raw.osm_pois", engine, geom_col="geometry")
    hoods = gpd.read_postgis('SELECT * FROM raw."city_neighborhoods"', engine, geom_col="geometry")
    name_col = "name" if "name" in hoods.columns else next(
        c for c in hoods.columns if hoods[c].dtype == object or pd.api.types.is_string_dtype(hoods[c]))
    hoods = hoods.rename(columns={name_col: "hood"})[["hood", "geometry"]]
    majors = gpd.read_postgis(f"""
        SELECT name, geometry FROM raw.osm_edges
        WHERE split_part(highway, ';', 1) = ANY(ARRAY{A['major_roads']}::text[])
    """, engine, geom_col="geometry")
    return net, nodes, anchors, pois, hoods, majors


def candidates(net, nodes, anchors):
    G = nx.Graph()
    for r in net.itertuples():
        G.add_edge(r.source, r.target, weight=r.length_m, edge_id=r.edge_id)
    nodes = nodes[nodes["node"].isin(G.nodes)]
    tree = shapely.STRtree(nodes.geometry.values)
    snaps = []
    for a in anchors.itertuples():
        i = tree.nearest(a.center_pt)
        d = a.center_pt.distance(nodes.geometry.values[i])
        if d <= 1000:
            snaps.append((a.name, nodes["node"].values[i], a.center_pt))
    comps = {n: c for c, comp in enumerate(nx.connected_components(G)) for n in comp}
    out = []
    for i in range(len(snaps)):
        for j in range(i + 1, len(snaps)):
            (na, a, pa), (nb, b, pb) = snaps[i], snaps[j]
            span = pa.distance(pb)
            if a == b or comps[a] != comps[b] or span < A["min_span_km"] * 1000:
                continue
            path = nx.shortest_path(G, a, b, weight="weight")
            eids = [G[u][v]["edge_id"] for u, v in zip(path[:-1], path[1:])]
            out.append((na, nb, span, eids))
    return out


def traits(eids, span, ends, net_idx, anchors, pois, hoods, majors):
    e = net_idx.loc[eids]
    L = e["length_m"].sum()
    geom = shapely.line_merge(shapely.union_all(e.geometry.values))
    exist = e["street_class"].eq("existing_greenway").values
    # gaps: runs of consecutive non-existing edges along the path
    runs, cur = [], 0.0
    for is_ex, length in zip(exist, e["length_m"].values):
        if is_ex:
            if cur:
                runs.append(cur)
            cur = 0.0
        else:
            cur += length
    if cur:
        runs.append(cur)
    runs = [r for r in runs if r >= 200]
    inside = hoods.geometry.intersection(geom).length
    shops = pois[pois["category"] == "shop"]
    parks = pois[pois["category"] == "park"]
    stops = pois[pois["category"] == "transit"]
    return {
        "length_km": L / 1000,
        "span_km": span / 1000,
        "sinuosity": L / span,
        "grade_avg": np.average(e["grade_avg"].fillna(0), weights=e["length_m"]),
        "grade_max": e["grade_max"].max(),
        "canopy": np.average(e["canopy_frac"].fillna(0), weights=e["length_m"]),
        "existing_share": e.loc[exist, "length_m"].sum() / L,
        "gaps": len(runs),
        "gap_km": sum(runs) / 1000,
        "robust_share": e.loc[e["band_share"] >= 0.8, "length_m"].sum() / L,
        # the two end centers count even though the network stops short of them (trimmed ends)
        "centers": len(set(ends) | set(anchors.loc[anchors.intersects(geom.buffer(A["center_radius_m"])), "name"])),
        "center_names": ", ".join(sorted(set(ends) | set(
            anchors.loc[anchors.intersects(geom.buffer(A["center_radius_m"])), "name"]))),
        "neighborhoods": int((inside >= A["min_neighborhood_m"]).sum()),
        "hood_names": ", ".join(hoods.loc[inside >= A["min_neighborhood_m"], "hood"].astype(str)),
        "shops_per_km": count_within(geom, shops, A["shop_radius_m"]) / (L / 1000),
        "parks": count_within(geom, parks, A["park_radius_m"]),
        "transit_per_km": distinct_stops(geom, stops, A["transit_radius_m"]) / (L / 1000),
        "crossings_per_km": crossings(geom, majors) / (L / 1000),
        "geom": geom,
    }


def score(df):
    rank = lambda s: s.rank(pct=True)  # noqa: E731
    w = A["score_weights"]
    parts = {
        "shops": rank(df["shops_per_km"]),
        "neighborhoods": rank(df["neighborhoods"]),
        "centers": rank(df["centers"]),
        "flatness": rank(-df["grade_avg"]),
        "buildable": rank(-(df["existing_share"] - A["buildable_target"]).abs()),
        "robust": rank(df["robust_share"]),
        "parks": rank(df["parks"] / df["length_km"]),
        "transit": rank(df["transit_per_km"]),
        "few_crossings": rank(-df["crossings_per_km"]),
    }
    total = sum(w[k] * v for k, v in parts.items()) / sum(w[k] for k in parts)
    return total


def arbutus(engine):
    insp = inspect(engine)
    if not insp.has_table("arbutus_line", schema="raw"):
        return None, None
    line = gpd.read_postgis("SELECT geometry FROM raw.arbutus_line", engine, geom_col="geometry")
    pois = gpd.read_postgis("SELECT category, geometry FROM raw.arbutus_pois", engine, geom_col="geometry")
    roads = gpd.read_postgis("SELECT geometry FROM raw.arbutus_roads", engine, geom_col="geometry")
    geom = line.geometry.union_all()
    # OSM maps the walking and cycling paths separately; estimate the corridor length
    # from a wide buffer so parallel paths aren't double counted
    est_km = geom.buffer(100).area / 200 / 1000
    km = A["official_length_km"]
    shops = pois[pois["category"] == "shop"]
    parks = pois[pois["category"] == "park"]
    stops = pois[pois["category"] == "transit"]
    return {
        "length_km": km,
        "length_est_km": est_km,
        "neighborhoods": len(A["neighbourhoods"]),
        "shops_per_km": count_within(geom, shops, A["shop_radius_m"]) / km,
        "parks": count_within(geom, parks, A["park_radius_m"]),
        "transit_per_km": distinct_stops(geom, stops, A["transit_radius_m"]) / km,
        "crossings_per_km": crossings(geom, roads) / km,
    }, line


def main():
    engine = get_engine()
    net, nodes, anchors, pois, hoods, majors = load(engine)
    net_idx = net.set_index("edge_id")
    cands = candidates(net, nodes, anchors)
    rows = []
    for na, nb, span, eids in cands:
        L = net_idx.loc[eids, "length_m"].sum() / 1000
        if not (A["min_length_km"] <= L <= A["max_length_km"]) or L * 1000 / span > A["max_sinuosity"]:
            continue
        t = traits(eids, span, (na, nb), net_idx, anchors, pois, hoods, majors)
        t.update({"from_center": na, "to_center": nb, "edge_ids": eids})
        rows.append(t)
    if not rows:
        raise SystemExit("No candidates met the length/span/sinuosity limits; loosen them in config.yaml")
    df = pd.DataFrame(rows)
    df["score"] = score(df)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)

    # drop candidates that mostly repeat a better one
    kept, kept_sets = [], []
    for i, r in df.iterrows():
        s = set(r["edge_ids"])
        L = net_idx.loc[list(s), "length_m"]
        if all(L[L.index.isin(k)].sum() / min(L.sum(), net_idx.loc[list(k), "length_m"].sum())
               <= A["max_overlap"] for k in kept_sets):
            kept.append(i)
            kept_sets.append(s)
    top = df.loc[kept].head(10).reset_index(drop=True)
    top.insert(0, "rank", range(1, len(top) + 1))
    print(f"{len(df)} candidates met the limits; {len(kept)} distinct after removing overlaps\n")

    out = gpd.GeoDataFrame(top.drop(columns=["edge_ids"]), geometry="geom", crs=CRS)
    for c in out.columns:
        if out[c].dtype == float:
            out[c] = out[c].round(3)
    out.to_postgis("arbutus_candidates", engine, schema="analysis", if_exists="replace", index=False)

    w = top.iloc[0]
    wedges = net_idx.loc[w["edge_ids"]].reset_index()[["edge_id", "street", "street_class", "length_m", "geom"]]
    wedges.insert(0, "seq", range(1, len(wedges) + 1))   # order along the route
    wedges["part"] = np.where(wedges["street_class"].eq("existing_greenway"), "existing", "gap")
    gpd.GeoDataFrame(wedges, geometry="geom", crs=CRS).to_postgis(
        "arbutus_winner_edges", engine, schema="analysis", if_exists="replace", index=False)

    show = ["rank", "from_center", "to_center", "length_km", "shops_per_km", "neighborhoods",
            "centers", "grade_avg", "existing_share", "gap_km", "robust_share", "score"]
    print("Top candidates:")
    print(top[show].round(2).to_string(index=False))

    ab, line = arbutus(engine)
    if line is not None:
        line.to_postgis("arbutus_benchmark", engine, schema="analysis", if_exists="replace", index=False)
    print(f"\nWinner: {w['from_center']} to {w['to_center']}")
    print(f"  neighborhoods: {w['hood_names']}")
    print(f"  centers passed: {w['center_names']}")
    # points of interest along the winner and the Arbutus
    P = A["poi"]
    w_poi = poi_counts(w["geom"], pois, w["length_km"])
    a_poi = None
    if line is not None:
        a_pois = gpd.read_postgis("SELECT category, geometry FROM raw.arbutus_pois", engine, geom_col="geometry")
        a_poi = poi_counts(line.geometry.union_all(), a_pois, A["official_length_km"])
    label = {"bench": "benches", "drinking_water": "drinking fountains",
             "public_bookcase": "free little libraries", "artwork": "public art",
             "school": "schools", "place_of_worship": "places of worship", "library": "libraries",
             "community_centre": "community centres", "community_garden": "community gardens",
             "toilets": "public toilets", "viewpoint": "viewpoints"}
    rows = []
    for group in ("passing", "nearby"):
        radius = P[f"{group}_radius_m"]
        for cat in P[group]:
            rows.append({"per km": f"{label.get(cat, cat)} (within {radius} m)",
                         "Portland candidate": w_poi[cat],
                         "Arbutus Greenway": a_poi[cat] if a_poi else None})
    poi_table = pd.DataFrame(rows).set_index("per km")

    comp = pd.DataFrame({
        "Portland candidate": {
            "length (km)": w["length_km"], "neighborhoods": w["neighborhoods"],
            "shops & food per km": w["shops_per_km"], "parks & gardens": w["parks"],
            "transit stops per km": w["transit_per_km"], "major-road crossings per km": w["crossings_per_km"],
            "average grade": w["grade_avg"], "already greenway/trail": w["existing_share"],
            "tree canopy over the route": w["canopy"],
        },
        "Arbutus Greenway": {
            "length (km)": ab["length_km"] if ab else None,
            "neighborhoods": ab["neighborhoods"] if ab else None,
            "shops & food per km": ab["shops_per_km"] if ab else None,
            "parks & gardens": ab["parks"] if ab else None,
            "transit stops per km": ab["transit_per_km"] if ab else None,
            "major-road crossings per km": ab["crossings_per_km"] if ab else None,
            "average grade": "gentle (former rail grade)",
            "already greenway/trail": 1.0,
            "tree canopy over the route": "(not comparable: Portland-only data)",
        },
    })
    # the winner, section by section: consecutive edges of the same kind on the same street
    wedges["street"] = wedges["street"].fillna("(unnamed path/street)")
    brk = (wedges["part"] != wedges["part"].shift()) | (wedges["street"] != wedges["street"].shift())
    sections = (wedges.assign(section=brk.cumsum())
                .groupby("section")
                .agg(part=("part", "first"), street=("street", "first"), m=("length_m", "sum")))
    sections = sections[sections["m"] >= 100]
    print("\nWinner, section by section (from first center to second; stretches under 100 m omitted):")
    print(sections.assign(m=sections["m"].round()).to_string(index=False))

    geom_w = w["geom"]
    near = majors[majors.intersects(geom_w.buffer(5))]
    crossed = sorted({str(r.name) for r in near.itertuples()
                      if r.name and any(g.geom_type == "Point"
                                        for g in getattr(geom_w.intersection(r.geometry), "geoms",
                                                         [geom_w.intersection(r.geometry)]))})
    print("\nMajor roads the winner crosses: " + (", ".join(crossed) if crossed else "(none named)"))

    fmt = lambda v: f"{v:.2f}" if isinstance(v, (float, np.floating)) else v  # noqa: E731
    print("\nSide by side (same OSM-based measures):")
    print(comp.map(fmt).to_string())
    print("\nPoints of interest per km (OSM; mapping completeness can differ between cities):")
    print(poi_table.map(lambda v: f"{v:.2f}" if isinstance(v, (float, np.floating)) else v).to_string())
    if ab:
        print(f"\n(Arbutus length estimated from OSM geometry: {ab['length_est_km']:.1f} km; "
              f"official {ab['length_km']} km used for per-km figures)")


if __name__ == "__main__":
    main()
