#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Build the iMessage Reader helper app from source.

Compiles `ImessageReader.swift` for arm64 and x86_64, merges them into one
universal binary and wraps it with `Info.plist` into `dist/iMessage Reader.app`.
Needs the Xcode command line tools (`swiftc`, `lipo`, `codesign`); no Xcode project.

Signing: ad hoc by default. With a Developer ID identity (`--identity` or
`CODESIGN_IDENTITY`) it is signed with the hardened runtime and a secure
timestamp; adding a notarytool keychain profile (`--notary-profile` or
`NOTARY_KEYCHAIN_PROFILE`) also notarizes it and staples the ticket, so
Gatekeeper accepts it offline and the Full Disk Access grant follows the team
and bundle id across rebuilds.

Usage: uv run --script helper/build.py [--out DIR] [--identity NAME] [--notary-profile NAME]
"""

from __future__ import annotations

import argparse
import logging
import os
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


def build(out: Path, identity: str | None = None) -> Path:
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
    # Ad hoc ("-") ties the privacy grant to this exact binary's hash; a Developer ID ties it to the team.
    sign = ["codesign", "--force", "--sign", identity or "-", "--options", "runtime", "--identifier", BUNDLE_ID]
    if identity:
        sign.append("--timestamp")
    subprocess.run([*sign, str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
    return app


def notarize(app: Path, profile: str) -> None:
    """Submit the app to Apple's notary service, wait for it, and staple the ticket."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "app.zip"
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
        subprocess.run(
            ["xcrun", "notarytool", "submit", str(archive), "--keychain-profile", profile, "--wait"], check=True
        )
    subprocess.run(["xcrun", "stapler", "staple", str(app)], check=True)
    subprocess.run(["spctl", "--assess", "--type", "execute", "--verbose", str(app)], check=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "dist", help="Output directory (default: helper/dist).")
    parser.add_argument("--identity", default=os.environ.get("CODESIGN_IDENTITY"),
                        help="Developer ID Application identity (default: $CODESIGN_IDENTITY, else ad hoc).")
    parser.add_argument("--notary-profile", default=os.environ.get("NOTARY_KEYCHAIN_PROFILE"),
                        help="notarytool keychain profile (default: $NOTARY_KEYCHAIN_PROFILE); needs --identity.")
    args = parser.parse_args()
    if args.notary_profile and not args.identity:
        parser.error("notarizing needs a Developer ID identity (--identity or CODESIGN_IDENTITY)")
    app = build(args.out, args.identity)
    if args.notary_profile:
        notarize(app, args.notary_profile)
    log.info("built %s", app)
    sys.stdout.write(f"{app}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
