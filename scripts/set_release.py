"""Point this repository at a specific snowflake-emulator release."""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"
TRACKS_THE_RELEASE = ("SNOWFLAKE_EMULATOR_VERSION",)
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

# THE CLIENT WHEEL IS NO LONGER HERE. Until the split this repository held both
# the image pin and the snowflake-target wheel URL, and this script moved the
# pair. The wheel went to the product with the code that imports it, so the two
# pins now live in two repositories and NOTHING IN EITHER ONE ALONE CAN SEE THE
# PAIR.
#
# What replaces the check is `check_product_pin.py`, which `make verify` runs
# against whatever product this platform was pointed at, before any step. The
# consequence is deliberate and worth stating: an emulator release now needs
# TWO bumps, and forgetting the second one fails the run loudly rather than
# quietly verifying a client and an image that disagree.


def set_version(text: str, version: str) -> tuple[str, dict[str, str]]:
    moved = {}
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, old = stripped.partition("=")
        key, old = key.strip(), old.strip()
        if key in TRACKS_THE_RELEASE:
            moved[key] = old
            lines[i] = f"{key}={version}\n"
    return "".join(lines), moved


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: set_release.py <version>   e.g. set_release.py 0.1.0")
    version = sys.argv[1].lstrip("v")
    if not SEMVER.match(version):
        sys.exit(f"not a version: {version!r} — expected something like 0.1.0")
    text = VERSIONS.read_text(encoding="utf-8")
    new, moved = set_version(text, version)
    missing = [k for k in TRACKS_THE_RELEASE if k not in moved]
    if missing:
        sys.exit(f"{VERSIONS.name} has no {', '.join(missing)} to set")
    VERSIONS.write_text(new, encoding="utf-8")
    for key, old in moved.items():
        note = "  (unchanged)" if old == version else ""
        print(f"  {key}: {old} -> {version}{note}")

    print(
        f"  the snowflake-target wheel is the PRODUCT's pin -- move it there too:\n"
        f"    python scripts/set_release.py {version}   (here, done)\n"
        f"    then in the product: point snowflake-target at v{version} and `uv lock`"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
