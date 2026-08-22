#!/usr/bin/env python3
"""Make every writable bind mount writable by the container that mounts it.

WHY THIS EXISTS. Four failures in one evening, all the same shape and all
invisible until something printed the error:

    PUT destination      the driver (uid 50000) could not write a dir the server made
    /data/identity.json  the emulator (uid 65532) could not write a dir Docker made
    admin.pat            the Airflow worker could not read a file compose cp copied
    product_snapshot.json  the Airflow worker could not write ${PRODUCT}/out

A container runs as a non-root uid. A host directory it bind-mounts is created
by whoever got there first -- Docker, as root, or the host user -- and the
container then cannot write it. On a developer laptop the uids often happen to
line up and nothing is noticed; on a CI runner they do not.

DERIVED FROM COMPOSE, NOT LISTED HERE. Reading `docker compose config` means
this covers the mounts that actually exist, including any added later, and it
means this script knows nothing about what the paths CONTAIN. That matters: the
product's directories belong to the product, and a platform that hardcoded
`${PRODUCT}/out` would be asserting knowledge of a product's layout, which is
the coupling these repositories are built to avoid. A mount the compose file
already declares is a different thing: the platform put it there.

READ-ONLY MOUNTS ARE SKIPPED, because nothing needs to write them and widening
them would be a permission granted for no reason. Named volumes are skipped
too: Docker owns those and chowns them to the image's user itself.

IT CREATES WHAT IS MISSING AND WIDENS ONLY WHAT IT CREATED. A directory that
already exists belongs to somebody -- `contoso-erp-seed` legitimately mounts the
sources CHECKOUT read-write, because it runs `uv sync` in it as root -- and
chmod 0777 on a checkout somebody is working in is an intrusive side effect for
no benefit. The failures this exists to prevent are all directories that did
not exist until a container needed them, so creating-and-widening is the whole
of the fix and no wider.

Reads `docker compose config --format json` on stdin.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    try:
        cfg = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"platform: could not read the compose config: {exc}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    made: list[str] = []
    for svc in (cfg.get("services") or {}).values():
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, dict) or vol.get("type") != "bind":
                continue
            if vol.get("read_only"):
                continue
            src = vol.get("source")
            if not src or src in seen:
                continue
            seen.add(src)

            if os.path.exists(src):
                continue
            os.makedirs(src, exist_ok=True)
            # 0777 rather than a matching uid: the host user differs between a
            # laptop and a CI runner, and the alternative is every consumer of
            # this platform discovering the image's uid for themselves.
            os.chmod(src, 0o777)
            made.append(src)

    for d in made:
        print(f"platform: created {d} writable, for the container that mounts it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
