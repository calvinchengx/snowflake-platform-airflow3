"""Repo-boundary tests. No Docker, no emulator, no product."""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def pins() -> dict[str, str]:
    out = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_pins_are_immutable():
    p = pins()
    assert "SNOWFLAKE_EMULATOR_VERSION" in p
    assert "AIRFLOW_VERSION" in p
    for k, v in p.items():
        assert v.lower() not in {"latest", "stable", "main", "edge"}, f"{k}={v}"


def test_compose_reads_every_pin():
    composed = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    composed += (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    for k in pins():
        assert k in composed, k


def test_the_platform_holds_no_product():
    """A platform holds no Contoso name and no product file.

    `00-family.md`'s split line. The vendors repository may be named -- this
    platform consumes one -- but a product may not.
    """
    assert not (ROOT / "platform").exists()
    assert not (ROOT / "dags").exists(), "a dags/ directory is the PRODUCT's"
    for f in (ROOT / "Makefile", ROOT / "docker-compose.yml"):
        for line in f.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if "contoso" in code.lower() and "contoso-sources" not in code:
                raise AssertionError(f"{f.name} names a product: {line.strip()!r}")


def test_the_product_is_supplied_as_a_path():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^PRODUCT \?= \./product$", makefile, re.M)


def test_the_stage_is_one_volume_not_two_paths():
    """The coupling that broke the Tasks cell, prevented by construction here.

    In `snowflake-platform-tasks` the steps run on the HOST, so the stage is a
    host directory that both halves name -- and before the split they named it
    by two independent derivations that agreed by accident. Here ingest runs
    INSIDE the worker, so a host path would mean the worker writing one
    filesystem and the warehouse reading another. The symptom would not be an
    error: it would be a `COPY INTO` that loads zero rows.

    One named volume, mounted into both services, cannot drift.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^volumes:.*^  stage-data:", compose, re.M | re.S), (
        "stage-data must be a named volume"
    )
    mounts = re.findall(r"- stage-data:(\S+)", compose)
    # The writer (worker), the reader (warehouse) and the one-shot that makes it
    # writable. What matters is not how many mount it but that they AGREE:
    # one path, so there is nothing for two configurations to drift about.
    assert len(mounts) >= 2, f"the stage must be mounted into at least two services: {mounts}"
    assert len(set(mounts)) == 1, f"services mount the stage at different paths: {mounts}"


def test_the_stage_is_made_writable_before_anything_mounts_it():
    """A named volume is created root-owned and 0755, and neither image is root.

    Measured rather than anticipated: the first real run failed all four `land`
    tasks with `PermissionError: [Errno 13] Permission denied:
    '/stages/contoso_pos_customers'`. The Tasks platform chmods its host stage
    directory for the same reason; a volume needs it done once, by something
    that is root, before anything else writes.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "stage-init:" in compose, "nothing makes the stage writable"
    assert "chmod 0777 /stages" in compose
    assert "stage-init: {condition: service_completed_successfully}" in compose, (
        "the worker must wait for the one-shot to FINISH -- ingest's first act "
        "is to create a directory in the stage"
    )


def test_the_worker_is_told_where_the_stage_is():
    """The product reads PRODUCT_STAGE; the platform sets it to the mount."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"PRODUCT_STAGE: (\S+)", compose)
    assert m, "the worker is not told where the stage is"
    mounts = set(re.findall(r"- stage-data:(\S+)", compose))
    assert m.group(1) in mounts, (
        f"PRODUCT_STAGE={m.group(1)} is not where stage-data is mounted ({mounts})"
    )


def test_ports_do_not_collide_with_the_sibling_platforms():
    """Four stacks share this machine and two of them are Snowflake.

    Comparing this cell's numbers against the Tasks cell's means having both up
    at once. A port they share turns that into a `make up` that fails on the
    second stack, or worse, one that silently talks to the other's warehouse.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    published = set(re.findall(r'"\$\{[A-Z_]+:-(\d+)\}:\d+"', compose))
    published |= set(re.findall(r'"(\d{4,5}):\d+"', compose))
    taken = {
        "18448": "snowflake-platform-tasks (the emulator)",
        "18080": "fabric-platform-airflow3 (Airflow)",
        "18082": "databricks-platform-airflow3 (Airflow)",
        "18586": "snowflake-platform-tasks (OpenMetadata)",
    }
    clashes = {p: taken[p] for p in published if p in taken}
    assert not clashes, f"host port already used by: {clashes}"


def test_the_worker_installs_the_project_not_just_its_dependencies():
    """`uv pip install -r pyproject.toml` installs the LIST and not the package.

    A DAG importing the product's own modules then dies at run time with
    ModuleNotFoundError while the bundle, the deps and the parse all look fine.
    """
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "-r /opt/build/pyproject.toml" not in dockerfile
    assert "uv pip install" in dockerfile and "/opt/build" in dockerfile


def test_start_provisions_the_connection_before_the_scheduler_runs():
    """Order is the whole point, and getting it wrong is a race.

    A scheduler that starts first can fire a run against a stack with no
    connections, and the task fails with `The conn_id 'snowflake' isn't
    defined` -- which reads like a fault in the product.
    """
    start = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    conn = start.index("connections add snowflake")
    sched = start.index("airflow scheduler")
    api = start.index("airflow api-server")
    assert api < conn < sched, (
        "api-server must come first, connections next, scheduler last"
    )


def test_the_credential_is_waited_for_rather_than_assumed():
    """`service_healthy` is not the same moment as `admin.pat` existing.

    The gap is small enough to pass by luck, and a missing PAT surfaces four
    tasks later as an authentication failure -- which reads like a wrong
    password rather than a platform that read too early.
    """
    start = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    assert "/emu-data/admin.pat" in start
    assert re.search(r"for _ in \$\(seq 1 \d+\); do\n\s+if \[ -s /emu-data/admin.pat", start), (
        "the PAT must be waited for, not read once"
    )
