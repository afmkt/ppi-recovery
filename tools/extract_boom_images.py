#!/usr/bin/env python3
"""Extract the mobile-app screenshots *and* their pair metadata from
``docs/boom.v1.0.docx`` into the committed asset dir ``src/dashboard/resources``,
which the dashboard's API Docs tab loads.

The docx is the source of truth for *which* screenshots pair with *which*
endpoint (parsed by ``dashboard.boom_docs.extract_pairs``). This tool copies the
screenshot binaries out of the docx **and** writes a ``pairs.json`` manifest of
the pairs, so the dashboard can render the tab without ever reading the docx at
runtime.

Usage:

  python tools/extract_boom_images.py            # extract into src/dashboard/resources
  python tools/extract_boom_images.py --prune    # also remove unreferenced images
  python tools/extract_boom_images.py --docx X   # source docx (default docs/boom.v1.0.docx)
  python tools/extract_boom_images.py --dest D   # destination (default the resources dir)

Stdlib + ``dashboard.boom_docs``; no new dependencies.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract boom screenshots + pair metadata into the dashboard resources dir."
    )
    ap.add_argument(
        "--docx", default=None, help="source docx (default docs/boom.v1.0.docx)"
    )
    ap.add_argument(
        "--dest", default=None, help="destination dir (default src/dashboard/resources)"
    )
    ap.add_argument(
        "--prune",
        action="store_true",
        help="remove image*.png/svg in dest that no pair references",
    )
    args = ap.parse_args()

    # Importable from the repo root whether or not the editable install is on path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from dashboard import boom_docs

    dest = boom_docs.resources_dir() if args.dest is None else Path(args.dest)
    # Parse the docx once; reuse the pairs for the image copy and the manifest so
    # the (large) docx is opened only twice, not three times.
    pairs = boom_docs.extract_pairs(args.docx)
    written = boom_docs.extract_images(
        args.docx, dest=args.dest, prune=args.prune, pairs=pairs
    )
    manifest = boom_docs.save_pairs(pairs, boom_docs.pairs_path(args.dest))
    print(
        f"wrote {len(written)} screenshot(s) + {manifest.name} ({len(pairs)} pairs) to {dest}"
    )
    for p in written:
        print(f"  {p.name}  {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
