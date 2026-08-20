#!/usr/bin/env bash
# Bring up an Airflow 3 stack and provision the connections the product asks
# for by name. Everything here is platform business; the product never sees it.
set -euo pipefail

airflow db migrate >/dev/null

# api-server FIRST: in Airflow 3 the scheduler hands tasks to it over HTTP, and
# a worker with nothing listening fails every task with `Connection refused`.
# ORDER MATTERS, and getting it wrong is a race a restart loses every time.
#
# The scheduler must not start until the connections exist. This platform ships
# its DAGs PAUSED (`DAGS_ARE_PAUSED_AT_CREATION: true`) which removes most of
# that risk, but the ordering is kept anyway: a product that ships an unpaused
# DAG should not be the thing that discovers this.
airflow api-server &

for _ in $(seq 1 60); do
  curl -sf http://localhost:8080/api/v2/monitor/health >/dev/null 2>&1 && break
  sleep 2
done

# THE CREDENTIAL, handed over rather than reached for. The emulator writes its
# admin PAT into its own data directory; that volume is mounted read-only here
# so the platform can read it once and put it in a Connection. The product asks
# for `snowflake` and learns nothing about where the token came from -- in
# production the same conn_id carries a real account's credential and not one
# line of product code changes.
#
# WAITED FOR, not assumed. `depends_on: service_healthy` means the emulator is
# answering, which is not the same as having written this file: healthcheck and
# first write are different moments, and the gap is small enough to pass by
# luck most of the time. A missing PAT here would surface four tasks later as
# an authentication failure, which reads like a wrong password.
PAT=""
for _ in $(seq 1 60); do
  if [ -s /emu-data/admin.pat ]; then PAT="$(cat /emu-data/admin.pat)"; break; fi
  sleep 2
done
if [ -z "$PAT" ]; then
  echo "platform: WARNING -- no /emu-data/admin.pat after 120s; the 'snowflake'" \
       "connection will carry no credential and every task will fail to authenticate" >&2
fi

airflow connections delete snowflake >/dev/null 2>&1 || true
airflow connections add snowflake \
  --conn-type generic \
  --conn-host "${SNOWFLAKE_EMULATOR_URL}" \
  --conn-password "$PAT" \
  --conn-extra "$(python3 - <<'PY'
import json, os
url = os.environ["SNOWFLAKE_EMULATOR_URL"]
host = url.split("://", 1)[-1]
hostname, _, port = host.partition(":")
print(json.dumps({
    "url": url,
    # dbt-snowflake wants a hostname and a port, not a URL. Split here rather
    # than in the product: which half of a URL an adapter wants is a property
    # of this deployment, and a real account supplies a hostname with no port
    # at all.
    "host": hostname,
    "port": int(port or 443),
    "account": os.environ.get("SNOWFLAKE_ACCOUNT", "test"),
    "user": os.environ.get("SNOWFLAKE_USER", "admin"),
    "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "contoso_warehouse"),
    "database": os.environ.get("SNOWFLAKE_DATABASE", "TEST_DB"),
    "target": os.environ.get("SNOWFLAKE_TARGET", "emulator"),
    # THE STAGE, told to the product rather than guessed by it. Here it is a
    # volume both containers mount; in production it is a named internal stage
    # and this is where that difference lives.
    "stage": os.environ.get("PRODUCT_STAGE", "/stages"),
}))
PY
)" >/dev/null
echo "platform: connection 'snowflake' provisioned -> ${SNOWFLAKE_EMULATOR_URL}"

# ONE CONNECTION PER DECLARED VENDOR. The product's DAG asks for these by the
# name the declaration gives them and learns nothing else -- so in production
# the same names are provisioned against the real vendors and no DAG changes.
if [ -f "${SOURCES_DECL:-/nonexistent}" ]; then
  python3 - <<'PYEOF'
import json, os, pathlib, subprocess

decl = pathlib.Path(os.environ["SOURCES_DECL"])
root = decl.parent
vendors, cur = [], None
for raw in decl.read_text().splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip() or line.strip() == "vendors:" or line.startswith("version:"):
        continue
    t = line.strip()
    if t.startswith("- "):
        cur = {}; vendors.append(cur); t = t[2:]
    if cur is None or ":" not in t:
        continue
    k, _, v = t.partition(":")
    cur[k.strip()] = v.strip()

for v in vendors:
    if v.get("kind") == "cdc":
        # A stream vendor has no base URL. Its broker and topic ride in the
        # connection extra the way an HTTP vendor's URL rides in its host --
        # same seam, so in production these point at the real ERP's stream.
        name = v["name"].replace("_", "-")
        subprocess.run(["airflow", "connections", "delete", v["conn"]], capture_output=True)
        r = subprocess.run(["airflow", "connections", "add", v["conn"],
                            "--conn-type", "generic",
                            "--conn-extra", json.dumps({
                                "bootstrap": f"{name}-broker:9092",
                                "topic": v.get("topic", ""),
                            })], capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"platform: connection {v['conn']!r} -> {name}-broker:9092 ({v.get('topic')})"
              if ok else
              f"platform: WARNING could not provision {v['conn']!r}: "
              f"{(r.stderr or r.stdout).strip()[:200]}", flush=True)
        continue
    if v.get("kind") != "openapi":
        continue
    host = f"http://{v['name'].replace('_','-')}:{v['port']}"
    # The vendor's own credential, from its fixture directory. Each vendor has
    # its own key that rotates separately -- that is the point of there being
    # more than one vendor, and sharing one here would erase it.
    key_file = root / v["data"] / ".api-key"
    key = key_file.read_text().strip() if key_file.exists() else ""
    # IDEMPOTENT, and LOUD when it fails. Provisioning runs on every start
    # against a metadata DB that may already carry these: an existing
    # connection is the normal case on restart, not an error.
    subprocess.run(["airflow", "connections", "delete", v["conn"]], capture_output=True)
    r = subprocess.run(["airflow", "connections", "add", v["conn"],
                        "--conn-type", "http", "--conn-host", host,
                        "--conn-password", key], capture_output=True, text=True)
    if r.returncode != 0:
        # Report and carry on: one vendor that cannot be provisioned should fail
        # ITS OWN tasks with a missing-connection error, not stop the platform.
        print(f"platform: WARNING could not provision {v['conn']!r}: "
              f"{(r.stderr or r.stdout).strip()[:300]}", flush=True)
    else:
        print(f"platform: connection {v['conn']!r} provisioned -> {host}", flush=True)
PYEOF
fi

# Only now is it safe to let anything run.
airflow scheduler &
airflow dag-processor &

echo "platform: ready. Airflow UI on :8080 (published as ${AIRFLOW_PORT:-18084})"
wait -n
