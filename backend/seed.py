"""Pre-process demo DXFs so the hackathon demo never waits on extraction.

Usage: python seed.py [path.dxf ...]   (defaults to ../samples/*.dxf)
Add --overlay to also write a rooms-over-plan debug image per project.
"""

import glob
import os
import sys

import planrender
import projects


def seed(paths, overlay=False):
    for path in paths:
        print(f"=== {os.path.basename(path)} ===")
        try:
            p = projects.create_project(path)
        except Exception as exc:
            print(f"  ! failed: {exc}")
            continue
        print(f"  ok: {p['id']}  rooms={len(p['rooms'])}  "
              f"{p['width_px']}x{p['height_px']}px")
        if overlay:
            root = projects.pdir(p["id"])
            out = os.path.join(root, "debug_rooms.png")
            planrender.debug_overlay(os.path.join(root, "plan.png"),
                                     p["rooms"], out)
            print(f"  overlay: {out}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    overlay = "--overlay" in sys.argv
    if not args:
        here = os.path.dirname(os.path.abspath(__file__))
        args = sorted(glob.glob(os.path.join(here, "..",
                                             "samples", "*.dxf")))
    seed(args, overlay=overlay)
