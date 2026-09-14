from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import deque
from pathlib import Path
from typing import Any

import h3



ALGORITHM_VERSION = "current-flow-atlas-v1"
CITY_TYPE = "city"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _neighbors(cell: str, known: set[str]) -> list[str]:
    try:
        return sorted(str(value) for value in h3.grid_ring(cell, 1) if str(value) in known)
    except Exception:
        return []


def _bearing(origin: str, destination: str) -> str:
    if origin == destination:
        return "here"
    lat1, lon1 = h3.cell_to_latlng(origin)
    lat2, lon2 = h3.cell_to_latlng(destination)
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1))
    angle = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")[int((angle + 22.5) // 45) % 8]


def _components(graph: dict[str, set[str]]) -> list[list[str]]:
    unseen = set(graph)
    result: list[list[str]] = []
    while unseen:
        root = min(unseen)
        queue = deque([root])
        unseen.remove(root)
        component = []
        while queue:
            node = queue.popleft()
            component.append(node)
            for adjacent in sorted(graph[node]):
                if adjacent in unseen:
                    unseen.remove(adjacent)
                    queue.append(adjacent)
        result.append(sorted(component))
    return sorted(result, key=lambda row: (-len(row), row[0]))


def _current_flow_scores(graph: dict[str, set[str]]) -> dict[str, float]:
    """Exact current-flow node betweenness, with a deterministic dense fallback.

    NetworkX is the declared build dependency. The fallback keeps scenario
    authoring usable in stripped development interpreters.
    """
    try:
        import networkx as nx
    except ImportError:  # pragma: no cover - exercised only in minimal authoring envs
        nx = None
    scores = {node: 0.0 for node in graph}
    components = _components(graph)
    total_pairs = sum(len(c) * (len(c) - 1) / 2 for c in components) or 1.0
    for component in components:
        if len(component) < 3:
            continue
        pair_share = (len(component) * (len(component) - 1) / 2) / total_pairs
        if nx is not None:
            subgraph = nx.Graph()
            subgraph.add_nodes_from(component)
            subgraph.add_edges_from(
                (node, adjacent) for node in component for adjacent in graph[node] if node < adjacent
            )
            local = nx.current_flow_betweenness_centrality(
                subgraph, normalized=True, weight=None, solver="lu"
            )
        else:
            # Electrical-flow fallback based on the Laplacian pseudoinverse.
            import numpy as np
            index = {node: pos for pos, node in enumerate(component)}
            laplacian = np.zeros((len(component), len(component)), dtype=float)
            for node in component:
                i = index[node]
                laplacian[i, i] = len(graph[node])
                for adjacent in graph[node]:
                    if adjacent in index:
                        laplacian[i, index[adjacent]] = -1.0
            # Ground the final node, invert the reduced Laplacian, and use the
            # same edge-flow accumulation identity as NetworkX. This is exact
            # for the unweighted graph but avoids enumerating all node pairs.
            inverse = np.zeros_like(laplacian)
            inverse[:-1, :-1] = np.linalg.inv(laplacian[:-1, :-1])
            values = np.zeros(len(component), dtype=float)
            for left in component:
                for right in sorted(graph[left]):
                    if right not in index or index[left] >= index[right]:
                        continue
                    row = inverse[index[left], :] - inverse[index[right], :]
                    positions = np.empty(len(component), dtype=int)
                    positions[np.argsort(row)[::-1]] = np.arange(1, len(component) + 1)
                    for pos in range(len(component)):
                        values[index[left]] += (pos + 1 - positions[pos]) * row[pos]
                        values[index[right]] += (len(component) - pos - positions[pos]) * row[pos]
            denominator = (len(component) - 1.0) * (len(component) - 2.0)
            local = {
                node: max(0.0, float((values[pos] - pos) * 2.0 / denominator))
                for node, pos in index.items()
            }
        for node, value in local.items():
            scores[node] = float(value) * pair_share
    return scores


def _shortest(graph: dict[str, set[str]], start: str, end: str) -> list[str] | None:
    queue = deque([start])
    previous: dict[str, str | None] = {start: None}
    while queue:
        node = queue.popleft()
        if node == end:
            path = []
            while node is not None:
                path.append(node)
                node = previous[node]
            return list(reversed(path))
        for adjacent in sorted(graph[node]):
            if adjacent not in previous:
                previous[adjacent] = node
                queue.append(adjacent)
    return None


