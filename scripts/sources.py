"""Stand up whatever vendors a sources repo declares.

THE PLATFORM OWNS THE MECHANISM, THE DECLARATION OWNS THE CONTENT — the same
split as the DAG bundle. This file knows how to run an OpenAPI simulator and a
CDC stack; it does not know that Contoso exists, how many vendors there are, or
what any of them serve. Point it at a different `sources.yaml` and it stands up
those vendors instead.

That is not tidiness. In production none of this runs at all: the vendors are
real, and the only thing that survives is their Airflow Connection names. A
platform that hard-coded three mokapis would have encoded a local convenience
into the thing that is supposed to be target-independent.

Emits a compose fragment on stdout rather than starting anything itself, so the
services join the same project, network and lifecycle as the rest of the stack
and `make down` really does take everything with it.
"""
from __future__ import annotations

import json
import pathlib
import sys


def _load(path: pathlib.Path) -> dict:
    """Read sources.yaml without a YAML dependency.

    The platform image is stock `apache/airflow` plus the product's own
    dependencies; adding PyYAML here would mean the platform has opinions about
    the worker's environment. The declaration is a small, flat document, so a
    minimal reader is cheaper than that coupling — and it FAILS on anything it
    does not understand rather than guessing, because a silently-skipped vendor
    would surface much later as an empty landing.
    """
    vendors: list[dict] = []
    current: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() in ("vendors:",) or line.startswith("version:"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {}
            vendors.append(current)
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        current[key.strip()] = value
    return {"vendors": vendors}


def fragment(decl: dict, sources_dir: str, pins: dict) -> dict:
    services: dict = {}
    for v in decl["vendors"]:
        name = v["name"].replace("_", "-")
        kind = v.get("kind")
        if kind == "openapi":
            services[name] = {
                "image": f"mokapi/mokapi:{pins['MOKAPI_VERSION']}",
                # The dashboard retains every request AND its response body. For
                # a large export that is a multi-hundred-MB copy per call, so the
                # history is capped at one entry per API -- the reason this flag
                # exists is a container that was being OOM-killed mid-response.
                "command": ["--event-store-default-size=1",
                            f"/sources/{v['spec']}", f"/sources/{v['script']}"],
                # Go does not read the cgroup limit; without GOMEMLIMIT the heap
                # climbs past mem_limit and the container dies mid-response.
                # THE mem_limit THIS COMMENT ASSUMED DID NOT EXIST until G38:
                # the ceiling was told to Go and never to Docker, so the host
                # killed this container instead -- measured, `contoso-pos`
                # Exited(137) OOMKilled mid-witness, surfacing as a vendor
                # extraction failure two layers away.
                # GOMEMLIMIT IS A SOFT TARGET and mem_limit is a hard kill, so
                # the gap between them is Go's room to collect before Docker
                # intervenes. 2GiB under a 2560m cap left 400MB of headroom and
                # died serving the orders export -- measured, `OOM=true
                # limit=2684354560`, mid-pagination.
                #
                # SIZED FROM THE WRONG MOMENT the first time: `docker stats`
                # showed this vendor at 159MiB, which is what it costs while
                # IDLE. A mokapi service holds request and response bodies, and
                # the export is 176MB of CSV per call, so its peak is an order
                # of magnitude above its resting size. A ceiling read off a
                # quiet stack is not a ceiling.
                "environment": {"GOMEMLIMIT": "3GiB"},
                "mem_limit": "4g",
                "volumes": [f"{sources_dir}:/sources:ro"],
                "expose": [str(v["port"])],
            }
        elif kind == "cdc":
            # THREE SERVICES, because a change stream needs all three and any
            # two of them is a snapshot wearing a stream's name. The database
            # holds the rows, Debezium reads its write-ahead log, and the broker
            # carries what Debezium produced. Standing up only Postgres would
            # serve rows -- possibly even the right count -- while testing
            # something else entirely.
            db, broker, connect = f"{name}-db", f"{name}-broker", f"{name}-connect"
            services[db] = {
                "image": f"postgres:{pins['POSTGRES_VERSION']}",
                "mem_limit": "512m",
                # LOGICAL replication, and the slots to hold it. Debezium reads
                # the WAL; at the default `replica` level there is nothing in it
                # for a decoder to read and the connector attaches to silence.
                "command": ["postgres", "-c", "wal_level=logical",
                            "-c", "max_replication_slots=4", "-c", "max_wal_senders=4"],
                "environment": {"POSTGRES_USER": v.get("db_user", "contoso"),
                                "POSTGRES_PASSWORD": v.get("db_password", "contoso-erp-dev"),
                                "POSTGRES_DB": v.get("db_name", "erp")},
                "healthcheck": {
                    "test": ["CMD-SHELL",
                             f"pg_isready -U {v.get('db_user','contoso')} -d {v.get('db_name','erp')}"],
                    "interval": "5s", "timeout": "3s", "retries": 20},
                "volumes": [f"{sources_dir}:/sources:ro"],
            }
            services[broker] = {
                "image": f"docker.redpanda.com/redpandadata/redpanda:{pins['REDPANDA_VERSION']}",
                # `--memory` is redpanda's OWN ceiling, and it is not optional
                # alongside mem_limit: seastar reserves against what it thinks
                # the machine has, so an uncapped broker sizes itself to the
                # host and dies at the cgroup edge instead of shedding.
                "mem_limit": "1536m",
                "command": ["redpanda", "start", "--mode=dev-container", "--smp=1",
                            "--memory=1G", "--reserve-memory=0M",
                            "--kafka-addr=INTERNAL://0.0.0.0:9092",
                            f"--advertise-kafka-addr=INTERNAL://{broker}:9092"],
                "healthcheck": {"test": ["CMD-SHELL", "rpk cluster health | grep -q 'Healthy:.*true'"],
                                "interval": "5s", "timeout": "5s", "retries": 30},
            }
            if v.get("seed"):
                services[f"{name}-seed"] = {
                    "image": f"python:{pins.get('PYTHON_VERSION', '3.12')}-slim",
                    "depends_on": {db: {"condition": "service_healthy"},
                                   connect: {"condition": "service_healthy"}},
                    "environment": {
                        "ERP_DSN": (f"host={db} port=5432 dbname={v.get('db_name','erp')} "
                                    f"user={v.get('db_user','contoso')} "
                                    f"password={v.get('db_password','contoso-erp-dev')}"),
                        "ERP_CONNECT_URL": f"http://{connect}:8083",
                        "ERP_DB_HOST": db,
                        "PYTHONUNBUFFERED": "1",
                    },
                    "volumes": [f"{sources_dir}:/sources:rw"],
                    "working_dir": "/sources",
                    # Installs the vendor's own generators, then runs its seeder.
                    # `restart: no` and a one-shot command: this is a step, not
                    # a service, and it must not loop if the replay fails.
                    # ONE INTERPRETER throughout. fixtures.py installs the
                    # generators into the project venv `uv sync` creates, so
                    # running the seeder with the system python afterwards
                    # cannot see them -- which is exactly the
                    # `ModuleNotFoundError: No module named 'erp_system'` this
                    # command produced on its first run. `uv run` puts both on
                    # the same side of that line.
                    "command": ["sh", "-c",
                                "pip install --quiet uv && "
                                "uv sync --quiet && "
                                # --frozen --no-sync, and it is load-bearing.
                                # A bare `uv run` RE-SYNCS and prunes anything
                                # not in the lock -- evicting the generators and
                                # psycopg that the two lines above just
                                # installed, then failing with
                                # ModuleNotFoundError for a package that was
                                # present moments earlier. The sibling platform
                                # documents this exact trap in its Makefile.
                                "uv run --frozen --no-sync python scripts/fixtures.py && "
                                # AFTER fixtures.py, never before. That script
                                # calls `uv sync` ITSELF to guarantee a venv,
                                # and a sync prunes everything absent from the
                                # lock -- so a psycopg installed first is gone
                                # by the time the seeder imports it, which is
                                # what `ModuleNotFoundError: No module named
                                # 'psycopg'` meant on two separate runs. The
                                # --frozen --no-sync flags above stop the two
                                # `uv run` calls from pruning; they cannot stop
                                # a sync the script performs internally.
                                "uv pip install --quiet 'psycopg[binary]' && "
                                "uv run --frozen --no-sync python scripts/seed_erp.py"],
                    "restart": "no",
                }
            services[connect] = {
                "image": f"debezium/connect:{pins['DEBEZIUM_VERSION']}",
                "depends_on": {db: {"condition": "service_healthy"},
                               broker: {"condition": "service_healthy"}},
                # A JVM: the cgroup cap alone is what G31 proved insufficient,
                # so the heap is bounded too.
                "mem_limit": "1536m",
                "environment": {
                    "JAVA_OPTS": "-Xmx900m -XX:MaxMetaspaceSize=256m",
                    "BOOTSTRAP_SERVERS": f"{broker}:9092",
                    "GROUP_ID": v["name"],
                    "CONFIG_STORAGE_TOPIC": "_connect_configs",
                    "OFFSET_STORAGE_TOPIC": "_connect_offsets",
                    "STATUS_STORAGE_TOPIC": "_connect_status",
                    "CONFIG_STORAGE_REPLICATION_FACTOR": "1",
                    "OFFSET_STORAGE_REPLICATION_FACTOR": "1",
                    "STATUS_STORAGE_REPLICATION_FACTOR": "1"},
                "healthcheck": {"test": ["CMD-SHELL", "curl -sf http://localhost:8083/connectors || exit 1"],
                                "interval": "10s", "timeout": "5s", "retries": 30},
            }
        else:
            raise SystemExit(
                f"platform: vendor {v['name']!r} declares kind={kind!r}, which this "
                f"platform does not know how to run. Add it here or fix the "
                f"declaration; guessing would stand up the wrong vendor.")
    return {"services": services}


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit("usage: sources.py <path-to-sources.yaml> <sources-dir-abs>")
    decl = _load(pathlib.Path(sys.argv[1]))
    if not decl["vendors"]:
        sys.exit("platform: that sources.yaml declares no vendors")
    # The simulator version comes from the SOURCES repo. A platform that
    # defaulted it would be deciding what the vendor is, and a wrong guess
    # fails at pull time with `manifest unknown` -- which is how this line got
    # written, after inventing a tag that does not exist.
    versions = pathlib.Path(sys.argv[2]) / "versions.env"
    pins = dict(
        line.split("=", 1) for line in versions.read_text().splitlines()
        if "=" in line and not line.strip().startswith("#")
    ) if versions.exists() else {}
    pins = {k.strip(): val.strip() for k, val in pins.items()}
    # Every image this platform starts on a product's behalf is pinned by the
    # SOURCES repo. A platform defaulting any of them would be deciding what
    # the vendor is -- and a guessed tag fails at pull time with `manifest
    # unknown`, which is how this check came to exist.
    needed = {"openapi": ["MOKAPI_VERSION"],
              "cdc": ["POSTGRES_VERSION", "REDPANDA_VERSION", "DEBEZIUM_VERSION"]}
    for v in decl["vendors"]:
        for key in needed.get(v.get("kind"), []):
            if key not in pins:
                sys.exit(f"platform: vendor {v['name']!r} is kind={v.get('kind')!r} but "
                         f"{versions} does not pin {key}; this platform will not guess it")
    print(json.dumps(fragment(decl, sys.argv[2], pins), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
