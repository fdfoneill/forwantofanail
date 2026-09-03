from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import h3


REQUIRED_STRONGHOLD_COLUMNS = {
    "stronghold_id",
    "location_id",
    "stronghold_name",
    "stronghold_type",
    "control",
    "historical_gloss",
}


class ScenarioCatalogError(ValueError):
    """Raised when immutable scenario map metadata cannot be loaded safely."""


def _resolve_beneath(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    resolved_root = root.resolve()
    if resolved_root not in (candidate, *candidate.parents):
        raise ScenarioCatalogError(f"Scenario path escapes its configured directory: {relative_path!r}")
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioCatalogError(f"Could not read scenario configuration at '{path}'.") from exc
    if not isinstance(payload, dict):
        raise ScenarioCatalogError(f"Scenario configuration must be an object: '{path}'.")
    return payload


def _configured_paths(data_dir: Path) -> tuple[Path, Path, Path, Path]:
    manifest = _read_json(data_dir / "scenario_manifest.json")
    csv_files = manifest.get("csv_files")
    if not isinstance(csv_files, dict) or not csv_files.get("strongholds"):
        raise ScenarioCatalogError("Scenario manifest is missing the strongholds CSV mapping.")
    strongholds_path = _resolve_beneath(data_dir, str(csv_files["strongholds"]))
    stronghold_points_name = manifest.get("stronghold_points")
    if not stronghold_points_name:
        raise ScenarioCatalogError("Scenario manifest is missing the corrected stronghold point layer.")
    stronghold_points_path = _resolve_beneath(data_dir, str(stronghold_points_name))

    history_config_name = manifest.get("history_export_config")
    if not history_config_name:
        raise ScenarioCatalogError("Scenario manifest is missing the history export configuration.")
    history_config = _read_json(_resolve_beneath(data_dir, str(history_config_name)))
    basemap = history_config.get("basemap")
    if not isinstance(basemap, dict) or not basemap.get("path"):
        raise ScenarioCatalogError("History export configuration is missing the georeferenced basemap path.")
    geotiff_path = _resolve_beneath(data_dir, str(basemap["path"]))

    png_target = manifest.get("display_map")
    if not isinstance(png_target, str) or not png_target:
        raise ScenarioCatalogError("Scenario manifest is missing the displayed diegetic map asset.")
    png_path = _resolve_beneath(data_dir, png_target)
    return strongholds_path, stronghold_points_path, geotiff_path, png_path


def build_historical_stronghold_catalog(
    strongholds_path: Path,
    stronghold_points_path: Path,
    geotiff_path: Path,
    png_path: Path,
) -> dict[str, Any]:
    try:
        import rasterio
        import shapefile
        from PIL import Image
        from rasterio.crs import CRS
        from rasterio.warp import transform as transform_coordinates
    except ImportError as exc:
        raise ScenarioCatalogError("Rasterio, Pillow, and PyShp are required for diegetic map annotations.") from exc

    try:
        with strongholds_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or [])
            missing = sorted(REQUIRED_STRONGHOLD_COLUMNS - columns)
            if missing:
                raise ScenarioCatalogError(f"Strongholds CSV is missing required columns: {', '.join(missing)}")
            rows = list(reader)
    except OSError as exc:
        raise ScenarioCatalogError(f"Could not read stronghold history at '{strongholds_path}'.") from exc

    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        component = stronghold_points_path.with_suffix(suffix)
        if not component.exists():
            raise ScenarioCatalogError(f"Corrected stronghold point component is missing: '{component}'.")
    try:
        with shapefile.Reader(str(stronghold_points_path), encoding="utf-8") as point_reader:
            point_fields = {field[0] for field in point_reader.fields[1:]}
            if "GRID_ID" not in point_fields:
                raise ScenarioCatalogError("Corrected stronghold points require a GRID_ID field.")
            point_shape_records = point_reader.shapeRecords()
        point_crs = CRS.from_wkt(stronghold_points_path.with_suffix(".prj").read_text(encoding="utf-8"))
    except ScenarioCatalogError:
        raise
    except Exception as exc:
        raise ScenarioCatalogError(f"Could not read corrected stronghold points at '{stronghold_points_path}'.") from exc

    points_by_location: dict[str, tuple[float, float]] = {}
    point_attributes: dict[str, dict[str, Any]] = {}
    for shape_record in point_shape_records:
        attributes = shape_record.record.as_dict()
        location_id = str(attributes.get("GRID_ID") or "").strip()
        if not location_id:
            raise ScenarioCatalogError("Every corrected stronghold point requires a GRID_ID value.")
        if location_id in points_by_location:
            raise ScenarioCatalogError(f"Duplicate corrected stronghold point for H3 cell: {location_id}")
        if shape_record.shape.shapeType != shapefile.POINT or len(shape_record.shape.points) != 1:
            raise ScenarioCatalogError(f"Corrected stronghold {location_id} must have exactly one point geometry.")
        point_x, point_y = shape_record.shape.points[0]
        points_by_location[location_id] = (float(point_x), float(point_y))
        point_attributes[location_id] = attributes

    try:
        with Image.open(png_path) as image:
            png_width, png_height = image.size
    except OSError as exc:
        raise ScenarioCatalogError(f"Could not read displayed diegetic map at '{png_path}'.") from exc

    seen_ids: set[int] = set()
    catalog_rows: list[dict[str, Any]] = []
    try:
        source = rasterio.open(geotiff_path)
    except Exception as exc:
        raise ScenarioCatalogError(f"Could not open georeferenced map at '{geotiff_path}'.") from exc
    with source:
        if source.crs is None or source.transform.is_identity:
            raise ScenarioCatalogError("The diegetic GeoTIFF lacks a usable CRS or affine transform.")
        if (source.width, source.height) != (png_width, png_height):
            raise ScenarioCatalogError(
                "The diegetic GeoTIFF and displayed PNG must have identical pixel dimensions."
            )
        inverse_transform = ~source.transform
        source_location_ids = {str(row.get("location_id") or "").strip() for row in rows}
        missing_points = sorted(source_location_ids - set(points_by_location))
        extra_points = sorted(set(points_by_location) - source_location_ids)
        if missing_points or extra_points:
            details = []
            if missing_points:
                details.append(f"missing {missing_points}")
            if extra_points:
                details.append(f"unexpected {extra_points}")
            raise ScenarioCatalogError(f"Corrected stronghold points do not match the scenario CSV: {'; '.join(details)}")
        for row in rows:
            try:
                stronghold_id = int(str(row.get("stronghold_id") or "").strip())
            except ValueError as exc:
                raise ScenarioCatalogError("Every historical stronghold requires an integer ID.") from exc
            if stronghold_id in seen_ids:
                raise ScenarioCatalogError(f"Duplicate historical stronghold ID: {stronghold_id}")
            seen_ids.add(stronghold_id)

            location_id = str(row.get("location_id") or "").strip()
            if not h3.is_valid_cell(location_id):
                raise ScenarioCatalogError(f"Historical stronghold {stronghold_id} has an invalid H3 location.")
            name = str(row.get("stronghold_name") or "").strip()
            stronghold_type = str(row.get("stronghold_type") or "").strip()
            historical_faction = str(row.get("control") or "").strip()
            if not name or not stronghold_type or not historical_faction:
                raise ScenarioCatalogError(f"Historical stronghold {stronghold_id} has incomplete display metadata.")
            point_metadata = point_attributes[location_id]
            attribute_checks = {
                "Stronghold": name,
                "Control": historical_faction,
                "Strongho_1": stronghold_type,
            }
            for field_name, expected in attribute_checks.items():
                if field_name in point_metadata and str(point_metadata[field_name] or "").strip() != expected:
                    raise ScenarioCatalogError(
                        f"Corrected stronghold point metadata disagrees with the scenario CSV for {location_id}."
                    )

            point_x, point_y = points_by_location[location_id]
            try:
                projected_x, projected_y = transform_coordinates(
                    point_crs, source.crs, [point_x], [point_y]
                )
                map_x, map_y = inverse_transform * (projected_x[0], projected_y[0])
            except Exception as exc:
                raise ScenarioCatalogError(
                    f"Could not georeference historical stronghold {stronghold_id}."
                ) from exc
            if not (0 <= map_x <= source.width and 0 <= map_y <= source.height):
                raise ScenarioCatalogError(f"Historical stronghold {stronghold_id} falls outside the diegetic map.")

            gloss = str(row.get("historical_gloss") or "").strip() or None
            catalog_rows.append(
                {
                    "id": f"sh_{stronghold_id}",
                    "name": name,
                    "historical_faction": historical_faction,
                    "stronghold_type": stronghold_type,
                    "historical_gloss": gloss,
                    "map_x": float(map_x),
                    "map_y": float(map_y),
                }
            )

    catalog_rows.sort(key=lambda item: int(str(item["id"]).removeprefix("sh_")))
    return {"map_width": png_width, "map_height": png_height, "strongholds": catalog_rows}


def build_historical_stronghold_catalog_for_scenario(
    data_dir: Path,
) -> dict[str, Any]:
    return build_historical_stronghold_catalog(*_configured_paths(data_dir))


@lru_cache(maxsize=1)
def load_historical_stronghold_catalog() -> dict[str, Any]:
    from forwantofanail.core.scenario import get_scenario_package
    return build_historical_stronghold_catalog_for_scenario(get_scenario_package().root)
