"use strict";

const $ = (id) => document.getElementById(id);
const grid = $("grid");
let state = { configs: [], limits: {}, running: 0 };
let timer = null;

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

function badge(text, cls) {
  const el = document.createElement("span");
  el.className = `badge ${cls || ""}`.trim();
  el.textContent = text;
  return el;
}

function card(cfg) {
  const el = document.createElement("article");
  el.className = "card" + (cfg.session ? " running" : "");

  const h = document.createElement("h3");
  h.textContent = cfg.name || cfg.slug;
  el.append(h);

  const badges = document.createElement("div");
  badges.className = "badges";
  badges.append(badge(cfg.network, `net-${cfg.network}`));
  badges.append(badge(`${cfg.cores} vCPU`));
  badges.append(badge(`${Math.round(cfg.memoryMib / 1024 * 10) / 10} GiB`));
  badges.append(badge(`${cfg.diskGib} GiB disk`));
  if (cfg.persist) badges.append(badge("persists"));
  if (cfg.arch === "arm") badges.append(badge("ARM · emulated"));
  if (cfg.source === "seed") badges.append(badge("from values.yaml"));
  if (cfg.session) {
    badges.append(badge(cfg.session.ready ? "running" : cfg.session.phase, "live"));
  }
  el.append(badges);

  // A running guest shows what it looks like right now, refreshed server-side;
  // a stopped one shows its note.
  if (cfg.session && cfg.hasLiveTile) {
    const live = document.createElement("img");
    live.className = "live";
    live.alt = `${cfg.name} live screen`;
    live.src = `/api/live/${cfg.slug}/screenshot.png?t=${Date.now()}`;
    live.title = "click to open the console";
    live.onclick = () => openConsole(cfg.session, cfg.name || cfg.slug, cfg.savepointsEnabled);
    el.append(live);
  } else {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = cfg.note || "";
    el.append(note);
  }

  const actions = document.createElement("div");
  actions.className = "actions";

  if (cfg.session) {
    const open = document.createElement("button");
    open.className = "btn";
    open.textContent = "Open console";
    open.disabled = !cfg.session.podIP;
    open.onclick = () => openConsole(cfg.session, cfg.name || cfg.slug, cfg.savepointsEnabled);
    actions.append(open);

    const stop = document.createElement("button");
    stop.className = "btn danger";
    stop.textContent = "Stop";
    stop.onclick = () => act(stop, () => api(`/api/sessions/${cfg.session.id}`, { method: "DELETE" }));
    actions.append(stop);
  } else if (!cfg.hasIsoUrl) {
    actions.append(badge("no ISO URL set — add one in values.yaml"));
  } else if (cfg.iso_status === "cached") {
    const full = state.running >= state.limits.maxConcurrent;
    const launch = document.createElement("button");
    launch.className = "btn";
    launch.textContent = "Launch";
    launch.disabled = full;
    launch.onclick = () =>
      act(launch, () => api("/api/sessions", { method: "POST", body: JSON.stringify({ slug: cfg.slug }) }));
    actions.append(launch);

    if (cfg.persist) {
      // A persisted config attaches the installer only while its disk is new,
      // so an interrupted install would otherwise be unreachable forever —
      // the disk exists, so the ISO is never offered again. This is the way
      // back to Setup.
      const reinstall = document.createElement("button");
      reinstall.className = "btn ghost";
      reinstall.textContent = "Boot installer";
      reinstall.title = "Attach the ISO and boot it, e.g. to (re)run Setup";
      reinstall.disabled = full;
      reinstall.onclick = () => act(reinstall, () => api("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ slug: cfg.slug, bootFromIso: true }),
      }));
      actions.append(reinstall);
    }
  } else if (cfg.iso_status === "fetching") {
    actions.append(badge("downloading ISO…"));
  } else {
    const fetchBtn = document.createElement("button");
    fetchBtn.className = "btn ghost";
    fetchBtn.textContent = cfg.iso_status === "failed" ? "Retry ISO download" : "Download ISO";
    fetchBtn.onclick = () => act(fetchBtn, () => api(`/api/isos/${cfg.slug}/fetch`, { method: "POST" }));
    actions.append(fetchBtn);
  }

  if (cfg.source === "user" && !cfg.session) {
    const del = document.createElement("button");
    del.className = "btn ghost";
    del.textContent = "Delete";
    del.onclick = () => act(del, () => api(`/api/catalog/${cfg.slug}`, { method: "DELETE" }));
    actions.append(del);
  }

  el.append(actions);

  if (cfg.savepoints && cfg.savepoints.length) {
    el.append(savepointStrip(cfg));
  } else if (cfg.savepointsEnabled && cfg.session) {
    const hint = document.createElement("p");
    hint.className = "note dim";
    hint.textContent = "No save points yet — open the console and save once you are set up.";
    el.append(hint);
  }
  return el;
}

