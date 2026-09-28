from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    args = parse_args()
    config = load_yaml(resolve_path(args.config))
    blender = resolve_path(args.blender or config.get("blender", {}).get("executable") or default_blender_path())
    avatar = resolve_path(args.avatar or config["avatar"]["path"])
    profile = resolve_path(args.profile)
    bone_map = resolve_path(args.bone_map)
    script = PROJECT_ROOT / "src" / "blender" / "blender_calibrate_avatar.py"

    if not blender.exists():
        raise FileNotFoundError(f"Blender executable not found: {blender}")
    if not avatar.exists():
        raise FileNotFoundError(f"Avatar FBX not found: {avatar}")

    cmd = [
        str(blender),
        "--factory-startup",
        "--background",
        "--python",
        str(script),
        "--",
        "--avatar",
        str(avatar),
        "--profile",
        str(profile),
        "--bone-map",
        str(bone_map),
    ]
    print("Launching Blender avatar calibration...")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)
    print(f"Wrote {profile}")
    print(f"Wrote {bone_map}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate the avatar FBX in Blender background mode.")
    parser.add_argument("--avatar", help="Avatar FBX path.")
    parser.add_argument("--blender", help="Blender executable path.")
    parser.add_argument("--config", default="./config/settings.yaml")
    parser.add_argument("--profile", default="./config/avatar_profile.json")
    parser.add_argument("--bone-map", default="./config/avatar_bone_map.json")
    return parser.parse_args()


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def default_blender_path() -> Path:
    return Path("C:/Program Files/Blender Foundation/Blender 4.5/blender.exe")


if __name__ == "__main__":
    raise SystemExit(main())
