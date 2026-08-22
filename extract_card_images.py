"""Extract the card textures referenced by card_image_text_map.csv from Gremlins."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import UnityPy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "game_dir",
        type=Path,
        help="Gremlins installation directory containing Gremlins_Inc_Data",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("card_images_full"),
        help="Directory for extracted PNG files (default: card_images_full)",
    )
    return parser.parse_args()


def load_textures(resource_paths: list[Path]) -> dict[str, object]:
    textures: dict[str, object] = {}
    for resources_path in resource_paths:
        env = UnityPy.load(str(resources_path))
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            texture = obj.read()
            textures.setdefault(texture.m_Name, texture)
    return textures


def main() -> int:
    args = parse_args()
    resources_path = args.game_dir / "Gremlins_Inc_Data" / "resources.assets"
    map_path = Path("card_image_text_map.csv")
    if not resources_path.is_file():
        raise SystemExit(f"Game resources not found: {resources_path}")
    if not map_path.is_file():
        raise SystemExit(f"Card map not found: {map_path}")

    with map_path.open("r", encoding="utf-8-sig", newline="") as handle:
        image_names = sorted({row["image"] for row in csv.DictReader(handle)})

    resource_paths = [resources_path]
    resource_paths.extend(
        path for path in sorted(resources_path.parent.glob("*.assets"))
        if path != resources_path
    )
    textures = load_textures(resource_paths)
    args.output.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    extracted = 0
    for image_name in image_names:
        texture = textures.get(Path(image_name).stem)
        if texture is None:
            missing.append(image_name)
            continue
        texture.image.save(args.output / image_name)
        extracted += 1

    print(f"Extracted {extracted}/{len(image_names)} card images to {args.output}")
    if missing:
        print("Missing: " + ", ".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
