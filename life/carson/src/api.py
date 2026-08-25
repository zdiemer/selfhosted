"""carson's HTTP surface: a JSON API, a small web UI, and the calendar feed.

THREE AUDIENCES, THREE AUTH STORIES, and they are not interchangeable:

  /api/v1/*   Claude runs, in-cluster over ClusterIP. No auth here on purpose:
              reaching this address at all means you are already inside the
              cluster, and the ingress (which is the only way in from outside)
              gates everything behind Authelia. Same reasoning as the Ollama
              service in ai/.

  /           The dashboard, for hand-entering contacts and dates. Gated by
              Authelia forward-auth at the ingress, so there is no login code
              in this file and never should be.

  /feed/*     The calendar feed. CANNOT be behind Authelia — Apple Calendar and
              Google Calendar cannot complete an interactive login, they just
              silently stop refreshing. It is protected by an unguessable token
              in the path and served by a SECOND Ingress with no forward-auth
              middleware (Traefik applies middleware per-router, so this has to
              be a separate Ingress object, not a second path on the first).

The UI is server-rendered HTML with plain forms. No SPA, no build step, no
frontend dependency to keep patched — the whole thing is one operator entering
about thirty people once and editing them rarely.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets as pysecrets
from datetime import date, datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import db
import ics
import reminders

logger = logging.getLogger("carson.api")

FEED_TOKEN = os.environ.get("CARSON_FEED_TOKEN", "")
CAL_NAME = os.environ.get("CARSON_CAL_NAME", "Mr. Carson")

app = FastAPI(title="carson", docs_url=None, redoc_url=None)


def conn():
    return db.connect()


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    with conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


# --------------------------------------------------------------------------
# calendar feed — unauthenticated by necessity, token-gated
# --------------------------------------------------------------------------
@app.get("/feed/{token}.ics")
def feed(token: str):
    if not FEED_TOKEN or not pysecrets.compare_digest(token, FEED_TOKEN):
        # 404 rather than 403: a wrong token should not confirm the endpoint.
        raise HTTPException(status_code=404, detail="not found")

    cal = ics.Calendar(CAL_NAME)
    today = date.today()
    with conn() as c:
        for row in c.execute(
            "SELECT d.id, d.kind, d.label, d.month, d.day, d.year, p.name "
            "FROM important_date d JOIN person p ON p.id = d.person_id"
        ):
            when = reminders.next_occurrence(row["month"], row["day"], today)
            occasion = row["label"] or row["kind"]
            summary = f"{row['name']} — {occasion}"
            desc = ""
            if row["year"]:
                desc = f"{reminders._ordinal(when.year - row['year'])} {occasion}"
            cal.add_all_day(
                f"date-{row['id']}@carson", summary, when,
                description=desc, yearly=True,
            )

        for row in c.execute(
            "SELECT id, uid, summary, description, starts_at, ends_at, all_day FROM event"
        ):
            if row["all_day"]:
                cal.add_all_day(
                    row["uid"], row["summary"],
                    date.fromisoformat(row["starts_at"][:10]),
                    description=row["description"] or "",
                )
            else:
                start = datetime.fromisoformat(row["starts_at"])
                end = datetime.fromisoformat(row["ends_at"]) if row["ends_at"] else None
                cal.add_timed(
                    row["uid"], row["summary"], start, end,
                    description=row["description"] or "",
                )

        # Todos with a due date ride the feed too — a follow-up you can see in
        # the same place as everything else is a follow-up you might do.
        for row in c.execute(
            "SELECT id, text, due FROM todo WHERE status='open' AND due IS NOT NULL"
        ):
            cal.add_all_day(
                f"todo-{row['id']}@carson", f"To do: {row['text']}",
                date.fromisoformat(row["due"][:10]),
            )

    return Response(
        content=cal.render(),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{CAL_NAME}.ics"'},
    )


# --------------------------------------------------------------------------
# JSON API — for Claude runs over ClusterIP
# --------------------------------------------------------------------------
@app.get("/api/v1/people")
def api_people():
    with conn() as c:
        out = []
        for p in c.execute("SELECT * FROM person ORDER BY name"):
            row = dict(p)
            row["handles"] = [
                dict(h) for h in c.execute(
                    "SELECT kind, value FROM handle WHERE person_id=?", (p["id"],))
            ]
            row["dates"] = [
                dict(d) for d in c.execute(
                    "SELECT id, kind, label, month, day, year FROM important_date "
                    "WHERE person_id=?", (p["id"],))
            ]
            out.append(row)
        return {"people": out}


@app.post("/api/v1/people")
def api_add_person(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    with conn() as c:
        cur = c.execute(
            "INSERT INTO person (name, cadence_tier, enrich_optin, notes, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (name, payload.get("cadence_tier", "none"),
             1 if payload.get("enrich_optin") else 0,
             payload.get("notes", ""), db.now(), db.now()),
        )
        pid = cur.lastrowid
        for h in payload.get("handles", []):
            c.execute(
                "INSERT OR IGNORE INTO handle (person_id, kind, value, created_at) VALUES (?,?,?,?)",
                (pid, h["kind"], h["value"], db.now()),
            )
        c.commit()
        return {"id": pid}


@app.post("/api/v1/people/{pid}/dates")
def api_add_date(pid: int, payload: dict = Body(...)):
    with conn() as c:
        if not c.execute("SELECT 1 FROM person WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "no such person")
        cur = c.execute(
            "INSERT INTO important_date (person_id, kind, label, month, day, year, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, payload.get("kind", "birthday"), payload.get("label", ""),
             int(payload["month"]), int(payload["day"]),
             int(payload["year"]) if payload.get("year") else None, db.now()),
        )
        c.commit()
        return {"id": cur.lastrowid}


@app.post("/api/v1/people/{pid}/notes")
def api_add_note(pid: int, payload: dict = Body(...)):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO note (person_id, ts, text, source_ref) VALUES (?,?,?,?)",
            (pid, db.now(), payload.get("text", ""), payload.get("source_ref", "")),
        )
        c.commit()
        return {"id": cur.lastrowid}


@app.get("/api/v1/todos")
def api_todos(status: str = "open"):
    with conn() as c:
        rows = c.execute(
            "SELECT t.*, p.name AS person_name FROM todo t "
            "LEFT JOIN person p ON p.id = t.person_id "
            "WHERE t.status=? ORDER BY (t.due IS NULL), t.due, t.id",
            (status,),
        ).fetchall()
        return {"todos": [dict(r) for r in rows]}


@app.post("/api/v1/todos")
def api_add_todo(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    with conn() as c:
        cur = c.execute(
            "INSERT INTO todo (text, status, due, person_id, source_snippet, source_ref, origin, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (text, "open", payload.get("due"), payload.get("person_id"),
             payload.get("source_snippet", ""), payload.get("source_ref", ""),
             payload.get("origin", "manual"), db.now()),
        )
        c.commit()
        return {"id": cur.lastrowid}


@app.patch("/api/v1/todos/{tid}")
def api_update_todo(tid: int, payload: dict = Body(...)):
    fields, values = [], []
    for key in ("text", "status", "due", "person_id", "nag_state"):
        if key in payload:
            fields.append(f"{key}=?")
            values.append(payload[key])
    if not fields:
        raise HTTPException(400, "nothing to update")
    if payload.get("status") == "done":
        fields.append("completed_at=?")
        values.append(db.now())
    values.append(tid)
    with conn() as c:
        c.execute(f"UPDATE todo SET {', '.join(fields)} WHERE id=?", values)
        c.commit()
    return {"ok": True}


@app.post("/api/v1/reminders/run")
def api_run_reminders(payload: dict = Body(default={})):
    """Walk the date ladder and send what is due.

    The CronJob calls THIS rather than opening the database itself. That is not
    a stylistic choice: the PVC is ReadWriteOnce, so a second pod mounting it
    only works when the scheduler happens to place it on the same node as the
    web pod — and when it does not, the job sits in ContainerCreating forever
    and the reminder is silently never sent. Observed exactly that on the first
    deploy (job on node-4, volume attached to node-5).

    Driving it over HTTP also means there is only ever one SQLite writer, and
    the Job still either succeeded or failed in a way `kubectl` can report.
    """
    import notify
    import reminders as R

    today = date.today()
    if payload.get("today"):
        today = date.fromisoformat(payload["today"])
        logger.warning("reminder sweep with overridden date: %s", today)

    sms = notify.SmsRelay()
    with conn() as c:
        sent = R.run(c, sms, today)
    logger.info("reminder sweep complete: %d sent", sent)
    return {"date": today.isoformat(), "sent": sent}


@app.get("/api/v1/digest")
def api_digest():
    """Everything the morning digest run needs, in one call.

    Deliberately a single endpoint rather than five: the Claude run composes one
    text from a whole picture, and five round-trips is five chances to get a
    partial one.
    """
    today = date.today()
    with conn() as c:
        overdue = [dict(r) for r in c.execute(
            "SELECT t.id, t.text, t.due, p.name AS person_name FROM todo t "
            "LEFT JOIN person p ON p.id=t.person_id "
            "WHERE t.status='open' AND t.due IS NOT NULL AND t.due <= ? ORDER BY t.due",
            (today.isoformat(),))]
        upcoming = []
        for row in c.execute(
            "SELECT d.id, d.kind, d.label, d.month, d.day, d.year, p.name "
            "FROM important_date d JOIN person p ON p.id=d.person_id"
        ):
            when = reminders.next_occurrence(row["month"], row["day"], today)
            days = (when - today).days
            if days <= 30:
                upcoming.append({
                    "name": row["name"], "kind": row["kind"], "label": row["label"],
                    "when": when.isoformat(), "days_until": days,
                })
        upcoming.sort(key=lambda r: r["days_until"])
        proposals = [dict(r) for r in c.execute(
            "SELECT id, kind, payload, short_code FROM proposal WHERE status='pending'")]
        events = [dict(r) for r in c.execute(
            "SELECT summary, starts_at, all_day FROM event "
            "WHERE date(starts_at) BETWEEN date(?) AND date(?, '+1 day') ORDER BY starts_at",
            (today.isoformat(), today.isoformat()))]
        return {
            "date": today.isoformat(),
            "overdue_todos": overdue,
            "upcoming_dates": upcoming,
            "pending_proposals": proposals,
            "events": events,
        }


# --------------------------------------------------------------------------
# web UI — behind Authelia at the ingress
# --------------------------------------------------------------------------
CSS = """
:root{--bg:#faf9f7;--fg:#1c1a17;--mut:#6b645c;--line:#e2ddd5;--acc:#7a5c3e}
@media(prefers-color-scheme:dark){:root{--bg:#17151300;--bg:#171513;--fg:#ece7e0;--mut:#9c948a;--line:#2e2a26;--acc:#c9a227}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
font:16px/1.55 ui-serif,Georgia,serif;max-width:52rem;margin-inline:auto}
h1{font-size:1.5rem;letter-spacing:.02em;margin:0 0 .2rem}
h2{font-size:1rem;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);
margin:2rem 0 .6rem;font-weight:600}
a{color:var(--acc)} .sub{color:var(--mut);margin:0 0 1.5rem;font-style:italic}
table{border-collapse:collapse;width:100%} td,th{text-align:left;padding:.4rem .5rem;
border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.soon{color:var(--acc);font-weight:600}
form{margin:.8rem 0;display:flex;flex-wrap:wrap;gap:.4rem}
input,select{padding:.4rem .5rem;border:1px solid var(--line);border-radius:4px;
background:var(--bg);color:var(--fg);font:inherit;font-size:.9rem}
button{padding:.4rem .9rem;border:1px solid var(--acc);background:var(--acc);
color:var(--bg);border-radius:4px;font:inherit;font-size:.9rem;cursor:pointer}
.empty{color:var(--mut);font-style:italic}
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


@app.get("/", response_class=HTMLResponse)
def ui_home():
    today = date.today()
    with conn() as c:
        upcoming = []
        for row in c.execute(
            "SELECT d.id, d.kind, d.label, d.month, d.day, d.year, p.id AS pid, p.name "
            "FROM important_date d JOIN person p ON p.id=d.person_id"
        ):
            when = reminders.next_occurrence(row["month"], row["day"], today)
            upcoming.append((when, (when - today).days, row))
        upcoming.sort(key=lambda r: r[1])
        todos = c.execute(
            "SELECT t.id, t.text, t.due, p.name AS person_name FROM todo t "
            "LEFT JOIN person p ON p.id=t.person_id WHERE t.status='open' "
            "ORDER BY (t.due IS NULL), t.due, t.id"
        ).fetchall()
        n_people = c.execute("SELECT count(*) FROM person").fetchone()[0]

    rows = "".join(
        f"<tr><td>{esc(r['name'])}</td><td>{esc(r['label'] or r['kind'])}</td>"
        f"<td>{when.strftime('%a %-d %b')}</td>"
        f"<td class='{'soon' if days <= 21 else ''}'>"
        f"{'today' if days == 0 else f'in {days}d'}</td></tr>"
        for when, days, r in upcoming[:12]
    ) or "<tr><td colspan=4 class=empty>No dates yet.</td></tr>"

    todo_rows = "".join(
        f"<tr><td>{esc(t['text'])}</td><td>{esc(t['person_name'])}</td>"
        f"<td>{esc(t['due'] or '')}</td>"
        f"<td><form method=post action='/ui/todos/{t['id']}/done' style='margin:0'>"
        f"<button>done</button></form></td></tr>"
        for t in todos
    ) or "<tr><td colspan=4 class=empty>Nothing outstanding, sir.</td></tr>"

    return page("Mr. Carson", f"""
<h1>Mr. Carson</h1>
<p class=sub>“The business of life is the acquisition of memories.”</p>
<h2>Upcoming dates</h2>
<table><tr><th>Who</th><th>Occasion</th><th>When</th><th></th></tr>{rows}</table>
<h2>Outstanding</h2>
<table><tr><th>Task</th><th>Who</th><th>Due</th><th></th></tr>{todo_rows}</table>
<form method=post action=/ui/todos>
  <input name=text placeholder="Add a task…" size=34 required>
  <input name=due type=date>
  <button>Add</button>
</form>
<h2>Household</h2>
<p><a href=/ui/people>{n_people} {'person' if n_people == 1 else 'people'} on file →</a></p>
""")


@app.get("/ui/people", response_class=HTMLResponse)
def ui_people():
    with conn() as c:
        people = c.execute("SELECT * FROM person ORDER BY name").fetchall()
        counts = {
            r["person_id"]: r["n"] for r in c.execute(
                "SELECT person_id, count(*) AS n FROM important_date GROUP BY person_id")
        }
    rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td>{esc(p['cadence_tier'])}</td>"
        f"<td>{counts.get(p['id'], 0)}</td>"
        f"<td><a href='/ui/people/{p['id']}'>open</a></td></tr>"
        for p in people
    ) or "<tr><td colspan=4 class=empty>Nobody yet.</td></tr>"
    return page("Household — Mr. Carson", f"""
<h1>Household</h1>
<p class=sub><a href=/>← back</a></p>
<table><tr><th>Name</th><th>Cadence</th><th>Dates</th><th></th></tr>{rows}</table>
<h2>Add someone</h2>
<form method=post action=/ui/people>
  <input name=name placeholder="Name" required>
  <select name=cadence_tier>
    <option value=none>no cadence</option><option value=close>close (~2w)</option>
    <option value=family>family (~1w)</option><option value=keep_in_touch>keep in touch (~2mo)</option>
  </select>
  <button>Add</button>
</form>
""")


@app.get("/ui/people/{pid}", response_class=HTMLResponse)
def ui_person(pid: int):
    today = date.today()
    with conn() as c:
        p = c.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "no such person")
        dates = c.execute("SELECT * FROM important_date WHERE person_id=?", (pid,)).fetchall()
        handles = c.execute("SELECT * FROM handle WHERE person_id=?", (pid,)).fetchall()
        notes = c.execute(
            "SELECT * FROM note WHERE person_id=? ORDER BY ts DESC LIMIT 20", (pid,)).fetchall()

    drows = "".join(
        f"<tr><td>{esc(d['label'] or d['kind'])}</td>"
        f"<td>{reminders.next_occurrence(d['month'], d['day'], today).strftime('%-d %b')}</td>"
        f"<td>{esc(d['year'] or '—')}</td></tr>" for d in dates
    ) or "<tr><td colspan=3 class=empty>No dates.</td></tr>"
    hrows = "".join(
        f"<tr><td>{esc(h['kind'])}</td><td>{esc(h['value'])}</td></tr>" for h in handles
    ) or "<tr><td colspan=2 class=empty>No handles.</td></tr>"
    nrows = "".join(
        f"<tr><td>{esc(n['ts'][:10])}</td><td>{esc(n['text'])}</td></tr>" for n in notes
    ) or "<tr><td colspan=2 class=empty>No notes.</td></tr>"

    return page(f"{p['name']} — Mr. Carson", f"""
<h1>{esc(p['name'])}</h1>
<p class=sub>{esc(p['cadence_tier'])} · <a href=/ui/people>← household</a></p>
<h2>Dates</h2>
<table><tr><th>Occasion</th><th>Day</th><th>Year</th></tr>{drows}</table>
<form method=post action=/ui/people/{pid}/dates>
  <select name=kind><option value=birthday>birthday</option>
    <option value=anniversary>anniversary</option><option value=custom>custom</option></select>
  <input name=label placeholder="label (optional)" size=14>
  <input name=month type=number min=1 max=12 placeholder=MM required style=width:5rem>
  <input name=day type=number min=1 max=31 placeholder=DD required style=width:5rem>
  <input name=year type=number min=1900 max=2100 placeholder=YYYY style=width:6rem>
  <button>Add date</button>
</form>
<h2>Handles</h2>
<table><tr><th>Kind</th><th>Value</th></tr>{hrows}</table>
<form method=post action=/ui/people/{pid}/handles>
  <select name=kind><option value=email>email</option><option value=phone>phone</option>
    <option value=imessage>imessage</option></select>
  <input name=value placeholder="address / number" size=24 required>
  <button>Add handle</button>
</form>
<h2>Notes</h2>
<table><tr><th>When</th><th>Note</th></tr>{nrows}</table>
<form method=post action=/ui/people/{pid}/notes>
  <input name=text placeholder="Something worth remembering…" size=40 required>
  <button>Add note</button>
</form>
""")


