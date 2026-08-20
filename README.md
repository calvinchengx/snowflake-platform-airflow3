# snowflake-platform-airflow3

Runs a data product on **snowflake-emulator (or a real Snowflake account),
orchestrated by an external Apache Airflow 3** — the Snowflake counterpart of
[`fabric-platform-airflow3`](https://github.com/calvinchengx/fabric-platform-airflow3)
and [`databricks-platform-airflow3`](https://github.com/calvinchengx/databricks-platform-airflow3).

Distinct from [`snowflake-platform-tasks`](https://github.com/calvinchengx/snowflake-platform-tasks),
which drives the same product through Snowflake's own Tasks. Same product, two
orchestrators — and that is the only variable between them.

The platform installs the product and knows no Contoso.

```sh
make up     PRODUCT=../contoso-data-product-snowflake-airflow3
make verify PRODUCT=../contoso-data-product-snowflake-airflow3 DAG=contoso_daily
make down   PRODUCT=../contoso-data-product-snowflake-airflow3
```

`PRODUCT` is a **path**, not a name. This repository contains no product
identifier — the property that makes "a second product can use this platform
unchanged" a fact rather than an aspiration.

## What the product gets, and how

Two mechanisms that are routinely conflated:

| | |
|---|---|
| its **DAG files** | a bundle — a bind mount here, `GitDagBundle` in production |
| its **dependencies** | `uv pip install` of its `pyproject.toml` into the worker |

The bundle delivers files and installs nothing. A product whose DAGs import a
library the worker lacks parses fine and fails at run time.

## What crosses the boundary

A product does not reach into a platform. Three things are handed over, all by
name:

| | |
|---|---|
| connection `snowflake` | host, credential and warehouse. The PAT is read from the emulator's volume and put in a Connection; in production the same `conn_id` carries a real account's |
| one connection per vendor | named by `contoso-sources`' own declaration, so production points the same names at the real vendors |
| `PRODUCT_STAGE` | where the internal stage is mounted |

## The one thing that differs from the Tasks cell

**The stage is a volume, not a host directory.**

`snowflake-platform-tasks` runs its steps on the host, so ingest writes into a
host directory bind-mounted into the warehouse. Here ingest runs *inside the
worker*, so a host path would mean the worker writing one filesystem and the
warehouse reading another — and the symptom would not be an error, it would be
a `COPY INTO` that loads **zero rows** into an empty bronze.

One named volume, mounted into both services. `test_the_stage_is_one_volume_not_two_paths`
holds it: two mounts, one path, by construction rather than by two
configurations agreeing.

## Ports

Chosen so this stack and its siblings can all be up at once — which is how
their numbers get compared without tearing one down.

| | |
|---|---|
| Airflow | `18084` (fabric-airflow3 has `18080`, databricks-airflow3 `18082`) |
| emulator | `18449` (the Tasks platform has `18448`) |

Vendors are `expose`d, not published: the worker reaches them by service name
inside the compose network.

## `make verify` can go red

It triggers a run **by an explicit `--run-id`** and waits for that run's
verdict, then prints which task instances failed and which were merely blocked
behind them. It does not unpause: unpausing changes the schedule and starts a
catch-up run alongside, and two runs writing one warehouse is not a witness.

## Where this fits

- [The family](https://github.com/calvinchengx/contoso-data-product/blob/main/docs/00-family.md)
- [The plan](https://github.com/calvinchengx/contoso-data-product/blob/main/docs/01-plan.md)

Apache-2.0.
