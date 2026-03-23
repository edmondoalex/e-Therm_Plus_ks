#!/usr/bin/env python3
"""
A small helper to add a virtual thermostat to the local `data/vtherm.json` config
used by the e-Therm addon in development/testing.

Usage:
  python scripts/add_thermostat.py --id 1 --name TestTherm --source-num 1 --power --fan3

This will create `data/vtherm.json` if missing and append the thermostat entry.
It writes both to `./data/vtherm.json` and `/data/vtherm.json` (if possible).
"""
import argparse
import json
import os
from pathlib import Path

DEFAULT_DATA_PATH = os.path.join(os.getcwd(), "data", "vtherm.json")
ALT_DATA_PATH = os.path.join(os.sep, "data", "vtherm.json")


def load_cfg(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_cfg(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True, help="Thermostat ID (string or number)")
    p.add_argument("--name", required=True, help="Thermostat name")
    p.add_argument("--source-num", required=True, help="Source e-safe thermostat number")
    p.add_argument("--source-type", default="esafe", help="Source type (default: esafe)")
    p.add_argument("--power", action="store_true", help="Expose power (number) output")
    p.add_argument("--fan3", action="store_true", help="Expose fan3 (min/med/max) outputs")
    args = p.parse_args()

    cfg = load_cfg(DEFAULT_DATA_PATH)
    if not isinstance(cfg, dict):
        cfg = {}

    items = cfg.get("thermostats") or []

    # avoid duplicates by ID
    sid = str(args.id)
    for t in items:
        if str(t.get("id")) == sid:
            print(f"Thermostat with id={sid} already exists. Aborting.")
            raise SystemExit(1)

    new = {
        "id": sid,
        "name": args.name,
        "source": {"type": args.source_type, "num": int(args.source_num)},
        "outputs": {"power": bool(args.power), "fan3": bool(args.fan3)}
    }

    items.append(new)
    cfg["thermostats"] = items

    save_cfg(DEFAULT_DATA_PATH, cfg)
    print(f"Saved config to {DEFAULT_DATA_PATH}")

    # also try to save to /data for runtime container
    try:
        save_cfg(ALT_DATA_PATH, cfg)
        print(f"Also saved config to {ALT_DATA_PATH}")
    except Exception:
        print(f"Could not write to {ALT_DATA_PATH} (permission or not present) - OK for dev)")

    print("New thermostat:")
    print(json.dumps(new, indent=2, ensure_ascii=False))