# --- UI form posts: plain 303-redirect-after-post, no JS anywhere ----------
@app.post("/ui/people")
async def ui_add_person(request: Request):
    form = await request.form()
    with conn() as c:
        c.execute(
            "INSERT INTO person (name, cadence_tier, enrich_optin, notes, created_at, updated_at) "
            "VALUES (?,?,0,'',?,?)",
            (form["name"].strip(), form.get("cadence_tier", "none"), db.now(), db.now()),
        )
        c.commit()
    return RedirectResponse("/ui/people", status_code=303)


@app.post("/ui/people/{pid}/dates")
async def ui_add_date(pid: int, request: Request):
    form = await request.form()
    with conn() as c:
        c.execute(
            "INSERT INTO important_date (person_id, kind, label, month, day, year, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, form.get("kind", "birthday"), form.get("label", "").strip(),
             int(form["month"]), int(form["day"]),
             int(form["year"]) if form.get("year") else None, db.now()),
        )
        c.commit()
    return RedirectResponse(f"/ui/people/{pid}", status_code=303)


@app.post("/ui/people/{pid}/handles")
async def ui_add_handle(pid: int, request: Request):
    form = await request.form()
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO handle (person_id, kind, value, created_at) VALUES (?,?,?,?)",
            (pid, form["kind"], form["value"].strip(), db.now()),
        )
        c.commit()
    return RedirectResponse(f"/ui/people/{pid}", status_code=303)


@app.post("/ui/people/{pid}/notes")
async def ui_add_note(pid: int, request: Request):
    form = await request.form()
    with conn() as c:
        c.execute("INSERT INTO note (person_id, ts, text, source_ref) VALUES (?,?,?,'')",
                  (pid, db.now(), form["text"].strip()))
        c.commit()
    return RedirectResponse(f"/ui/people/{pid}", status_code=303)


@app.post("/ui/todos")
async def ui_add_todo(request: Request):
    form = await request.form()
    with conn() as c:
        c.execute(
            "INSERT INTO todo (text, status, due, origin, created_at) VALUES (?,'open',?,'manual',?)",
            (form["text"].strip(), form.get("due") or None, db.now()),
        )
        c.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/ui/todos/{tid}/done")
def ui_done_todo(tid: int):
    with conn() as c:
        c.execute("UPDATE todo SET status='done', completed_at=? WHERE id=?", (db.now(), tid))
        c.commit()
    return RedirectResponse("/", status_code=303)
