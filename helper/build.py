#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build the iMessage Reader helper app from source.

Compiles `ImessageReader.swift` for arm64 and x86_64, merges them into one
universal binary, wraps it with `Info.plist` into `dist/iMessage Reader.app`
and signs it ad hoc. Needs the Xcode command line tools (`swiftc`, `lipo`,
`codesign`); no Xcode project.

Usage: uv run --script helper/build.py [--out DIR]
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("build-helper")

HERE = Path(__file__).resolve().parent
APP_NAME = "iMessage Reader.app"
EXECUTABLE = "imessage-reader"
BUNDLE_ID = "com.github.clementwalter.imessage-reader"
# Matches LSMinimumSystemVersion in Info.plist.
MIN_MACOS = "13.0"
ARCHS = ("arm64", "x86_64")


def build(out: Path) -> Path:
    """Build the signed app bundle under `out` and return its path."""
    app = out / APP_NAME
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        slices = []
        for arch in ARCHS:
            target = Path(tmp) / arch
            subprocess.run(
                ["swiftc", "-O", "-target", f"{arch}-apple-macos{MIN_MACOS}",
                 str(HERE / "ImessageReader.swift"), "-o", str(target)],
                check=True,
            )
            slices.append(str(target))
        subprocess.run(["lipo", "-create", *slices, "-output", str(macos / EXECUTABLE)], check=True)
    shutil.copy2(HERE / "Info.plist", app / "Contents" / "Info.plist")
    # Ad hoc: the privacy grant is then tied to this exact binary's hash.
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--options", "runtime", "--identifier", BUNDLE_ID, str(app)],
        check=True,
    )
    subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
    return app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "dist", help="Output directory (default: helper/dist).")
    args = parser.parse_args()
    app = build(args.out)
    log.info("built %s", app)
    sys.stdout.write(f"{app}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
