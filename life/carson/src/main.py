"""Entrypoints. One image, three jobs, selected by argv[1].

    serve    the API + UI + feed (the Deployment)
    remind   walk the date ladder and text what is due (the CronJob)
    init     create the schema and exit

`remind` is a separate process rather than a thread inside `serve` on purpose:
a reminder that silently stopped firing because a background task died in a
long-lived pod is exactly the failure this tool must not have. As a CronJob it
either ran or it did not, and Kubernetes says which.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date

logging.basicConfig(
    level=os.environ.get("CARSON_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("carson")


def cmd_serve() -> int:
    import uvicorn
    import db
    db.init()
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        # NOT "CARSON_PORT". Kubernetes injects Docker-link-style env vars for
        # every Service in the namespace, and the Service here is called
        # `carson` — so CARSON_PORT arrives as "tcp://10.43.x.x:8080" and
        # int() dies on it. The pod specs also set enableServiceLinks: false,
        # but this name stays collision-proof regardless of that.
        port=int(os.environ.get("CARSON_HTTP_PORT", "8080")),
        log_level=os.environ.get("CARSON_LOG_LEVEL", "info").lower(),
        # One SQLite writer. Extra workers would be extra writers.
        workers=1,
    )
    return 0


def cmd_remind() -> int:
    """Ask the running web pod to walk the ladder.

    This deliberately does NOT open the database. The PVC is ReadWriteOnce, so
    a CronJob pod that mounts it can only start when the scheduler puts it on
    the same node as the web pod; otherwise it hangs in ContainerCreating and
    the reminder never goes out, with no error anyone sees until a birthday is
    missed. Calling the API instead removes the node coupling entirely and
    keeps a single SQLite writer.

    Exit status still carries the signal: a failed call is a failed Job.
    """
    import json
    import urllib.error
    import urllib.request

    base = os.environ.get("CARSON_API_URL", "http://carson:8080").rstrip("/")
    body = {}
    if override := os.environ.get("CARSON_TODAY"):
        body["today"] = override
        logger.warning("CARSON_TODAY override: %s", override)

    req = urllib.request.Request(
        f"{base}/api/v1/reminders/run",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read() or b"{}")
        logger.info("reminder sweep complete: %s sent (date %s)",
                    data.get("sent"), data.get("date"))
        return 0
    except urllib.error.HTTPError as exc:
        logger.error("carson API %s: %s", exc.code,
                     exc.read()[:400].decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        logger.error("carson API unreachable at %s: %s", base, exc)
    return 1


def cmd_init() -> int:
    import db
    db.init()
    logger.info("schema ready at %s", db.DEFAULT_DB)
    return 0


COMMANDS = {"serve": cmd_serve, "remind": cmd_remind, "init": cmd_init}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd not in COMMANDS:
        print(f"usage: {sys.argv[0]} [{'|'.join(COMMANDS)}]", file=sys.stderr)
        return 2
    return COMMANDS[cmd]()


if __name__ == "__main__":
    raise SystemExit(main())
