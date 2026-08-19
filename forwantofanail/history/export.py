from __future__ import annotations

import argparse
import colorsys
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any

import h3
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Allows snapshot selection/config validation before optional renderer setup.
    Image = ImageDraw = ImageFont = None
from sqlalchemy.orm import Session

from forwantofanail.core.database import get_engine
from forwantofanail.core.models import Location, TerrainType, WorldHistoryEvent, WorldSnapshot


DEFAULT_OUTPUT = Path("exports/game-history")
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SCENARIO_MANIFEST = DATA_DIR / "scenario_manifest.json"
SCENARIO_EPOCH = date(1410, 5, 20)
WATCH_NAMES = {0: "Night", 1: "Matin", 2: "Prime", 3: "Sixbell", 4: "Vesper"}
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _scenario_config_path() -> Path:
    manifest = json.loads(SCENARIO_MANIFEST.read_text(encoding="utf-8"))
    relative = manifest.get("history_export_config")
    if not relative:
        raise ValueError("Scenario manifest does not define history_export_config")
    candidate = (DATA_DIR / str(relative)).resolve()
    if DATA_DIR.resolve() not in (candidate, *candidate.parents):
        raise ValueError("Scenario history export config escapes the data directory")
    return candidate


DEFAULT_CONFIG = _scenario_config_path()


