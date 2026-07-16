#!/usr/bin/env python3
"""Generate deterministic Studio previews and a lightweight timing manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dev-output/visual-content-studio"),
        help="Ignored directory for PNGs and the timing manifest",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Optional Studio database. Defaults to an ignored database beside the samples.",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = (args.database or (output / "preview.db")).resolve()
    os.environ["DATABASE_PATH"] = str(database)
    os.environ.setdefault("VISUAL_ASSET_DIR", str(output / "assets"))

    from utils.visual_studio.preview import render_preview
    from utils.visual_studio.registry import REGISTRY
    from utils.visual_studio.repository import initialize_visual_studio_schema

    initialize_visual_studio_schema()
    manifest = {
        "schema": "broeden.visual-content-studio.samples",
        "database": str(database),
        "templates": [],
    }
    started = time.perf_counter()
    for definition in REGISTRY.all():
        record = {
            "template_key": definition.key,
            "canvas": [definition.width, definition.height],
            "samples": [],
        }
        for edge_case in ("minimum", "maximum", "empty"):
            before = time.perf_counter()
            payload = render_preview(
                definition.key,
                edge_case=edge_case,
                safe_area=edge_case == "maximum",
            )
            elapsed_ms = round((time.perf_counter() - before) * 1000, 1)
            filename = "{}-{}.png".format(definition.key, edge_case)
            (output / filename).write_bytes(payload)
            record["samples"].append(
                {"filename": filename, "bytes": len(payload), "render_ms": elapsed_ms}
            )
            print("{} {}: {:,} bytes in {} ms".format(definition.key, edge_case, len(payload), elapsed_ms))
        manifest["templates"].append(record)
    manifest["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("Generated {} templates in {} ms".format(len(manifest["templates"]), manifest["total_ms"]))


if __name__ == "__main__":
    main()
