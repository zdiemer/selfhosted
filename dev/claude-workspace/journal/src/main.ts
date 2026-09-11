import { spawn } from "bun";
import fs from "node:fs";
import path from "node:path";
import { marked } from "marked";

// A read-only window onto the trading journal's WORKING COPY — the same files
// the trading agent reads and rewrites every run (dev/claude-workspace
// values `journal.dir`, normally /home/node/code/trading).
//
// Deliberately not a copy and not a database: the agent commits and pushes on
// every run, so the checkout on the PVC is the freshest thing that exists, and
// anything that mirrored it would only ever be staler. Every request reads
// from disk. The whole corpus is a few hundred KB of markdown, and a page
// served from a five-minute cache during a market hour is a page that can be
// wrong about a position that just moved.
//
// Read-only in the strong sense: no writes, no shell but `git log`, no route
// that takes a path from the URL without resolving it back inside `dir`.

const dir = process.env.JV_DIR || "/home/node/code/trading";
const port = Number(process.env.JV_PORT || 8088);
const title = process.env.JV_TITLE || "trading journal";
/** Entries rendered on the front page. Enough to cover a full trading day of
 * hourly runs (seven) plus the tail of yesterday. */
const RECENT = Number(process.env.JV_RECENT || 8);

/** Files offered in the nav, in order, with the label each gets. Anything not
 * listed here is still readable under /f/ if it is a .md inside `dir` — this
 * is the menu, not the permission boundary. */
const PAGES: { file: string; label: string }[] = [
  { file: "positions.md", label: "positions" },
  { file: "benchmark.md", label: "benchmark" },
  { file: "CLAUDE.md", label: "mandate" },
];

/**
 * Resolve a request path to a markdown file inside `dir`, or null.
 *
 * realpath, not a string prefix test: the journal lives on the same PVC as
 * every repo on this pod, and a symlink out of it would otherwise read as an
 * ordinary relative path. Both sides are resolved, so `..`, an absolute path
 * and a link all fail the same way — by not being under the root.
 */
function resolveMd(rel: string): string | null {
  if (!rel.endsWith(".md")) return null;
  let root: string;
  let full: string;
  try {
    root = fs.realpathSync(dir);
    full = fs.realpathSync(path.resolve(dir, rel));
  } catch {
    return null;
  }
  if (full !== root && !full.startsWith(root + path.sep)) return null;
  return fs.statSync(full).isFile() ? full : null;
}

/** Journal month files, newest first (`journal/2026-09.md` sorts by name). */
function months(): string[] {
  try {
    return fs
      .readdirSync(path.join(dir, "journal"))
      .filter((f) => f.endsWith(".md"))
      .sort()
      .reverse();
  } catch {
    return [];
  }
}

interface Entry {
  heading: string;
  body: string;
  month: string;
}

/**
 * Split a month file into its runs.
 *
 * One `##` per run is the journal's own convention (CLAUDE.md: every run
 * appends an entry), so the heading level is the record boundary rather than a
 * formatting choice. Anything above the first `##` — the `# September 2026`
 * title — is not an entry and is dropped.
 */