def _normalized_faction(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _stable_faction_color(name: str) -> str:
    digest = hashlib.sha256(_normalized_faction(name).encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535.0
    saturation = 0.50 + (digest[2] / 255.0) * 0.20
    lightness = 0.40 + (digest[3] / 255.0) * 0.16
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def load_export_config(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    config = json.loads(raw)
    required = {"background", "neutral", "water", "road", "text", "cell_border"}
    colors = config.get("colors")
    if not isinstance(colors, dict) or not required.issubset(colors):
        raise ValueError(f"History export config must define colors: {', '.join(sorted(required))}")
    faction_colors = config.get("faction_colors", {})
    if not isinstance(faction_colors, dict):
        raise ValueError("History export faction_colors must be an object")
    for label, value in {**colors, **faction_colors}.items():
        if not isinstance(value, str) or HEX_COLOR.fullmatch(value) is None:
            raise ValueError(f"Invalid six-digit hex color for '{label}': {value!r}")
    return config, hashlib.sha256(raw).hexdigest()


def _faction_color(config: dict[str, Any], faction: str) -> str:
    configured = config.get("faction_colors", {})
    wanted = _normalized_faction(faction)
    for name, color in configured.items():
        if _normalized_faction(name) == wanted:
            return color
    return _stable_faction_color(faction)


def select_snapshots(
    session: Session,
    *,
    start_tick: int | None,
    end_tick: int | None,
    include_provisional: bool,
) -> list[WorldSnapshot]:
    query = session.query(WorldSnapshot)
    if not include_provisional:
        query = query.filter(WorldSnapshot.is_final.is_(True))
    if start_tick is not None:
        query = query.filter(WorldSnapshot.world_tick >= start_tick)
    if end_tick is not None:
        query = query.filter(WorldSnapshot.world_tick <= end_tick)
    rows = query.order_by(WorldSnapshot.world_tick.asc()).all()
    if not rows:
        raise ValueError("No snapshots match the requested range and finalization policy")
    expected_start = start_tick if start_tick is not None else rows[0].world_tick
    expected_end = end_tick if end_tick is not None else rows[-1].world_tick
    actual = [row.world_tick for row in rows]
    expected = list(range(int(expected_start), int(expected_end) + 1))
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise ValueError(f"Snapshot range contains gaps or non-final ticks; missing: {missing}")
    return rows


def _font(size: int, *, bold: bool = False):
    if ImageFont is None:
        raise RuntimeError("Pillow is required for history export; install the project environment")
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]
    for candidate in names:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cell_boundary(cell: str) -> list[tuple[float, float]]:
    return [(float(lng), float(lat)) for lat, lng in h3.cell_to_boundary(cell)]


class HistoryRenderer:
    def __init__(self, session: Session, *, width: int, height: int, config: dict[str, Any]):
        if Image is None or ImageDraw is None:
            raise RuntimeError("Pillow is required for history export; install the project environment")
        self.width = width
        self.height = height
        self.config = config
        rows = (
            session.query(
                Location.location_id,
                Location.region,
                Location.is_road,
                TerrainType.is_water,
            )
            .join(TerrainType, TerrainType.terrain_id == Location.terrain_id)
            .order_by(Location.location_id.asc())
            .all()
        )
        if not rows:
            raise ValueError("Cannot render history without map locations")
        self.cells: dict[str, dict[str, Any]] = {}
        all_points: list[tuple[float, float]] = []
        for location_id, region, is_road, is_water in rows:
            try:
                boundary = _cell_boundary(location_id)
                lat, lng = h3.cell_to_latlng(location_id)
            except Exception as exc:
                raise ValueError(f"Invalid H3 location in map: {location_id}") from exc
            all_points.extend(boundary)
            self.cells[location_id] = {
                "region": str(region or ""),
                "road": bool(is_road),
                "water": bool(is_water),
                "boundary_geo": boundary,
                "center_geo": (float(lng), float(lat)),
            }
        min_x = min(point[0] for point in all_points)
        max_x = max(point[0] for point in all_points)
        min_y = min(point[1] for point in all_points)
        max_y = max(point[1] for point in all_points)
        map_left, map_top = width * 0.035, height * 0.10
        map_right, map_bottom = width * 0.965, height * 0.95
        scale = min(
            (map_right - map_left) / max(max_x - min_x, 1e-9),
            (map_bottom - map_top) / max(max_y - min_y, 1e-9),
        )
        used_w, used_h = (max_x - min_x) * scale, (max_y - min_y) * scale
        offset_x = map_left + ((map_right - map_left) - used_w) / 2
        offset_y = map_top + ((map_bottom - map_top) - used_h) / 2

        def project(point: tuple[float, float]) -> tuple[float, float]:
            return (offset_x + (point[0] - min_x) * scale, offset_y + (max_y - point[1]) * scale)

        self.project = project
        for cell in self.cells.values():
            cell["polygon"] = [project(point) for point in cell["boundary_geo"]]
            cell["center"] = project(cell["center_geo"])
        road_ids = {cell_id for cell_id, cell in self.cells.items() if cell["road"]}
        connections: set[tuple[str, str]] = set()
        for cell_id in sorted(road_ids):
            for neighbor in h3.grid_disk(cell_id, 1):
                if neighbor in road_ids and neighbor != cell_id:
                    connections.add(tuple(sorted((cell_id, neighbor))))
        self.road_connections = sorted(connections)
        self.title_font = _font(max(20, round(height * 0.032)), bold=True)
        self.label_font = _font(max(11, round(height * 0.016)), bold=True)
        self.small_font = _font(max(10, round(height * 0.013)))

    def render(self, snapshot: WorldSnapshot, state: dict[str, Any], events: list[dict[str, Any]]) -> Image.Image:
        colors = self.config["colors"]
        image = Image.new("RGB", (self.width, self.height), colors["background"])
        draw = ImageDraw.Draw(image)
        strongholds = state.get("strongholds", [])
        controller_by_region = {
            _normalized_faction(row.get("name", "")): str(row.get("controller", ""))
            for row in strongholds
        }
        for cell_id, cell in self.cells.items():
            if cell["water"]:
                fill = colors["water"]
            else:
                controller = controller_by_region.get(_normalized_faction(cell["region"]), "")
                fill = _faction_color(self.config, controller) if controller else colors["neutral"]
            draw.polygon(cell["polygon"], fill=fill, outline=colors["cell_border"], width=1)
        for first, second in self.road_connections:
            draw.line([self.cells[first]["center"], self.cells[second]["center"]], fill=colors["road"], width=max(2, self.height // 270))

        for stronghold in strongholds:
            cell = self.cells.get(stronghold.get("location_h3"))
            if cell is None:
                continue
            x, y = cell["center"]
            radius = max(7, self.height // 65)
            fill = _faction_color(self.config, stronghold.get("controller", ""))
            kind = str(stronghold.get("type", "")).casefold()
            if kind == "fortress":
                points = [(x, y - radius), (x + radius, y), (x, y + radius), (x - radius, y)]
                draw.polygon(points, fill=fill, outline=colors["text"], width=2)
            elif kind == "city":
                draw.rectangle((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=colors["text"], width=2)
            else:
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=colors["text"], width=2)
            if stronghold.get("siege"):
                ring = radius + 6
                draw.ellipse((x - ring, y - ring, x + ring, y + ring), outline="#F2D35E", width=4)
            draw.text((x + radius + 4, y - radius), str(stronghold.get("name", "")), fill=colors["text"], font=self.small_font, stroke_width=2, stroke_fill=colors["background"])

        occupying: dict[str, list[dict[str, Any]]] = {}
        for army in state.get("armies", []):
            if army.get("is_garrison") or int(army.get("strength", 0) or 0) <= 0:
                continue
            occupying.setdefault(str(army.get("location_h3", "")), []).append(army)
        for cell_id, armies in sorted(occupying.items()):
            cell = self.cells.get(cell_id)
            if cell is None:
                continue
            armies.sort(key=lambda row: int(row.get("army_id", 0)))
            for index, army in enumerate(armies):
                angle = (2 * math.pi * index / len(armies)) if len(armies) > 1 else 0.0
                offset = min(self.width, self.height) * (0.018 if len(armies) > 1 else 0.0)
                x = cell["center"][0] + math.cos(angle) * offset
                y = cell["center"][1] + math.sin(angle) * offset
                flag_w, flag_h = max(24, self.width // 55), max(18, self.height // 45)
                faction_color = _faction_color(self.config, str(army.get("faction", "")))
                draw.line((x, y + flag_h, x, y - flag_h), fill=colors["text"], width=3)
                draw.polygon([(x, y - flag_h), (x + flag_w, y - flag_h * 0.72), (x, y - flag_h * 0.15)], fill=faction_color, outline=colors["text"])
                label = "".join(word[:1] for word in str(army.get("name", "Army")).split())[:4].upper() or f"A{army.get('army_id')}"
                draw.text((x + 3, y + 3), label, fill=colors["text"], font=self.label_font, stroke_width=2, stroke_fill=colors["background"])

        for event in events:
            self._draw_event(draw, event)
        scenario_date = SCENARIO_EPOCH + timedelta(days=max(0, int(snapshot.day) - 1))
        heading = f"{scenario_date.strftime('%B %d, %Y')}  ·  Day {snapshot.day}  ·  {WATCH_NAMES.get(int(snapshot.watch), str(snapshot.watch))} Watch"
        draw.text((self.width * 0.035, self.height * 0.025), heading, fill=colors["text"], font=self.title_font)
        return image

    def _draw_event(self, draw: ImageDraw.ImageDraw, event: dict[str, Any]) -> None:
        kind = event["kind"]
        payload = event["payload"]
        location_ids = payload.get("location_h3s") or [payload.get("location_h3") or event.get("location_h3")]
        location_id = next((value for value in location_ids if value in self.cells), None)
        if location_id is None:
            return
        x, y = self.cells[location_id]["center"]
        radius = max(18, self.height // 34)
        text = ""
        if kind == "battle":
            draw.line((x - radius, y - radius, x + radius, y + radius), fill="#F6E3A1", width=5)
            draw.line((x + radius, y - radius, x - radius, y + radius), fill="#F6E3A1", width=5)
            text = str(payload.get("winner_faction") or "Draw")
        elif kind == "stronghold_conquest":
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="#FFFFFF", width=6)
            text = f"Conquered: {payload.get('new_controller', '')}"
        elif kind in {"siege_started", "siege_ended"}:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="#F2D35E" if kind == "siege_started" else "#FFFFFF", width=5)
            text = "Siege begins" if kind == "siege_started" else "Siege ends"
        if text:
            draw.text((x + radius + 5, y - radius), text, fill=self.config["colors"]["text"], font=self.label_font, stroke_width=2, stroke_fill=self.config["colors"]["background"])


def _event_rows(session: Session, start_tick: int, end_tick: int) -> list[dict[str, Any]]:
    rows = (
        session.query(WorldHistoryEvent)
        .filter(WorldHistoryEvent.world_tick >= start_tick, WorldHistoryEvent.world_tick <= end_tick)
        .order_by(WorldHistoryEvent.world_tick.asc(), WorldHistoryEvent.event_id.asc())
        .all()
    )
    return [
        {
            "world_tick": row.world_tick,
            "kind": row.event_kind,
            "location_h3": row.location_id,
            "payload": json.loads(row.payload_json),
        }
        for row in rows
    ]


def schedule_events_for_frames(
    snapshots: list[WorldSnapshot],
    events: list[dict[str, Any]],
    *,
    duration: int,
) -> dict[int, list[dict[str, Any]]]:
    index_by_tick = {snapshot.world_tick: index for index, snapshot in enumerate(snapshots)}
    scheduled: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        occurrence = index_by_tick.get(event["world_tick"])
        if occurrence is None:
            continue
        for frame_index in range(occurrence, min(len(snapshots), occurrence + duration)):
            scheduled.setdefault(frame_index, []).append(event)
    return scheduled


def export_history(
    *,
    output_dir: Path = DEFAULT_OUTPUT,
    start_tick: int | None = None,
    end_tick: int | None = None,
    width: int = 1920,
    height: int = 1080,
    fps: float = 2.0,
    event_duration: int = 3,
    config_path: Path = DEFAULT_CONFIG,
    no_video: bool = False,
    include_provisional: bool = False,
) -> dict[str, Any]:
    if width < 320 or height < 240 or fps <= 0 or event_duration < 1:
        raise ValueError("Invalid dimensions, fps, or event duration")
    config, config_hash = load_export_config(config_path)
    engine = get_engine()
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        snapshots = select_snapshots(
            session,
            start_tick=start_tick,
            end_tick=end_tick,
            include_provisional=include_provisional,
        )
        events = _event_rows(session, snapshots[0].world_tick, snapshots[-1].world_tick)
        renderer = HistoryRenderer(session, width=width, height=height, config=config)
        frames_dir = output_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        events_by_frame = schedule_events_for_frames(snapshots, events, duration=event_duration)
        for frame_index, snapshot in enumerate(snapshots):
            state = json.loads(snapshot.state_json)
            frame = renderer.render(snapshot, state, events_by_frame.get(frame_index, []))
            frame.save(frames_dir / f"frame_{frame_index:06d}.png", format="PNG")

    provisional_included = any(not bool(snapshot.is_final) for snapshot in snapshots)
    manifest = {
        "tick_range": {"start": snapshots[0].world_tick, "end": snapshots[-1].world_tick},
        "authoritative_starting_tick": snapshots[0].world_tick,
        "frame_count": len(snapshots),
        "width": width,
        "height": height,
        "fps": fps,
        "configuration_hash": config_hash,
        "provisional_final_frame_included": provisional_included and not bool(snapshots[-1].is_final),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not no_video:
        video_path = output_dir / "game-history.mp4"
        command = [
            "ffmpeg", "-y", "-framerate", str(fps), "-i", str(output_dir / "frames" / "frame_%06d.png"),
            "-frames:v", str(len(snapshots)), "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(video_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError(f"PNG frames are complete at {output_dir / 'frames'}, but FFmpeg was not found") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown FFmpeg error"
            raise RuntimeError(f"PNG frames are complete at {output_dir / 'frames'}, but FFmpeg encoding failed: {detail}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Render authoritative game history snapshots")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start-tick", type=int)
    parser.add_argument("--end-tick", type=int)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--event-duration", type=int, default=3)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--include-provisional", action="store_true")
    args = parser.parse_args()
    manifest = export_history(
        output_dir=args.output_dir,
        start_tick=args.start_tick,
        end_tick=args.end_tick,
        width=args.width,
        height=args.height,
        fps=args.fps,
        event_duration=args.event_duration,
        config_path=args.config,
        no_video=args.no_video,
        include_provisional=args.include_provisional,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
