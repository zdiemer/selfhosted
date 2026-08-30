"""Two HTTP surfaces on two ports, and the split is the security model.

  ingest_app  :8081  ONE route, POST /api/v1/samples, bearer-token only.
                     This is the port the LoadBalancer publishes on the node
                     IPs (see templates/service-ingest.yaml) because the ESP32
                     cannot reach anything else: every *.zachd.duckdns.org name
                     resolves to a TAILSCALE address, and the cluster advertises
                     no subnet routes, so a device on the LAN that cannot run
                     tailscaled can only get in by IP. Publishing `app` there
                     instead would put the dashboard and the cycle history on an
                     unauthenticated LAN port to save one Service object.

  app         :8080  Dashboard, JSON API and /metrics, on a ClusterIP, reached
                     only through the Traefik ingress behind Authelia. No login
                     code lives here and none should — same arrangement as
                     life/carson and docs/stirling-pdf.

Nothing on the ingest port can read anything back: it answers with the machine's
own state (so the device can drive its status LED) and nothing else.
"""

from __future__ import annotations

import hmac
import logging
import time

from fastapi import Body, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse, Response

import config
import db
import engine
import metrics
import notify
from detect import RUNNING

logger = logging.getLogger("laundry.api")

STATIC_DIR = __file__.rsplit("/", 1)[0] + "/static"

app = FastAPI(title="laundry", docs_url=None, redoc_url=None)
ingest_app = FastAPI(title="laundry-ingest", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# Device plane
# --------------------------------------------------------------------------
def _require_token(authorization: str | None, x_device_token: str | None) -> None:
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_device_token:
        presented = x_device_token.strip()

    if not config.INGEST_TOKEN:
        # Refuse rather than accept-everything. An unset token on a listener
        # published to the LAN is the one default that must not quietly work.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "LAUNDRY_INGEST_TOKEN is not configured")
    if not presented or not hmac.compare_digest(presented, config.INGEST_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad device token")


@ingest_app.post("/api/v1/samples")
async def post_sample(
    payload: dict = Body(...),
    authorization: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
) -> dict:
    """One vibration window from one sensor.

    The device sends no timestamp — it has no RTC and no NTP, only milliseconds
    since boot — so arrival time IS the sample time. That also means a sample
    delayed by a slow retry is treated as arriving late, which is correct: the
    state machine cares when the evidence reached us, not when it was measured.
    """
    _require_token(authorization, x_device_token)

    device = str(payload.get("device", "")).strip()
    if device not in engine.MACHINES:
        # Naming an unknown machine is a config mismatch between the flashed
        # DEVICE_ID and values.yaml, and it would otherwise be a silent no-op.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown device {device!r}; configured: {sorted(engine.MACHINES)}",
        )
    try:
        rms_mg = float(payload["rms_mg"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "rms_mg is required and must be a number") from None

    def _opt(key, cast):
        try:
            return cast(payload[key])
        except (KeyError, TypeError, ValueError):
            return None

    st = engine.record_sample(
        device,
        rms_mg,
        _opt("peak_mg", float),
        window_ms=_opt("window_ms", int),
        seq=_opt("seq", int),
        rssi=_opt("rssi", int),
        temp_c=_opt("temp_c", float),
    )
    # Echoed back so the firmware can show state on the onboard LED without
    # holding any opinion about thresholds itself.
    return {"ok": True, "device": device, "state": st.state,
            "running": st.state == RUNNING}


@ingest_app.get("/healthz")
async def ingest_health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Control plane
# --------------------------------------------------------------------------
def _state_payload() -> list[dict]:
    now = time.time()
    out = []
    for device, cfg in engine.MACHINES.items():
        st = engine.STATES[device]
        out.append({
            "device": device,
            "label": cfg.label,
            "state": st.state,
            "running": st.state == RUNNING,
            "online": st.online,
            "last_rms_mg": round(st.last_rms_mg, 1),
            "threshold_mg": cfg.threshold_mg,
            "last_sample_age_s": round(now - st.last_sample_at, 1) if st.last_sample_at else None,
            "cycle_started_at": st.cycle_started_at,
            "cycle_elapsed_s": round(now - st.cycle_started_at) if st.cycle_started_at else None,
            "cycle_peak_mg": round(st.cycle_peak_mg, 1),
            # Surfaced because it is the single most useful number while tuning:
            # how much of the quiet timer has run down right now.
            "quiet_for_s": round(now - st.quiet_since) if st.quiet_since else None,
            "quiet_seconds": cfg.quiet_seconds,
            "start_seconds": cfg.start_seconds,
            "min_run_seconds": cfg.min_run_seconds,
        })
    return out


@app.get("/api/v1/state")
async def get_state() -> dict:
    return {"machines": _state_payload(), "now": time.time()}


@app.get("/api/v1/cycles")
async def get_cycles(
    device: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=500),
) -> dict:
    rows = db.recent_cycles(device, limit)
    return {"cycles": [dict(r) for r in rows]}


@app.get("/api/v1/trace")
async def get_trace(
    device: str = Query(...),
    minutes: float = Query(default=120, ge=1, le=10080),
) -> dict:
    """The vibration trace. This is the tuning instrument — see the README."""
    if device not in engine.MACHINES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    rows = db.recent_samples(device, time.time() - minutes * 60)
    return {
        "device": device,
        "threshold_mg": engine.MACHINES[device].threshold_mg,
        "points": [
            {"t": r["received_at"], "rms_mg": r["rms_mg"], "peak_mg": r["peak_mg"]}
            for r in rows
        ],
    }


@app.post("/api/v1/test-notify")
async def test_notify(device: str = Query(...)) -> dict:
    """Send a real text right now, without running a load.

    Exists because every other way of testing this path takes an hour and a
    basket of towels. Uses a synthetic start time so it never collides with a
    real cycle's idempotency key.
    """
    if device not in engine.MACHINES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    cfg = engine.MACHINES[device]
    now = time.time()
    ok, error = notify.send(cfg, now - 2820, now)
    return {"ok": ok, "error": error, "body": notify.compose(cfg, now - 2820, now)}


@app.get("/healthz")
async def health() -> dict:
    return {
        "status": "ok",
        "machines": len(engine.MACHINES),
        "sms_configured": bool(config.SMS_API_KEY and config.SMS_TO) or config.DRY_RUN,
    }


@app.get("/metrics")
async def prometheus() -> Response:
    body = metrics.render(
        engine.MACHINES,
        engine.STATES,
        {d: db.cycle_count(d) for d in engine.MACHINES},
    )
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    with open(f"{STATIC_DIR}/index.html", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())
