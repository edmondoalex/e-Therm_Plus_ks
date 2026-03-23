#!/usr/bin/env python3
"""Small helper to bump addon version and append a worklog entry.

Usage:
  python scripts/bump_release.py --version 2.0.3 --author "MarioR" --notes "Short note" --files app/debug_server.py,config.yaml

This script:
- updates `version:` in `config.yaml`
- appends a markdown entry to `worklog.md` (inside the existing fenced block if present)
- writes .bak backups for safety
"""
from __future__ import annotations
import argparse
import shutil
import datetime
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
WORKLOG = ROOT / "worklog.md"
DEBUG_SERVER = ROOT / "app" / "debug_server.py"


def backup(p: Path) -> Path:
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dst = p.with_suffix(p.suffix + f".bak.{ts}")
    shutil.copy2(p, dst)
    return dst


def update_config_version(cfg_path: Path, new_version: str) -> bool:
    txt = cfg_path.read_text(encoding="utf-8")
    pat = re.compile(r'^(\s*version\s*:\s*)(["\']?)([^"\'\r\n]+)(["\']?)\s*$', flags=re.M)
    if not pat.search(txt):
        raise RuntimeError(f"version: line not found in {cfg_path}")
    new_txt = pat.sub(lambda m: f"{m.group(1)}\"{new_version}\"", txt, count=1)
    cfg_path.write_text(new_txt, encoding="utf-8")
    return True


def update_code_version(py_path: Path, new_version: str) -> bool:
    if not py_path.exists():
        return False
    txt = py_path.read_text(encoding="utf-8-sig")
    pat = re.compile(r'(?m)^(\s*CODE_VERSION\s*=\s*)(["\'])([^"\']*)(["\'])\s*$')
    if not pat.search(txt):
        return False
    new_txt = pat.sub(lambda m: f"{m.group(1)}\"{new_version}\"", txt, count=1)
    py_path.write_text(new_txt, encoding="utf-8-sig")
    return True


def append_worklog(wl_path: Path, version: str, author: str, notes: str, files: str | None) -> None:
    now = datetime.datetime.utcnow().date().isoformat()
    header = f"\n## {now} — {version} — Autore: {author}\n"
    body = ""
    if notes:
        for line in notes.strip().splitlines():
            body += f"- {line.strip()}\n"
    if files:
        body += "- File modificati: " + ", ".join([f.strip() for f in files.split(",")]) + "\n"

    entry = header + body + "\n"

    txt = wl_path.read_text(encoding="utf-8") if wl_path.exists() else "# Worklog\n\n"
    # If file uses a fenced markdown block that wraps content (```...```), insert before last fence
    last_fence = txt.rfind("```")
    if last_fence != -1 and txt.strip().startswith("```"):
        # find the position of the last fence that closes the block
        before = txt[:last_fence]
        after = txt[last_fence:]
        new_txt = before + entry + after
    else:
        new_txt = txt.rstrip() + "\n\n" + entry
    wl_path.write_text(new_txt, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Bump addon version and append worklog entry")
    p.add_argument("--version", required=True, help="New version string (e.g. 2.0.3)")
    p.add_argument("--author", default="Automator", help="Author name for worklog")
    p.add_argument("--notes", default="", help="Short notes describing the change")
    p.add_argument("--files", default="", help="Comma-separated list of files modified")
    args = p.parse_args()

    if not CONFIG.exists():
        print(f"ERROR: config.yaml not found at {CONFIG}")
        return 2
    if not WORKLOG.exists():
        # create minimal worklog
        WORKLOG.write_text("# Worklog — e-Therm_Plus_ks\nQuesto file traccia in modo minimale tutte le modifiche significative al progetto.\n\n---\n", encoding="utf-8")

    print(f"Backing up {CONFIG} and {WORKLOG}...")
    b1 = backup(CONFIG)
    b2 = backup(WORKLOG)
    print(f"Backups: {b1.name}, {b2.name}")

    print(f"Updating {CONFIG} -> version {args.version}")
    update_config_version(CONFIG, args.version)
    if update_code_version(DEBUG_SERVER, args.version):
        print(f"Updated CODE_VERSION in {DEBUG_SERVER}")

    print(f"Appending worklog entry {args.version}")
    append_worklog(WORKLOG, args.version, args.author, args.notes, args.files)

    print("Done.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
