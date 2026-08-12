"""The run page — one URL per digest, so the SMS can be a single short link.

Why this exists: SMS is a terrible container for a list of listings. Every URL
is ~90 characters, four listings is four concatenated segments, and carriers
score that as spam (they did, and started dropping messages). One link is one
segment and carries more information than a dozen texts could.

Each send window mints a token and stamps it on exactly the listings that went
out, so `/r/<token>` is a permanent, self-contained record of that run — not a
live feed that changes under you after you've been told to look at it.

No auth by design: a token is unguessable, the contents are public rental
listings, and requiring a login on a link you tap from a text is the fastest
way to make it unused.

Read-only against the same SQLite file the CronJob writes. WAL mode makes that
safe for a reader alongside the single writer.
"""

from __future__ import annotations

import html
import logging
import os
import sqlite3
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, RedirectResponse

# Small in-process cache so re-opening a run page doesn't re-fetch every photo.
# Bounded because this pod has a 256Mi limit.
_IMG_CACHE: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_IMG_CACHE_MAX = 120

logger = logging.getLogger("apartment-watch.web")

DB_PATH = os.environ.get("APARTMENT_WATCH_DB", "/data/apartment-watch.db")
TZ = os.environ.get("APARTMENT_WATCH_TZ", "America/Los_Angeles")

# Where this is reachable from outside, for og:image — which has to be an
# absolute URL or scrapers drop it and the preview falls back to a grey box.
# Set by the chart from the ingress host; the default matches criteria.yaml's
# alerts.web_base_url, which is what the SMS link is built from. If those two
# ever disagree, the link works and the preview points at the wrong host.
OG_BASE = os.environ.get("APARTMENT_WATCH_BASE_URL", "https://homes.diemer.codes").rstrip("/")

app = FastAPI(title="homes", docs_url=None, redoc_url=None, openapi_url=None)


def _connect() -> sqlite3.Connection:
    # Read-only URI: the CronJob owns writes, and a stray write from the web
    # process could corrupt the dedup history that stops duplicate alerts.
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


# ---------------------------------------------------------------- formatting

def _money(v) -> str:
    try:
        return f"${int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _beds(n) -> str:
    if n == 0:
        return "Studio"
    if n is None:
        return "—"
    return f"{int(n)} bed"


LAUNDRY_LABEL = {
    "in_unit": "In-unit laundry",
    "in_building": "Laundry in building",
    "on_site": "Laundry on site",
}
PARKING_LABEL = {
    "garage": "Garage",
    "carport": "Carport",
    "off_street": "Off-street parking",
    "valet": "Valet parking",
}


def _chips(row) -> list[tuple[str, str]]:
    """(label, kind) — kind drives colour. 'good' is a perk, 'flag' needs a decision."""
    out: list[tuple[str, str]] = []
    if row["laundry"] in LAUNDRY_LABEL:
        out.append((LAUNDRY_LABEL[row["laundry"]], "good"))
    if row["parking"] in PARKING_LABEL:
        label = PARKING_LABEL[row["parking"]]
        if row["parking_fee"]:
            label += f" +{_money(row['parking_fee'])}/mo"
        out.append((label, "good"))
    if row["rent_controlled"]:
        year = row["year_built"]
        out.append((f"Likely rent-controlled{f' · built {year}' if year else ''}", "good"))
    if row["note"]:
        # BMR deadlines and income limits: the thing you must act on.
        for part in str(row["note"]).split(", "):
            if part.strip():
                out.append((part.strip().capitalize(), "flag"))
    return out


def _host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0].replace("www.", "")
    except IndexError:
        return ""