function entriesOf(md: string, month: string): Entry[] {
  const out: Entry[] = [];
  let heading = "";
  let body: string[] = [];
  for (const line of md.split("\n")) {
    if (/^## /.test(line)) {
      if (heading) out.push({ heading, body: body.join("\n").trim(), month });
      heading = line.replace(/^##\s*/, "").trim();
      body = [];
    } else if (heading) {
      body.push(line);
    }
  }
  if (heading) out.push({ heading, body: body.join("\n").trim(), month });
  // Newest first: the file is appended to, so the last entry is the latest run.
  return out.reverse();
}

/** The newest `RECENT` runs, reaching back into the previous month when the
 * current one is only a few days old — on the 1st, a page showing one entry
 * would otherwise look like an outage. */
function recentEntries(n: number): Entry[] {
  const out: Entry[] = [];
  for (const m of months()) {
    const full = resolveMd(path.join("journal", m));
    if (!full) continue;
    out.push(...entriesOf(fs.readFileSync(full, "utf8"), m));
    if (out.length >= n) break;
  }
  return out.slice(0, n);
}

function esc(s: string): string {
  return s.replace(/[&<>"]/g, (c) =>
    c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;" : "&quot;",
  );
}

function md(src: string): string {
  return marked.parse(src, { async: false, gfm: true }) as string;
}

const CSS = `
:root { color-scheme: dark; --bg:#0f1115; --card:#161a21; --line:#262c36;
        --fg:#e6e8eb; --dim:#9aa4b2; --accent:#7aa2f7; --up:#4ec9a4; --down:#f07178; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 -apple-system,
       BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }
main { max-width: 46rem; margin: 0 auto; padding: 0 1rem 4rem; }
header { position: sticky; top:0; background:rgba(15,17,21,.94);
         backdrop-filter: blur(8px); border-bottom:1px solid var(--line); z-index:2; }
header .bar { max-width:46rem; margin:0 auto; padding:.7rem 1rem; display:flex;
              flex-wrap:wrap; gap:.35rem .9rem; align-items:baseline; }
header a { color:var(--dim); text-decoration:none; font-size:.9rem; }
header a:hover, header a[aria-current] { color:var(--accent); }
header .home { color:var(--fg); font-weight:600; font-size:1rem; margin-right:.3rem; }
h1 { font-size:1.5rem; margin:1.4rem 0 .3rem; }
h2 { font-size:1.15rem; margin:1.6rem 0 .4rem; }
h3 { font-size:1rem; margin:1.3rem 0 .3rem; color:var(--dim);
     text-transform:none; letter-spacing:0; }
a { color:var(--accent); }
code { background:#1d222b; padding:.1em .35em; border-radius:4px; font-size:.87em; }
pre { background:#1d222b; padding:.8rem; border-radius:8px; overflow-x:auto; }
pre code { background:none; padding:0; }
blockquote { margin:.8rem 0; padding:.1rem 1rem; border-left:3px solid var(--line);
             color:var(--dim); }
hr { border:0; border-top:1px solid var(--line); margin:1.6rem 0; }
table { border-collapse:collapse; width:100%; font-size:.9rem; display:block;
        overflow-x:auto; }
th, td { border:1px solid var(--line); padding:.35rem .6rem; text-align:left; }
th { background:#1d222b; }
.entry { background:var(--card); border:1px solid var(--line); border-radius:10px;
         margin:1rem 0; padding:0 1rem; }
/* Two rows, not a flex line: the timestamp is one unbreakable unit ("2026-09-02
   3:30pm ET") and letting it share a line with the summary text wraps it a word
   per line on a phone. Marker in its own column so both rows hang off it. */
.entry > summary { cursor:pointer; padding:.85rem 0; font-weight:600;
                   list-style:none; display:grid; gap:.15rem .5rem;
                   grid-template-columns:1em 1fr; }
.entry > summary::-webkit-details-marker { display:none; }
.entry > summary::before { content:"▸"; color:var(--dim); grid-row:1; }
.entry[open] > summary::before { content:"▾"; }
.entry[open] > summary { border-bottom:1px solid var(--line); }
.entry > summary .when { color:var(--accent); white-space:nowrap;
                         grid-column:2; grid-row:1; }
.entry > summary .what { color:var(--fg); font-weight:400;
                         grid-column:2; grid-row:2; }
.body { padding-bottom:.6rem; }
.body > :first-child { margin-top:.7rem; }
.meta { color:var(--dim); font-size:.85rem; margin:.2rem 0 1.2rem; }
.log { list-style:none; padding:0; margin:1rem 0; }
.log li { border-bottom:1px solid var(--line); padding:.6rem 0; }
.log .when { color:var(--dim); font-size:.82rem; display:block; }
`;

function page(
  heading: string,
  bodyHtml: string,
  current = "",
  status = 200,
): Response {
  const nav = [
    { href: "/", label: "latest" },
    ...PAGES.map((p) => ({ href: `/f/${p.file}`, label: p.label })),
    { href: "/log", label: "commits" },
    ...months().map((m) => ({
      href: `/f/journal/${m}`,
      label: m.replace(/\.md$/, ""),
    })),
  ];
  const html = `<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>${esc(heading)} — ${esc(title)}</title>
<style>${CSS}</style></head><body>
<header><div class="bar"><a class="home" href="/">${esc(title)}</a>
${nav
  .map(
    (n) =>
      `<a href="${n.href}"${n.href === current ? ' aria-current="page"' : ""}>${esc(n.label)}</a>`,
  )
  .join("")}
</div></header><main>${bodyHtml}</main></body></html>`;
  return new Response(html, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      // The files change every hour on a market day, and a stale read of a
      // position is worse than a slow one.
      "cache-control": "no-store",
    },
  });
}

/**
 * A run heading, split for the summary line.
 *
 * The journal writes `2026-09-02, 3:30pm ET — no action, sixth consecutive…`;
 * the em dash separates when from what, and showing them in different weights
 * is what makes a list of seven runs scannable on a phone.
 */
