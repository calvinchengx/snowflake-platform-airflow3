#!/usr/bin/env python3
"""Re-resolve every pinned digest from the versions currently in versions.env.

FOR THE HAND BUMP. `set_release.py` only moves the image a databricks-emulator
release retags; `SAIL_VERSION` and `SPARK_AGENT_VERSION` follow fabric-emulator's
cadence and are edited by a person. Editing one of those without its digest
leaves the stack pulling the OLD image while versions.env names the new one, and
nothing fails — so this exists to make the correct move a one-liner rather than
a thing to remember.

Run it after changing any *_VERSION, and commit the result.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from digests import PINS, digest_of, rewrite, value  # noqa: E402

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "versions.env"


def main() -> int:
    text = VERSIONS.read_text(encoding="utf-8")
    changed = 0
    for prefix, image in PINS.items():
        tag = value(text, f"{prefix}_VERSION")
        digest = digest_of(image, tag)
        text, before = rewrite(text, prefix, digest)
        moved = before != digest
        changed += moved
        print(f"{image}:{tag}\n  {before[:19]}… -> {digest[:19]}…"
              f"  ({'moved' if moved else 'unchanged'})")
    VERSIONS.write_text(text, encoding="utf-8")
    print(f"\n{changed} digest(s) moved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
