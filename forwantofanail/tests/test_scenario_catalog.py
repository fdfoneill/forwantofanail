from __future__ import annotations

import csv

import h3
import pytest

from forwantofanail.core.scenario_catalog import (
    ScenarioCatalogError,
    build_historical_stronghold_catalog,
)


def _write_catalog_fixture(tmp_path, *, duplicate: bool = False, png_size: tuple[int, int] = (200, 100)):
    Image = pytest.importorskip("PIL.Image")
    rasterio = pytest.importorskip("rasterio")
    shapefile = pytest.importorskip("shapefile")
    center = h3.latlng_to_cell(41.0, 29.0, 7)
    second_center = sorted(h3.grid_ring(center, 1))[0]
    latitude, longitude = h3.cell_to_latlng(center)
    csv_path = tmp_path / "strongholds.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "stronghold_id",
                "location_id",
                "stronghold_name",
                "stronghold_type",
                "control",
                "stronghold_threshold",
                "historical_gloss",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "stronghold_id": 7,
                "location_id": center,
                "stronghold_name": "Old Keep",
                "stronghold_type": "Fortress",
                "control": "Founders",
                "stronghold_threshold": 20,
                "historical_gloss": "  Raised above the ancient pass.  ",
            }
        )
        writer.writerow(
            {
                "stronghold_id": 7 if duplicate else 8,
                "location_id": second_center,
                "stronghold_name": "Empty Gloss",
                "stronghold_type": "Town",
                "control": "Founders",
                "stronghold_threshold": 10,
                "historical_gloss": "",
            }
        )

    shp_path = tmp_path / "stronghold_points.shp"
    with shapefile.Writer(str(shp_path), shapeType=shapefile.POINT, encoding="utf-8") as writer:
        writer.field("GRID_ID", "C", size=17)
        writer.field("Stronghold", "C", size=100)
        writer.field("Control", "C", size=30)
        writer.field("Strongho_1", "C", size=30)
        writer.point(longitude + 0.2, latitude - 0.1)
        writer.record(center, "Old Keep", "Founders", "Fortress")
        second_latitude, second_longitude = h3.cell_to_latlng(second_center)
        writer.point(second_longitude, second_latitude)
        writer.record(second_center, "Empty Gloss", "Founders", "Town")
    shp_path.with_suffix(".prj").write_text(rasterio.crs.CRS.from_epsg(4326).to_wkt(), encoding="utf-8")

    tif_path = tmp_path / "map.tif"
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        width=200,
        height=100,
        count=3,
        dtype="uint8",
        crs="EPSG:4326",
        transform=rasterio.transform.from_bounds(
            longitude - 1,
            latitude - 1,
            longitude + 1,
            latitude + 1,
            200,
            100,
        ),
    ):
        pass
    png_path = tmp_path / "map.png"
    Image.new("RGB", png_size, "white").save(png_path)
    return csv_path, shp_path, tif_path, png_path


def test_historical_catalog_parses_glosses_and_georeferences_h3_centroids(tmp_path):
    paths = _write_catalog_fixture(tmp_path)
    catalog = build_historical_stronghold_catalog(*paths)

    assert catalog["map_width"] == 200
    assert catalog["map_height"] == 100
    assert [row["id"] for row in catalog["strongholds"]] == ["sh_7", "sh_8"]
    assert catalog["strongholds"][0] == {
        "id": "sh_7",
        "name": "Old Keep",
        "historical_faction": "Founders",
        "stronghold_type": "Fortress",
        "historical_gloss": "Raised above the ancient pass.",
        "map_x": pytest.approx(120.0, abs=0.001),
        "map_y": pytest.approx(55.0, abs=0.001),
    }
    assert catalog["strongholds"][1]["historical_gloss"] is None


def test_historical_catalog_rejects_duplicate_ids(tmp_path):
    paths = _write_catalog_fixture(tmp_path, duplicate=True)
    with pytest.raises(ScenarioCatalogError, match="Duplicate historical stronghold ID"):
        build_historical_stronghold_catalog(*paths)


def test_historical_catalog_requires_matching_png_dimensions(tmp_path):
    paths = _write_catalog_fixture(tmp_path, png_size=(201, 100))
    with pytest.raises(ScenarioCatalogError, match="identical pixel dimensions"):
        build_historical_stronghold_catalog(*paths)