function summaryOf(heading: string): string {
  const [when, ...rest] = heading.split(/\s+[—–]\s+/);
  const what = rest.join(" — ");
  return (
    `<span class="when">${esc(when)}</span>` +
    (what ? `<span class="what">${esc(what)}</span>` : "")
  );
}

function entryHtml(e: Entry, open: boolean): string {
  return (
    `<details class="entry"${open ? " open" : ""}>` +
    `<summary>${summaryOf(e.heading)}</summary>` +
    `<div class="body">${md(e.body)}</div></details>`
  );
}

function indexPage(): Response {
  const entries = recentEntries(RECENT);
  if (!entries.length)
    return page(
      "latest",
      `<h1>nothing to show</h1><p class="meta">No journal entries under ` +
        `<code>${esc(dir)}/journal</code>.</p>`,
      "/",
    );
  const newest = entries[0];
  return page(
    "latest",
    `<h1>latest runs</h1>` +
      `<p class="meta">${entries.length} most recent entries · newest ` +
      `${esc(newest.heading.split(/\s+[—–]\s+/)[0])} · ` +
      `<a href="/f/journal/${esc(newest.month)}">full ${esc(newest.month.replace(/\.md$/, ""))}</a></p>` +
      entries.map((e, i) => entryHtml(e, i === 0)).join(""),
    "/",
  );
}

/** A whole file. Month files are re-split into per-run sections so a 1,200-line
 * month opens as a list of runs rather than as a wall; everything else
 * (positions, benchmark, the mandate) renders straight through, because those
 * are documents meant to be read top to bottom. */
function filePage(rel: string): Response {
  const full = resolveMd(rel);
  if (!full) return page("not found", `<h1>404</h1>`, "", 404);
  const src = fs.readFileSync(full, "utf8");
  const stat = fs.statSync(full);
  const meta = `<p class="meta">${esc(rel)} · updated ${stat.mtime.toISOString().replace("T", " ").slice(0, 16)}Z</p>`;
  const href = `/f/${rel}`;
  if (rel.startsWith("journal/")) {
    const entries = entriesOf(src, path.basename(rel));
    const first = src.split("\n").find((l) => /^# /.test(l)) ?? rel;
    return page(
      rel,
      `<h1>${esc(first.replace(/^#\s*/, ""))}</h1>${meta}` +
        `<p class="meta">${entries.length} runs, newest first</p>` +
        entries.map((e, i) => entryHtml(e, i === 0)).join(""),
      href,
    );
  }
  return page(rel, meta + md(src), href);
}

/** `git log` over the journal checkout: one commit per run, which makes this
 * the cheapest answer to "did the agent actually run at 11:45?" — the push is
 * also its dead-man's-switch heartbeat (trading/CLAUDE.md). */
async function logPage(): Promise<Response> {
  const proc = spawn(
    [
      "git",
      "-C",
      dir,
      "log",
      "-n",
      "100",
      "--date=iso-local",
      "--pretty=format:%ad%x1f%s",
    ],
    { stdout: "pipe", stderr: "pipe" },
  );
  const out = await new Response(proc.stdout).text();
  if ((await proc.exited) !== 0)
    return page("commits", `<h1>commits</h1><p class="meta">git log failed</p>`, "/log");
  const rows = out
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      const [when, ...rest] = line.split("\x1f");
      return `<li><span class="when">${esc(when)}</span>${esc(rest.join(" "))}</li>`;
    });
  return page(
    "commits",
    `<h1>commits</h1><p class="meta">last ${rows.length} pushes from the trading agent</p>` +
      `<ul class="log">${rows.join("")}</ul>`,
    "/log",
  );
}

const server = Bun.serve({
  port,
  hostname: "0.0.0.0",
  async fetch(req) {
    const url = new URL(req.url);
    // Read-only surface: anything that isn't a GET is a bug or a probe.
    if (req.method !== "GET" && req.method !== "HEAD")
      return new Response("method not allowed", { status: 405 });
    if (url.pathname === "/healthz") return new Response("ok");
    if (url.pathname === "/") return indexPage();
    if (url.pathname === "/log") return logPage();
    if (url.pathname.startsWith("/f/"))
      return filePage(decodeURIComponent(url.pathname.slice(3)));
    return page("not found", `<h1>404</h1>`, "", 404);
  },
});

console.log(`journal viewer: ${dir} on :${server.port}`);
