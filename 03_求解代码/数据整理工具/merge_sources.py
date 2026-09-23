"""Classify authoritative_for on the official sources and append derived / external entries.

Hashes and sizes are always computed from disk. Never hand-typed.
Run:  python merge_sources.py [--project <dir>]
"""
import argparse
import hashlib
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone

PROJECT = r"D:/CyberStorm/Documents/数模/新项目"

# --- judgement fields, chosen by the analyst (not machine facts) ---
ROLES = {
    "SRC-001": ["problem_statement"],
    "SRC-002": ["relay_uav_parameters", "relay_energy_inventory"],
    "SRC-003": ["demand_data", "deadline_data", "box_inventory"],
    "SRC-004": ["node_coordinates"],
    "SRC-005": ["transport_uav_parameters", "battery_inventory"],
    "SRC-006": ["communication_parameters"],
    "SRC-007": ["dem_elevation"],
    "SRC-008": ["dem_elevation"],
    "SRC-009": ["geospatial_context"],
    "SRC-010": ["geospatial_context"],
    "SRC-011": ["geospatial_context"],
    "SRC-012": ["geospatial_context"],
    "SRC-013": ["geospatial_context"],
    "SRC-014": ["geospatial_context"],
    "SRC-015": ["geospatial_context"],
    "SRC-016": ["geospatial_context"],
    "SRC-017": ["data_documentation"],
    "SRC-018": ["geospatial_context"],
    "SRC-019": ["results_template"],
}

# extra entries: (rel_path_from_project_or_abs, origin, derived_from, roles)
EXTRA = [
    ("problem/derived/PROBLEM_FULLTEXT_WITH_FORMULAS.txt", "team_created", "SRC-001",
     ["problem_text_with_formulas"]),
]

PRIOR_DIR = r"D:/CyberStorm/Documents/数模/D题"
PRIOR_FILES = [
    "第一问详细建模说明.md",
    "第二问详细建模说明.md",
    "第二问细化实施方案.md",
    "D题求解思路说明.md",
    "审查复算_Q1.py",
    "Q2结果.xlsx",
    "Q2结果说明.docx",
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def media_type(path):
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


def entry(project, sid, rel_or_abs, origin, derived_from, roles, method, by_user):
    path = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(project, rel_or_abs)
    if not os.path.isfile(path):
        print("  SKIP (missing):", path, file=sys.stderr)
        return None
    return {
        "source_id": sid,
        "path": path.replace("\\", "/") if os.path.isabs(rel_or_abs) else rel_or_abs,
        "sha256": sha256(path),
        "size": os.path.getsize(path),
        "media_type": media_type(path),
        "origin": origin,
        "acquisition": {"method": method, "provided_by_user": by_user, "source_reference": None},
        "authoritative_for": roles,
        "derived_from": derived_from,
        "mutable": origin != "official",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=PROJECT)
    args = ap.parse_args()
    project = args.project

    mpath = os.path.join(project, "problem", "SOURCE_MANIFEST.json")
    with open(mpath, encoding="utf-8") as fh:
        man = json.load(fh)

    # 1. classify existing sources
    unclassified = []
    for s in man["sources"]:
        if s["source_id"] in ROLES:
            s["authoritative_for"] = ROLES[s["source_id"]]
        elif not s.get("authoritative_for"):
            unclassified.append(s["source_id"])

    existing = {s["source_id"] for s in man["sources"]}
    nxt = max(int(s["source_id"].split("-")[1]) for s in man["sources"])

    # 2. derived
    for rel, origin, dfrom, roles in EXTRA:
        nxt += 1
        e = entry(project, f"SRC-{nxt:03d}", rel, origin, dfrom, roles,
                  "derived", False)
        if e:
            man["sources"].append(e)

    # 3. external reference: the team's own earlier D-题 work, not inherited
    for name in PRIOR_FILES:
        abs_path = os.path.join(PRIOR_DIR, name)
        nxt += 1
        e = entry(project, f"SRC-{nxt:03d}", abs_path, "external_reference", None,
                  ["prior_team_result_not_inherited"], "user_local_file", True)
        if e:
            man["sources"].append(e)

    man["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    man["producer"] = {"kind": "script", "name": "merge_sources.py", "version": "0.6.0"}

    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"sources: {len(man['sources'])} (was {len(existing)})")
    if unclassified:
        print("unclassified:", unclassified)
    for s in man["sources"]:
        print(f"  {s['source_id']} {s['origin']:<19} {','.join(s['authoritative_for']) or '-'}  {os.path.basename(s['path'])}")


if __name__ == "__main__":
    main()