def _local(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return (
            datetime.fromisoformat(ts)
            .astimezone(ZoneInfo(TZ))
            .strftime("%-I:%M %p on %A %-d %B")
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------- page

STYLE = """
:root{
  /* Cool, blue-biased neutrals — SF fog rather than the usual warm cream.
     One accent (deep green) carries price and perks; amber is reserved for
     things that need a decision, so the two never compete. */
  --ink:#12161C; --ink-2:#39424F; --fog:#5B6472; --line:#E2E6EC;
  --paper:#F7F8FA; --card:#FFFFFF;
  --accent:#0B6E4F; --accent-soft:#E6F2ED;
  --flag:#8A5200; --flag-soft:#FBF0DF;
  --shadow:0 1px 2px rgba(18,22,28,.06), 0 8px 24px -12px rgba(18,22,28,.18);
}
@media (prefers-color-scheme: dark){
  :root{
    --ink:#EDF0F4; --ink-2:#AFB8C6; --fog:#8792A2; --line:#252C36;
    --paper:#0D1116; --card:#151A21;
    --accent:#5FD3A6; --accent-soft:#122A22;
    --flag:#E9B067; --flag-soft:#2A2011;
    --shadow:0 1px 2px rgba(0,0,0,.5), 0 8px 24px -12px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"]{
  --ink:#EDF0F4; --ink-2:#AFB8C6; --fog:#8792A2; --line:#252C36;
  --paper:#0D1116; --card:#151A21;
  --accent:#5FD3A6; --accent-soft:#122A22;
  --flag:#E9B067; --flag-soft:#2A2011;
  --shadow:0 1px 2px rgba(0,0,0,.5), 0 8px 24px -12px rgba(0,0,0,.7);
}
:root[data-theme="light"]{
  --ink:#12161C; --ink-2:#39424F; --fog:#5B6472; --line:#E2E6EC;
  --paper:#F7F8FA; --card:#FFFFFF;
  --accent:#0B6E4F; --accent-soft:#E6F2ED;
  --flag:#8A5200; --flag-soft:#FBF0DF;
  --shadow:0 1px 2px rgba(18,22,28,.06), 0 8px 24px -12px rgba(18,22,28,.18);
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  /* Native stack on purpose: the audience opens this from a text on an
     iPhone, where this resolves to SF Pro. A webfont here would cost a
     round-trip on cellular to look less at home. */
  font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  font-size:17px; line-height:1.45;
  padding-bottom:max(2rem,env(safe-area-inset-bottom));
}
.wrap{max-width:34rem;margin:0 auto;padding:0 1rem}

header{padding:1.75rem 0 1rem}
.eyebrow{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--fog);margin:0 0 .4rem
}
h1{
  margin:0;font-size:1.85rem;line-height:1.15;letter-spacing:-.02em;
  font-weight:650;text-wrap:balance
}
.sub{margin:.4rem 0 0;color:var(--fog);font-size:.94rem}

.list{display:flex;flex-direction:column;gap:1rem;margin:1.25rem 0}

.card{
  display:block;background:var(--card);border:1px solid var(--line);
  border-radius:14px;overflow:hidden;text-decoration:none;color:inherit;
  box-shadow:var(--shadow);transition:transform .15s ease,border-color .15s ease
}
.card:hover,.card:focus-visible{transform:translateY(-2px);border-color:var(--accent)}
.card:focus-visible{outline:2px solid var(--accent);outline-offset:3px}

.figure{
  position:relative;aspect-ratio:16/10;background:var(--accent-soft);
  border-bottom:1px solid var(--line);
  display:flex;align-items:center;justify-content:center
}
/* The label sits underneath the photo, so a missing or blocked image reveals
   it with no JS and no layout shift. */
.figure::before{
  content:attr(data-label);color:var(--accent);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.78rem;letter-spacing:.06em;text-transform:uppercase
}
.shot{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}

.body{padding:.95rem 1rem 1.05rem}
.priceline{display:flex;align-items:baseline;gap:.55rem;flex-wrap:wrap}
.price{
  font-size:1.5rem;font-weight:680;letter-spacing:-.02em;color:var(--accent);
  font-variant-numeric:tabular-nums
}
.spec{color:var(--ink-2);font-size:.95rem}
.where{margin:.15rem 0 0;font-weight:560;font-size:1.02rem}
.dist{color:var(--fog);font-weight:420;font-size:.88rem;font-variant-numeric:tabular-nums}
.base{margin:.3rem 0 0;color:var(--fog);font-size:.84rem;font-variant-numeric:tabular-nums}

.chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:.7rem 0 0}
.chip{
  font-size:.775rem;padding:.24rem .55rem;border-radius:999px;
  background:var(--accent-soft);color:var(--accent);font-weight:560;
  border:1px solid transparent
}
.chip.flag{background:var(--flag-soft);color:var(--flag)}
.chip.dead{background:transparent;color:var(--fog);border-color:var(--line)}
/* Removed posts stay on the page — the link is permanent — but read as
   spent rather than competing with the live ones. */
.card.dead{opacity:.55}
.card.dead .price{color:var(--fog)}

.src{
  margin:.8rem 0 0;padding-top:.7rem;border-top:1px solid var(--line);
  display:flex;justify-content:space-between;align-items:center;
  color:var(--fog);font-size:.78rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace
}
.go{color:var(--accent);font-weight:600}

footer{margin:1.5rem 0 0;padding-top:1.1rem;border-top:1px solid var(--line);
  color:var(--fog);font-size:.82rem}
footer p{margin:.35rem 0}
.empty{
  background:var(--card);border:1px dashed var(--line);border-radius:14px;
  padding:2.25rem 1.25rem;text-align:center;color:var(--fog)
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _is_removed(row) -> bool:
    return str(row["reject_reason"] or "").startswith("removed")


def _distance(row) -> str:
    """'· 8.1 mi from work', or nothing when we couldn't place the listing.

    Worth its space because it is also the rule: past three miles a place has
    to come with parking, so the number explains why the digest looks the way
    it does.
    """
    try:
        miles = float(row["distance_mi"])
    except (TypeError, ValueError, IndexError):
        return ""
    return f' <span class="dist">· {miles:.1f} mi from work</span>'


def _card(row) -> str:
    price = _money(row["effective_price"] or row["price"])
    beds = _beds(row["bedrooms"])
    where = html.escape(row["neighborhood"] or "—")
    url = html.escape(row["url"], quote=True)
    title = html.escape((row["title"] or "").strip())[:120]

    # The placeholder is a CSS ::before on the wrapper, so a blocked or broken
    # image just uncovers it — no inline markup stuffed into an onerror
    # attribute, which is both fragile to parse and easy to get wrong.
    if row["image_url"]:
        # Served through /img/ rather than hot-linked: see the endpoint for why.
        src = f"/img/{html.escape(row['source'], quote=True)}/{html.escape(row['external_id'], quote=True)}"
        shot = (
            f'<div class="figure" data-label="No photo">'
            f'<img class="shot" src="{src}" alt="" loading="lazy" decoding="async" '
            f'onerror="this.remove()"></div>'
        )
    else:
        # No photo at all: render no figure rather than a large empty block.
        # Reserving 16:10 for something that will never arrive costs most of a
        # phone screen per card. (A photo that FAILS still reserves its space —
        # that avoids layout shift after the page has painted.)
        shot = ""

    chips = "".join(
        f'<span class="chip{" flag" if kind == "flag" else ""}">{html.escape(label)}</span>'
        for label, kind in _chips(row)
    )
    # A run page is a permanent link, so posts die under it. Say so plainly
    # rather than sending someone to a "flagged for removal" page.
    gone = _is_removed(row)
    if gone:
        chips = '<span class="chip dead">No longer listed</span>' + chips

    # Show the base rent only when parking changed the number, so the figure
    # here can be reconciled with the one on the listing page.
    base = ""
    if row["parking_fee"] and row["price"]:
        base = f'<p class="base">{_money(row["price"])} rent + {_money(row["parking_fee"])} parking</p>'

    return f"""<a class="card{' dead' if gone else ''}" href="{url}" target="_blank" rel="noopener noreferrer">
  {shot}
  <div class="body">
    <div class="priceline"><span class="price">{price}</span><span class="spec">/mo · {beds}</span></div>
    <p class="where">{where}{_distance(row)}</p>
    {base}
    <div class="chips">{chips}</div>
    <div class="src"><span>{html.escape(_host(row["url"]))}</span><span class="go">Open →</span></div>
  </div>
</a>"""


def _page(title: str, heading: str, sub: str, eyebrow: str, inner: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex,nofollow">
<title>{html.escape(title)}</title>
<!--
  Icons and the preview card (src/brand/, drawn by the selfhosted repo's
  scripts/gen-brand.py). noindex above and a preview card are not in tension:
  noindex keeps this out of search, while the card is what renders when the
  link is pasted into the text message it exists to be pasted into. iMessage
  and WhatsApp read these tags and ignore robots entirely.

  Absolute og:image URL because that is the only kind a scraper will resolve —
  a relative one is silently dropped and the preview falls back to a grey box.
-->
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0B6E4F">
<meta property="og:type" content="website">
<meta property="og:site_name" content="homes.diemer.codes">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="San Francisco rentals, filtered to what you asked for.">
<meta property="og:image" content="{OG_BASE}/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="homes.diemer.codes — apartments found in the latest sweep">
<meta name="twitter:card" content="summary_large_image">
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <header>
    <p class="eyebrow">{html.escape(eyebrow)}</p>
    <h1>{html.escape(heading)}</h1>
    <p class="sub">{html.escape(sub)}</p>
  </header>
  {inner}
</div>
</body>
</html>"""


@app.get("/img/{source}/{external_id:path}")
def image(source: str, external_id: str):
    """Re-host a listing photo.

    Two reasons this isn't a plain hot-link. Apartments.com returns 403 to
    foreign referrers, so its photos simply don't render; and going through
    here means one consistent path for every source rather than four different
    hot-link policies.

    The URL is looked up in the database by (source, external_id) — it is never
    taken from the query string. An open `?url=` proxy would let anyone use
    this pod to fetch arbitrary internal addresses.
    """
    key = f"{source}/{external_id}"
    hit = _IMG_CACHE.get(key)
    if hit:
        _IMG_CACHE.move_to_end(key)
        return Response(content=hit[0], media_type=hit[1],
                        headers={"Cache-Control": "public, max-age=86400"})
    try:
        with _connect() as db:
            row = db.execute(
                "SELECT image_url, url FROM listings WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row or not row["image_url"]:
        return Response(status_code=404)

    req = urllib.request.Request(
        row["image_url"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
            ),
            # Present as if the listing page itself asked for the photo, which
            # is what the hot-link checks are looking for.
            "Referer": row["url"],
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read(6_000_000)
            ctype = resp.headers.get("Content-Type", "image/jpeg")
    except Exception as exc:
        logger.info("image fetch failed for %s: %s", key, exc)
        return Response(status_code=404)

    if not ctype.startswith("image/"):
        return Response(status_code=404)
    _IMG_CACHE[key] = (body, ctype)
    while len(_IMG_CACHE) > _IMG_CACHE_MAX:
        _IMG_CACHE.popitem(last=False)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------- brand
#
# Icons, manifest and the preview card, from src/brand/ (drawn by the
# selfhosted repo's scripts/gen-brand.py and baked into the image).
#
# An explicit allowlist rather than StaticFiles over the directory: this app
# serves one other thing from disk and it is a photo proxy that goes to some
# lengths not to be an open one. A mount that hands out whatever is in a
# directory is the same shape of mistake, and the set of files here is seven
# and fixed. Read once at import, because they never change under a running
# pod — the image is the artifact.

_BRAND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand")
_BRAND_TYPES = {
    "icon.svg": "image/svg+xml",
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "og.png": "image/png",
    "manifest.webmanifest": "application/manifest+json",
}


def _load_brand() -> dict[str, tuple[bytes, str]]:
    loaded: dict[str, tuple[bytes, str]] = {}
    for name, media_type in _BRAND_TYPES.items():
        path = os.path.join(_BRAND_DIR, name)
        try:
            with open(path, "rb") as handle:
                loaded[name] = (handle.read(), media_type)
        except OSError as exc:
            # Warn rather than raise: a missing icon should not stop the run
            # page from serving, which is the thing someone is holding a link
            # to. It does need to be loud in the log, because the symptom
            # otherwise is a preview that silently stops unfurling.
            logger.warning("brand asset missing, not serving it: %s (%s)", path, exc)
    return loaded


def _register_brand_routes() -> None:
    """One explicit route per asset.

    Deliberately NOT a single `/{asset:path}` handler. FastAPI resolves in
    registration order and that pattern matches everything, so it would shadow
    /healthz and every /r/<token> declared after it — the run page, i.e. the
    only URL anyone is ever sent, would start 404ing. Seven fixed routes cannot
    do that to anything.
    """
    for name, (body, media_type) in _BRAND.items():
        def handler(body: bytes = body, media_type: str = media_type) -> Response:
            # A day, not a year: these names carry no content hash, so the
            # cache lifetime is also how long a redesigned mark keeps showing
            # the old one.
            return Response(content=body, media_type=media_type,
                            headers={"Cache-Control": "public, max-age=86400"})

        app.add_api_route(f"/{name}", handler, methods=["GET"], include_in_schema=False)


_BRAND = _load_brand()
_register_brand_routes()


@app.get("/healthz")
def healthz() -> dict:
    try:
        with _connect() as db:
            db.execute("SELECT 1 FROM listings LIMIT 1").fetchone()
        return {"status": "ok"}
    except sqlite3.OperationalError as exc:
        # No database yet is fine before the first run; say so rather than 500.
        return {"status": "starting", "detail": str(exc)}


@app.get("/", response_class=HTMLResponse)
def index():
    """Newest run, so a bookmark stays useful."""
    try:
        with _connect() as db:
            row = db.execute(
                "SELECT token FROM runs WHERE token IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        return RedirectResponse(f"/r/{row['token']}", status_code=307)
    return HTMLResponse(
        _page("homes", "Nothing yet", "The first matching listings will show up here.",
              "apartment watch", '<div class="empty">No runs have found a match yet.</div>'),
        status_code=200,
    )


@app.get("/r/{token}", response_class=HTMLResponse)
def run_page(token: str):
    try:
        with _connect() as db:
            rows = db.execute(
                "SELECT * FROM listings WHERE notified_run = ? "
                "ORDER BY effective_price ASC, first_seen ASC",
                (token,),
            ).fetchall()
            meta = db.execute(
                "SELECT * FROM runs WHERE token = ?", (token,)
            ).fetchone()
    except sqlite3.OperationalError:
        rows, meta = [], None

    if not rows:
        return HTMLResponse(
            _page("homes", "Nothing here",
                  "That link has no listings on it. It may be from a very old run.",
                  "apartment watch",
                  '<div class="empty">No listings for this link.</div>'),
            status_code=404,
        )

    when = _local(meta["started_at"]) if meta else ""
    n = len(rows)
    cheapest = _money(rows[0]["effective_price"] or rows[0]["price"])
    dearest = _money(rows[-1]["effective_price"] or rows[-1]["price"])
    span = cheapest if n == 1 else f"{cheapest}–{dearest}"

    cards = "".join(_card(r) for r in rows)
    footer = (
        '<footer>'
        f'<p>{span} per month. Rent shown includes any stated parking fee.</p>'
        '<p>Rent-control and affordable-housing labels are best guesses from public '
        'records — check with the landlord before relying on them.</p>'
        '</footer>'
    )
    return HTMLResponse(
        _page(
            f"{n} place{'s' if n != 1 else ''} · homes",
            f"{n} new place{'s' if n != 1 else ''}",
            when and f"Found {when}." or "",
            "apartment watch",
            f'<div class="list">{cards}</div>{footer}',
        )
    )