function savepointStrip(cfg) {
  const wrap = document.createElement("div");
  wrap.className = "saves";

  const head = document.createElement("div");
  head.className = "saves-head";
  head.textContent = `Save points (${cfg.savepoints.length})`;
  wrap.append(head);

  const strip = document.createElement("div");
  strip.className = "shots";
  cfg.savepoints.forEach((sp) => {
    const fig = document.createElement("figure");
    fig.className = "shot";

    if (sp.hasScreenshot) {
      const img = document.createElement("img");
      // Cache-busted on `created` so a re-save under the same name repaints,
      // while ordinary polling still hits the browser cache.
      img.src = `/api/savepoints/${cfg.slug}/${sp.name}/screenshot.png?v=${encodeURIComponent(sp.created || "")}`;
      img.alt = `${sp.name} screenshot`;
      img.loading = "lazy";
      img.title = "click for full size";
      img.onclick = () => window.open(
        `/api/savepoints/${cfg.slug}/${sp.name}/screenshot.png?full=1`, "_blank");
      fig.append(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "shot-none";
      ph.textContent = "no image";
      fig.append(ph);
    }

    const cap = document.createElement("figcaption");
    cap.textContent = sp.name;
    if (!sp.observed) cap.title = "remembered — the VM is not running";
    fig.append(cap);

    const row = document.createElement("div");
    row.className = "shot-actions";

    const load = document.createElement("button");
    load.className = "btn tiny";
    load.textContent = "Load";
    load.disabled = !!cfg.session;
    load.title = cfg.session ? "stop the VM first" : "resume from this save point";
    load.onclick = () => act(load, () => api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ slug: cfg.slug, loadSnapshot: sp.name }),
    }));
    row.append(load);

    const del = document.createElement("button");
    del.className = "btn tiny ghost";
    del.textContent = "Delete";
    del.onclick = () => act(del, () =>
      api(`/api/savepoints/${cfg.slug}/${sp.name}`, { method: "DELETE" }));
    row.append(del);

    fig.append(row);
    strip.append(fig);
  });
  wrap.append(strip);
  return wrap;
}

async function act(button, fn) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "…";
  try {
    await fn();
    await refresh();
  } catch (err) {
    button.textContent = original;
    button.disabled = false;
    $("footer").textContent = err.message;
  }
}

async function refresh() {
  try {
    state = await api("/api/catalog");
  } catch (err) {
    grid.innerHTML = "";
    const p = document.createElement("p");
    p.className = "err";
    p.textContent = err.message;
    grid.append(p);
    return;
  }
  grid.removeAttribute("aria-busy");
  grid.innerHTML = "";
  state.configs.forEach((cfg) => grid.append(card(cfg)));
  $("slots").textContent = `${state.running} / ${state.limits.maxConcurrent} running`;
  $("footer").textContent =
    `Sessions are culled after ${Math.round(state.limits.idleTimeoutSeconds / 60)} minutes with the console closed.`;
}

/* ---- console ---- */

let current = null;

function openConsole(session, title, savepointsEnabled) {
  current = session;
  $("console-save").hidden = !savepointsEnabled;
  $("console-title").textContent = title;
  // The trailing slash matters: noVNC resolves its assets and its websocket
  // against location.href, so /vm/<id> would resolve them against /vm/.
  $("console-frame").src = `/vm/${session.id}/`;
  $("console-logbox").hidden = true;
  $("console").hidden = false;
}

function closeConsole() {
  $("console").hidden = true;
  $("console-frame").src = "about:blank";
  current = null;
  refresh();
}

$("console-save").onclick = async () => {
  if (!current) return;
  const name = prompt("Name this save point (letters, digits, - and _):", "after_install");
  if (!name) return;
  const btn = $("console-save");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    await api(`/api/savepoints/${current.slug}`, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    btn.textContent = "Saved";
  } catch (err) {
    btn.textContent = original;
    alert(err.message);
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 1500);
  }
};

$("console-close").onclick = closeConsole;
$("console-stop").onclick = async () => {
  if (!current) return;
  await api(`/api/sessions/${current.id}`, { method: "DELETE" }).catch(() => {});
  closeConsole();
};
$("console-log").onclick = async () => {
  if (!current) return;
  const box = $("console-logbox");
  if (!box.hidden) { box.hidden = true; return; }
  try {
    const { log } = await api(`/api/sessions/${current.id}/log`);
    box.textContent = log || "(no output yet)";
  } catch (err) {
    box.textContent = err.message;
  }
  box.hidden = false;
};

/* ---- new VM ---- */

const dialog = $("new-dialog");
$("new-btn").onclick = () => {
  const select = $("network-select");
  select.innerHTML = "";
  (state.limits.networkProfiles || ["none"]).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent =
      name === "none" ? "none — no NIC at all (safest)"
      : name === "internet" ? "internet — no LAN, no tailnet"
      : "full — unrestricted";
    select.append(opt);
  });
  $("new-err").hidden = true;
  dialog.showModal();
};

$("new-form").addEventListener("submit", async (event) => {
  if (event.submitter && event.submitter.value !== "create") return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  data.persist = "persist" in data;
  const err = $("new-err");
  try {
    await api("/api/catalog", { method: "POST", body: JSON.stringify(data) });
    dialog.close();
    event.target.reset();
    await refresh();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  }
});

refresh();
timer = setInterval(() => { if ($("console").hidden) refresh(); }, 5000);
