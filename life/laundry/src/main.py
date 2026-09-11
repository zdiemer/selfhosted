"""Entrypoint. One process, two listeners, two background tasks.

    serve   the whole thing (the Deployment)
    init    create the schema and exit
    check   render the configured machines and the SMS wiring, then exit —
            a dry pre-flight for `helm upgrade --set ...` tuning changes

Both HTTP listeners run in ONE process on purpose. They share the in-memory
machine state and a single SQLite writer; splitting them into two pods would
mean two writers on a ReadWriteOnce volume and a state machine that only sees
half its input. The split is about which port is reachable from where (see
api.py), not about isolation of the work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=os.environ.get("LAUNDRY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("laundry")


def cmd_serve() -> int:
    import uvicorn

    import api
    import engine

    engine.init()

    # NOT "LAUNDRY_PORT": Kubernetes injects Docker-link-style env vars for
    # every Service in the namespace, so a Service named `laundry` would land
    # LAUNDRY_PORT="tcp://10.43.x.x:8080" and int() would die on it. The pod
    # also sets enableServiceLinks: false, but this name is safe regardless.
    port = int(os.environ.get("LAUNDRY_HTTP_PORT", "8080"))
    ingest_port = int(os.environ.get("LAUNDRY_INGEST_PORT", "8081"))
    log_level = os.environ.get("LAUNDRY_LOG_LEVEL", "info").lower()

    async def run() -> None:
        servers = [
            uvicorn.Server(uvicorn.Config(
                api.app, host="0.0.0.0", port=port, log_level=log_level)),
            uvicorn.Server(uvicorn.Config(
                api.ingest_app, host="0.0.0.0", port=ingest_port,
                # The device posts every few seconds forever; at INFO each one
                # is an access-log line, which is ~17k lines a day per machine
                # shipped to Grafana Cloud for no information at all.
                log_level="warning")),
        ]
        logger.info("dashboard on :%d, device ingest on :%d", port, ingest_port)
        await asyncio.gather(
            *(s.serve() for s in servers),
            engine.sweep_forever(),
            engine.prune_forever(),
        )

    asyncio.run(run())
    return 0


def cmd_init() -> int:
    import db
    db.init()
    logger.info("schema ready at %s", os.environ.get("LAUNDRY_DB"))
    return 0


def cmd_check() -> int:
    import config
    machines = config.load_machines()
    print(f"machines: {len(machines)}")
    for cfg in machines.values():
        print(f"  {cfg.id:10s} threshold={cfg.threshold_mg:>6.1f} mg  "
              f"start={cfg.start_seconds:>5.0f}s  quiet={cfg.quiet_seconds:>5.0f}s  "
              f"min_run={cfg.min_run_seconds:>5.0f}s")
        print(f"             message: {cfg.message}")
    print(f"sms url:   {config.SMS_URL}")
    print(f"sms key:   {'set' if config.SMS_API_KEY else 'MISSING'}")
    print(f"sms to:    {'set' if config.SMS_TO else 'MISSING'}")
    print(f"ingest tok:{'set' if config.INGEST_TOKEN else 'MISSING'}")
    print(f"dry run:   {config.DRY_RUN}")
    return 0


COMMANDS = {"serve": cmd_serve, "init": cmd_init, "check": cmd_check}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if name not in COMMANDS:
        print(f"usage: main.py [{'|'.join(COMMANDS)}]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(COMMANDS[name]())
