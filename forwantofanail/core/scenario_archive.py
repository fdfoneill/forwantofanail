from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from forwantofanail.core.scenario import get_scenario_package, load_scenario_package


SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


def _archive_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Scenario archives do not follow symbolic links: '{path}'.")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts):
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _inside(candidate: Path, parent: Path) -> bool:
    candidate = candidate.resolve()
    parent = parent.resolve()
    return parent in (candidate, *candidate.parents)


def create_scenario_archive(
    *, scenario_dir: Path | None = None, output_dir: Path, label: str | None = None
) -> tuple[Path, Path]:
    package = load_scenario_package(scenario_dir) if scenario_dir else get_scenario_package()
    output_dir = output_dir.expanduser().resolve()
    if _inside(output_dir, package.root):
        raise ValueError("Archive output must be outside the scenario package.")
    normalized_label = SAFE_LABEL.sub("-", (label or "").strip()).strip("-._")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    version = normalized_label or f"v{SAFE_LABEL.sub('-', package.scenario_version)}-{timestamp}"
    filename = f"{SAFE_LABEL.sub('-', package.scenario_id)}_{version}.tar.gz"
    archive_path = output_dir / filename
    checksum_path = output_dir / f"{filename}.sha256"
    if archive_path.exists() or checksum_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing archive or checksum for '{filename}'.")
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / f".{filename}.partial"
    try:
        with temporary.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for source in _archive_files(package.root):
                        relative = source.relative_to(package.root)
                        info = archive.gettarinfo(str(source), arcname=f"{package.scenario_id}/{relative.as_posix()}")
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = 0
                        info.mode = 0o644
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)
        temporary.replace(archive_path)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return archive_path, checksum_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and create a versioned deterministic scenario archive.")
    parser.add_argument("--scenario-dir", type=Path, help="Overrides SCENARIO_DIR.")
    parser.add_argument("--output-dir", type=Path, help="Overrides SCENARIO_ARCHIVE_DIR.")
    parser.add_argument("--label", help="Optional immutable release label, such as release-1.")
    args = parser.parse_args()
    configured_output = args.output_dir or (Path(os.environ["SCENARIO_ARCHIVE_DIR"]) if os.getenv("SCENARIO_ARCHIVE_DIR") else None)
    if configured_output is None:
        parser.error("--output-dir or SCENARIO_ARCHIVE_DIR is required")
    archive, checksum = create_scenario_archive(
        scenario_dir=args.scenario_dir, output_dir=configured_output, label=args.label
    )
    print(f"Created {archive}")
    print(f"Created {checksum}")


if __name__ == "__main__":
    main()
