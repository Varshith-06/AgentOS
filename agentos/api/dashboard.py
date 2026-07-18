"""The dashboard (Phase 8, p.8): one HTML file, served by the daemon at /.

Running / waiting / blocked agents, the live dependency graph, the event
timeline, and cost — polling the same JSON API everything else uses. Vanilla
JS on purpose: no build step, no node, nothing to install; the API is the
interesting part and the page is a window onto it.

Status colors follow the reserved status palette (never reused for series),
and every state is always written as text — color never carries meaning alone.
"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AgentOS</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
    --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
    --accent: #2a78d6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19;
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
      --accent: #3987e5;
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--page); color: var(--ink);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 20px; max-width: 1200px; margin: 0 auto;
  }
  h1 { font-size: 18px; font-weight: 650; }
  h1 small { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 10px; }
  h2 { font-size: 12px; font-weight: 600; color: var(--ink-2);
       text-transform: uppercase; letter-spacing: .06em; margin: 0 0 8px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
           gap: 10px; margin: 16px 0; }
  .tile { background: var(--surface); border: 1px solid var(--border);
          border-radius: 8px; padding: 10px 14px; }
  .tile .v { font-size: 26px; font-weight: 650; }
  .tile .k { font-size: 11px; color: var(--muted); text-transform: uppercase;
             letter-spacing: .05em; }
  .cols { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
  section { background: var(--surface); border: 1px solid var(--border);
            border-radius: 8px; padding: 14px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse;
          font-variant-numeric: tabular-nums; }
  th { text-align: left; font-size: 11px; color: var(--muted); font-weight: 600;
       text-transform: uppercase; letter-spacing: .05em;
       border-bottom: 1px solid var(--grid); padding: 4px 8px; }
  td { padding: 5px 8px; border-bottom: 1px solid var(--grid); }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px;
           font-size: 12px; border: 1px solid var(--border); color: var(--ink-2); }
  .badge.Running  { color: var(--good); border-color: var(--good); }
  .badge.Blocked  { color: var(--warning); border-color: var(--warning); }
  .badge.Failed   { color: var(--critical); border-color: var(--critical); }
  .badge.Finished { color: var(--muted); }
  .num { text-align: right; }
  .log { font-family: ui-monospace, Consolas, monospace; font-size: 12px;
         color: var(--ink-2); max-height: 260px; overflow-y: auto; }
  .log div { padding: 1px 0; border-bottom: 1px dotted var(--grid); }
  .log .kind { color: var(--muted); display: inline-block; width: 64px; }
  svg text { font: 11px system-ui, sans-serif; fill: var(--ink); }
  svg .dep { fill: var(--muted); font-size: 10px; }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  #err { color: var(--critical); font-size: 12px; display: none; }
</style>
</head>
<body>
<h1>AgentOS <small id="meta">connecting…</small> <span id="err">runtime unreachable — retrying</span></h1>

<div class="tiles">
  <div class="tile"><div class="v" id="t-live">–</div><div class="k">live agents</div></div>
  <div class="tile"><div class="v" id="t-running">–</div><div class="k">running</div></div>
  <div class="tile"><div class="v" id="t-waiting">–</div><div class="k">waiting</div></div>
  <div class="tile"><div class="v" id="t-blocked">–</div><div class="k">blocked on humans</div></div>
  <div class="tile"><div class="v" id="t-cost">–</div><div class="k">model spend</div></div>
</div>

<section>
  <h2>Process table</h2>
  <table>
    <thead><tr><th>PID</th><th>Name</th><th>State</th><th>Waiting on</th>
      <th class="num">Ckpt</th><th class="num">Mem</th><th class="num">Cost</th></tr></thead>
    <tbody id="procs"></tbody>
  </table>
</section>

<div class="cols">
  <div>
    <section><h2>Dependency graph</h2><div id="graph" class="empty">nobody is waiting</div></section>
    <section><h2>Event timeline</h2><div id="events" class="log"></div></section>
  </div>
  <div>
    <section><h2>Kernel log</h2><div id="logs" class="log"></div></section>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const TERMINAL = ["Finished", "Failed"];

function fmtBytes(n) {
  if (!n) return "–";
  return n < 1024 ? n + "B" : (n / 1024).toFixed(1) + "K";
}

function badge(state) { return `<span class="badge ${esc(state)}">${esc(state)}</span>`; }

function drawGraph(snapshot) {
  const byPid = Object.fromEntries(snapshot.processes.map(p => [p.pid, p]));
  const deps = snapshot.deps;
  if (!deps.length) { $("graph").innerHTML = '<div class="empty">nobody is waiting</div>'; return; }
  const nodes = new Map();  // key -> {label, kind}
  deps.forEach(d => {
    nodes.set("pid:" + d.pid, {label: `${byPid[d.pid]?.name ?? "?"} #${d.pid}`, kind: "agent"});
    d.waits_on.forEach(k => {
      if (!nodes.has(k)) {
        const [kind, , name] = [k.split(":")[0], null, k.split(":").slice(1).join(":")];
        const label = kind === "agent" ? `${byPid[+name]?.name ?? "?"} #${name}` : `${kind} ${name}`;
        nodes.set(kind === "agent" ? "pid:" + name : k, {label, kind});
      }
    });
  });
  const keys = [...nodes.keys()];
  const W = 520, H = Math.max(200, 60 * Math.ceil(keys.length / 2)), R = Math.min(W, H) / 2 - 70;
  const pos = {};
  keys.forEach((k, i) => {
    const a = (2 * Math.PI * i) / keys.length - Math.PI / 2;
    pos[k] = [W / 2 + R * Math.cos(a), H / 2 + (H / 2 - 40) * Math.sin(a)];
  });
  let svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%">`;
  svg += `<defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--muted)"/></marker></defs>`;
  deps.forEach(d => {
    const from = pos["pid:" + d.pid];
    d.waits_on.forEach(k => {
      const key = k.startsWith("agent:") ? "pid:" + k.split(":")[1] : k;
      const to = pos[key];
      if (!to) return;
      svg += `<line x1="${from[0]}" y1="${from[1]}" x2="${to[0]}" y2="${to[1]}"
              stroke="var(--grid)" stroke-width="1.5" marker-end="url(#arr)"/>`;
    });
  });
  keys.forEach(k => {
    const [x, y] = pos[k], n = nodes.get(k);
    const cls = n.kind === "agent" ? "" : "dep";
    svg += `<circle cx="${x}" cy="${y}" r="5" fill="${n.kind === "agent" ? "var(--accent)" : "var(--muted)"}"/>`;
    svg += `<text class="${cls}" x="${x}" y="${y - 10}" text-anchor="middle">${esc(n.label)}</text>`;
  });
  $("graph").innerHTML = svg + "</svg>";
}

async function tick() {
  try {
    const [state, ps, events, logs] = await Promise.all([
      fetch("/state").then(r => r.json()),
      fetch("/ps").then(r => r.json()),
      fetch("/events?limit=15").then(r => r.json()),
      fetch("/logs?limit=25").then(r => r.json()),
    ]);
    $("err").style.display = "none";
    const procs = state.processes;
    const live = procs.filter(p => !TERMINAL.includes(p.status));
    const count = s => procs.filter(p => p.status === s).length;
    $("meta").textContent =
      `policy=${state.policy} · slots=${state.slots} · isolation=${state.isolation}` +
      (state.transport ? `(${state.transport})` : "") + ` · ${procs.length} agents`;
    $("t-live").textContent = live.length;
    $("t-running").textContent = count("Running");
    $("t-waiting").textContent = count("Waiting") + count("Sleeping");
    $("t-blocked").textContent = count("Blocked");
    const spend = Object.values(ps.costs).reduce((a, c) => a + c.cost, 0);
    $("t-cost").textContent = "$" + spend.toFixed(4);

    $("procs").innerHTML = procs.map(p => `<tr>
      <td>${p.pid}</td><td>${esc(p.name)}</td><td>${badge(p.status)}</td>
      <td>${esc(p.waiting_on ?? "–")}</td>
      <td class="num">${p.checkpoint ? "#" + p.checkpoint : "–"}</td>
      <td class="num">${fmtBytes((ps.memory[String(p.pid)] || 0) + (ps.memory[p.name] || 0))}</td>
      <td class="num">${ps.costs[String(p.pid)] ? "$" + ps.costs[String(p.pid)].cost.toFixed(4) : "–"}</td>
    </tr>`).join("") || '<tr><td colspan="7" class="empty">no agents yet</td></tr>';

    drawGraph(state);

    $("events").innerHTML = events.slice().reverse().map(e =>
      `<div><span class="kind">${esc(e.type)}</span> from ${e.source_pid == null ? "kernel" : "pid " + e.source_pid}
       → ${e.subscribers.length ? "woke " + e.subscribers.map(p => "pid " + p).join(", ") : "no subscribers"}</div>`
    ).join("") || '<div class="empty">no events yet</div>';

    $("logs").innerHTML = logs.slice().reverse().map(l =>
      `<div><span class="kind">${esc(l.kind)}</span>${l.pid == null ? "" : "pid " + l.pid + " "}${esc(l.message)}</div>`
    ).join("");
  } catch (err) {
    $("err").style.display = "inline";
  }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