def generate_atlas(data_dir: Path | None = None) -> dict[str, Any]:
    if data_dir is None:
        from forwantofanail.core.scenario import get_scenario_package
        data_dir = get_scenario_package().root
    manifest_path = data_dir / "scenario_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    csv_files = manifest["csv_files"]
    location_path = data_dir / csv_files["locations"]
    stronghold_path = data_dir / csv_files["strongholds"]
    locations = _rows(location_path)
    strongholds = _rows(stronghold_path)
    location_ids = {row["location_id"] for row in locations}
    road_cells = {row["location_id"] for row in locations if row["is_road"].strip().upper() in {"TRUE", "1", "YES"}}
    graph: dict[str, set[str]] = {cell: set() for cell in road_cells}
    for cell in sorted(road_cells):
        for adjacent in _neighbors(cell, road_cells):
            graph[cell].add(adjacent)
    stronghold_nodes: dict[str, str] = {}
    by_node: dict[str, dict[str, str]] = {}
    for row in strongholds:
        node = f"stronghold:{row['stronghold_id']}"
        stronghold_nodes[row["stronghold_id"]] = node
        by_node[node] = row
        graph[node] = set()
        cell = row["location_id"]
        connected = ({cell} if cell in road_cells else set()) | set(_neighbors(cell, road_cells))
        for road in connected:
            graph[node].add(road)
            graph[road].add(node)
    scores = _current_flow_scores(graph)
    scored = []
    for row in strongholds:
        node = stronghold_nodes[row["stronghold_id"]]
        neighborhood = {node} | graph[node]
        scored.append((max(scores.get(item, 0.0) for item in neighborhood), int(row["stronghold_id"]), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    output_path = data_dir / str(manifest["agent_strategic_atlas"])
    old_selected: set[str] = set()
    if output_path.exists():
        try:
            old_selected = {
                str(row["stronghold_ref"]) for row in json.loads(output_path.read_text()).get("choke_point_candidates", []) if row.get("selected")
            }
        except (ValueError, TypeError, KeyError):
            pass
    candidates = []
    for rank, (score, sid, row) in enumerate((item for item in scored if item[2]["stronghold_type"].casefold() != CITY_TYPE), 1):
        if len(candidates) == 20:
            break
        ref = f"sh_{sid}"
        candidates.append({
            "stronghold_ref": ref, "name": row["stronghold_name"], "type": row["stronghold_type"],
            "historical_faction": row["control"], "rank": rank, "score": round(score, 10),
            "selected": ref in old_selected,
        })
    selected = {row["stronghold_ref"] for row in candidates if row["selected"]}
    majors = []
    for score, sid, row in sorted(scored, key=lambda item: item[1]):
        ref = f"sh_{sid}"
        if row["stronghold_type"].casefold() == CITY_TYPE or ref in selected:
            majors.append({
                "stronghold_ref": ref, "name": row["stronghold_name"], "type": row["stronghold_type"],
                "historical_faction": row["control"], "role": "city" if row["stronghold_type"].casefold() == CITY_TYPE else "reviewed_choke_point",
                "centrality_score": round(score, 10),
            })
    rows_by_ref = {f"sh_{int(row['stronghold_id'])}": row for row in strongholds}
    major_refs = {row["stronghold_ref"] for row in majors}
    corridors = []
    for index, left in enumerate(majors):
        for right in majors[index + 1:]:
            left_node = stronghold_nodes[left["stronghold_ref"].split("_")[1]]
            right_node = stronghold_nodes[right["stronghold_ref"].split("_")[1]]
            path = _shortest(graph, left_node, right_node)
            if not path:
                continue
            internal_major = {
                f"sh_{int(by_node[node]['stronghold_id'])}" for node in path[1:-1] if node in by_node
            } & major_refs
            if internal_major:
                continue
            intermediate = [
                f"sh_{int(by_node[node]['stronghold_id'])}" for node in path[1:-1] if node in by_node
            ]
            transitions = []
            faction_sequence = [left["historical_faction"]] + [rows_by_ref[ref]["control"] for ref in intermediate] + [right["historical_faction"]]
            for faction in faction_sequence:
                if not transitions or transitions[-1] != faction:
                    transitions.append(faction)
            left_cell = rows_by_ref[left["stronghold_ref"]]["location_id"]
            next_cell = next((node for node in path[1:] if node in location_ids), rows_by_ref[right["stronghold_ref"]]["location_id"])
            corridors.append({
                "from_ref": left["stronghold_ref"], "from": left["name"], "to_ref": right["stronghold_ref"], "to": right["name"],
                "distance_leagues": max(0, len(path) - 1), "initial_bearing": _bearing(left_cell, next_cell),
                "intermediate_stronghold_refs": intermediate, "historical_faction_transitions": transitions,
                "frontier_crossing": len(transitions) > 1,
            })
    faction_cells: dict[str, list[str]] = {}
    for row in strongholds:
        faction_cells.setdefault(row["control"], []).append(row["location_id"])
    faction_regions = []
    for faction, cells in sorted(faction_cells.items()):
        lat = sum(h3.cell_to_latlng(cell)[0] for cell in cells) / len(cells)
        lon = sum(h3.cell_to_latlng(cell)[1] for cell in cells) / len(cells)
        representative = min(cells, key=lambda cell: (h3.great_circle_distance(h3.cell_to_latlng(cell), (lat, lon)), cell))
        faction_regions.append({"faction": faction, "centroid_stronghold_ref": f"sh_{int(next(row['stronghold_id'] for row in strongholds if row['location_id'] == representative))}"})
    for region in faction_regions:
        origin_cell = rows_by_ref[region["centroid_stronghold_ref"]]["location_id"]
        region["relationships"] = [
            {
                "faction": other["faction"],
                "direction": _bearing(origin_cell, rows_by_ref[other["centroid_stronghold_ref"]]["location_id"]),
            }
            for other in faction_regions if other["faction"] != region["faction"]
        ]
    all_cells = set(location_ids)
    edge_cells = {cell for cell in all_cells if len(_neighbors(cell, all_cells)) < 6}
    edge_context = []
    for major in majors:
        cell = rows_by_ref[major["stronghold_ref"]]["location_id"]
        distance = min((h3.grid_distance(cell, edge) for edge in edge_cells), default=99)
        if distance <= 8:
            nearest = min(edge_cells, key=lambda edge: (h3.grid_distance(cell, edge), edge))
            edge_context.append({"stronghold_ref": major["stronghold_ref"], "name": major["name"], "distance_leagues": distance, "direction": _bearing(cell, nearest)})
    edge_road_terminations = []
    stronghold_cells = [(f"sh_{int(row['stronghold_id'])}", row["stronghold_name"], row["location_id"]) for row in strongholds]
    for terminal in sorted(road_cells):
        if len(_neighbors(terminal, road_cells)) > 1:
            continue
        edge_distance = min((h3.grid_distance(terminal, edge) for edge in edge_cells), default=99)
        if edge_distance > 2:
            continue
        ref, name, anchor = min(stronghold_cells, key=lambda item: (h3.grid_distance(terminal, item[2]), item[0]))
        edge_road_terminations.append({
            "nearest_stronghold_ref": ref, "nearest_stronghold": name,
            "direction": _bearing(anchor, terminal),
            "distance_from_stronghold_leagues": int(h3.grid_distance(anchor, terminal)),
        })
    source_hashes = {"locations.csv": _sha(location_path), "strongholds.csv": _sha(stronghold_path)}
    payload = {
        "schema_version": 1, "algorithm_version": ALGORITHM_VERSION,
        "source_hashes": source_hashes,
        "graph": {"component_sizes": [len(row) for row in _components(graph)]},
        "faction_regions": faction_regions, "major_strongholds": majors,
        "choke_point_candidates": candidates, "corridors": sorted(corridors, key=lambda row: (row["from_ref"], row["to_ref"])),
        "edge_context": edge_context, "edge_road_terminations": edge_road_terminations,
    }
    payload["artifact_hash"] = hashlib.sha256(_canonical({key: value for key, value in payload.items() if key != "artifact_hash"}).encode()).hexdigest()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the scenario strategic atlas used by agent commanders.")
    parser.add_argument("--scenario-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    from forwantofanail.core.scenario import load_scenario_package
    package = load_scenario_package(args.scenario_dir)
    generated = generate_atlas(package.root)
    output = package.resolve("agent_strategic_atlas")
    if args.check:
        if not output.exists():
            raise SystemExit("Strategic atlas is missing; run the generator.")
        existing = json.loads(output.read_text())
        missing = {row["stronghold_ref"] for row in existing.get("choke_point_candidates", []) if row.get("selected")} - {row["stronghold_ref"] for row in generated["choke_point_candidates"]}
        if missing:
            raise SystemExit(f"Selected choke points disappeared: {', '.join(sorted(missing))}")
        if existing.get("source_hashes") != generated.get("source_hashes") or existing.get("artifact_hash") != generated.get("artifact_hash"):
            raise SystemExit("Strategic atlas is stale; regenerate and review it.")
        return
    output.write_text(json.dumps(generated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output}; review choke_point_candidates and set selected=true where appropriate.")


if __name__ == "__main__":
    main()
