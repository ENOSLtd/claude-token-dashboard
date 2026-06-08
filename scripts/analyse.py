#!/usr/bin/env python3
"""
Claude Token Usage Analyser — v2
Reads sessions_export.json (token counts from JSONL) and
analyses/YYYY-MM-DD.json files (LLM analysis from overnight routine),
merges them, and writes docs/index.html.

Run by GitHub Actions at 01:30 UTC after the routine pushes its analysis.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from glob import glob

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).parent.parent
TOKEN_FILE   = REPO_ROOT / "data" / "sessions_export.json"
ANALYSES_DIR = REPO_ROOT / "analyses"
OUTPUT_FILE  = REPO_ROOT / "docs" / "index.html"
OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
ANALYSES_DIR.mkdir(parents=True, exist_ok=True)

# ── Load token data ───────────────────────────────────────────────────────────
if not TOKEN_FILE.exists():
    print(f"ERROR: {TOKEN_FILE} not found. Run the PowerShell exporter first.")
    sys.exit(1)

with open(TOKEN_FILE, encoding="utf-8") as f:
    token_data = json.load(f)

sessions_raw  = token_data.get("sessions", [])
LIMIT         = token_data.get("estimated_limit", 88000)
exported_at   = token_data.get("exported_at", "")

# ── Load all routine analysis files ──────────────────────────────────────────
analyses_by_session = {}   # session_id → analysis entry
analyses_meta = {}         # date → {daily_summary, top_improvements, transcript_access}

for analysis_file in sorted(ANALYSES_DIR.glob("*.json")):
    try:
        with open(analysis_file, encoding="utf-8") as f:
            day_data = json.load(f)
        date_str = day_data.get("date", "")
        analyses_meta[date_str] = {
            "daily_summary":    day_data.get("daily_summary", ""),
            "top_improvements": day_data.get("top_improvements", []),
            "transcript_access": day_data.get("transcript_access", False),
        }
        for s in day_data.get("sessions", []):
            sid = s.get("session_id", "")
            if sid:
                analyses_by_session[sid] = s
    except Exception as e:
        print(f"Warning: could not parse {analysis_file.name}: {e}")

print(f"Loaded {len(analyses_by_session)} routine analyses across {len(analyses_meta)} days.")

# ── Parse and enrich sessions ─────────────────────────────────────────────────
def parse_dt(s):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None

def eff_tokens(s):
    return s["input_tokens"] + s["output_tokens"] + s["cache_create"]

def cache_hit_rate(s):
    total = s["cache_create"] + s["cache_read"]
    return (s["cache_read"] / total) if total > 0 and s["api_calls"] > 1 else None

def heuristic_recs(s):
    """Fallback recommendations when no routine analysis is available."""
    recs = []
    if s["cache_create"] > 25_000:
        recs.append(f"Large system prompt (~{s['cache_create']:,} tokens). Trim CLAUDE.md and project instructions.")
    hit = cache_hit_rate(s)
    if hit is not None and hit < 0.35:
        recs.append(f"Low cache reuse ({hit:.0%}). Prefer fewer, longer sessions over many short ones.")
    if s["api_calls"] > 20:
        recs.append(f"High tool-call count ({s['api_calls']}). Batch related requests and be more specific.")
    if s["api_calls"] > 0 and s["output_tokens"] / s["api_calls"] > 3_000:
        recs.append("High average output. Ask for concise answers on exploratory turns.")
    if eff_tokens(s) > 120_000:
        recs.append("Very large session. Break complex tasks into smaller focused conversations.")
    if not recs:
        recs.append("No significant inefficiencies detected from token data alone.")
    return recs

sessions = []
for raw in sessions_raw:
    dt = parse_dt(raw.get("start_time", ""))
    if dt is None:
        continue
    s = dict(raw)
    s["start_dt"]   = dt
    s["eff_tokens"] = eff_tokens(s)

    # Merge routine analysis if available
    routine = analyses_by_session.get(raw.get("session_id", ""))
    if routine:
        s["recs"]             = routine.get("recommendations", [])
        s["efficiency_score"] = routine.get("efficiency_score")
        s["analysis_note"]    = routine.get("analysis_note", "")
        s["has_llm_analysis"] = True
    else:
        s["recs"]             = heuristic_recs(s)
        s["efficiency_score"] = None
        s["analysis_note"]    = ""
        s["has_llm_analysis"] = False

    sessions.append(s)

sessions.sort(key=lambda s: s["start_dt"])

# ── Group into 5-hour rolling windows ────────────────────────────────────────
WINDOW_H = 5
windows = []
current = None

for s in sessions:
    t = s["start_dt"]
    if current is None or t >= current["end_dt"]:
        current = {"start_dt": t, "end_dt": t + timedelta(hours=WINDOW_H),
                   "sessions": [], "total_eff": 0}
        windows.append(current)
    current["sessions"].append(s)
    current["total_eff"] += s["eff_tokens"]
    s["window_idx"] = len(windows) - 1

# ── Summary stats ─────────────────────────────────────────────────────────────
now_utc   = datetime.now(timezone.utc)
week_ago  = now_utc - timedelta(days=7)

recent_sessions   = [s for s in sessions if s["start_dt"] >= week_ago]
total_recent_eff  = sum(s["eff_tokens"] for s in recent_sessions)
windows_at_limit  = sum(1 for w in windows if w["total_eff"] >= LIMIT * 0.90)
cowork_count      = sum(1 for s in sessions if s["type"] == "cowork")
llm_analysed      = sum(1 for s in recent_sessions if s["has_llm_analysis"])

# Latest daily summary (if any)
latest_date  = max(analyses_meta.keys()) if analyses_meta else None
daily_banner = ""
if latest_date and analyses_meta[latest_date].get("daily_summary"):
    meta = analyses_meta[latest_date]
    daily_banner = meta["daily_summary"]
    top_imps = meta.get("top_improvements", [])
else:
    top_imps = []

# ── Chart data (last 60 days) ─────────────────────────────────────────────────
cutoff60  = now_utc - timedelta(days=60)
cw        = [w for w in windows if w["start_dt"] >= cutoff60]

chart_labels = [w["start_dt"].strftime("%d %b %H:%M") for w in cw]
chart_tokens = [w["total_eff"] for w in cw]
chart_pct    = [round(min(w["total_eff"] / LIMIT * 100, 100), 1) for w in cw]
chart_colors = [
    "rgba(239,68,68,0.8)" if p >= 80 else
    "rgba(234,179,8,0.8)" if p >= 50 else
    "rgba(34,197,94,0.8)"
    for p in chart_pct
]

# ── Session rows HTML ─────────────────────────────────────────────────────────
def score_badge(score):
    if score is None:
        return '<span class="score-none">—</span>'
    colour = "#ef4444" if score <= 4 else "#eab308" if score <= 6 else "#22c55e"
    return f'<span class="score-badge" style="background:{colour}22;color:{colour}">{score}/10</span>'

def type_badge(t):
    label = "Cowork" if t == "cowork" else "CLI"
    cls   = "badge-cowork" if t == "cowork" else "badge-cli"
    return f'<span class="badge {cls}">{label}</span>'

def model_badge(model):
    if not model: return ""
    short = model.replace("claude-","").replace("-20"," ").strip()
    return f'<span class="badge">{short}</span>'

def pct_bar(pct):
    colour = "#ef4444" if pct >= 80 else "#eab308" if pct >= 50 else "#22c55e"
    w = min(pct, 100)
    return (f'<div class="pct-wrap" title="{pct:.1f}% of estimated window limit">'
            f'<div class="pct-bar" style="width:{w}%;background:{colour}"></div>'
            f'<span class="pct-lbl">{pct:.1f}%</span></div>')

def source_tag(has_llm):
    if has_llm:
        return '<span class="src-llm" title="Recommendations from overnight LLM analysis">LLM ✓</span>'
    return '<span class="src-heur" title="Heuristic recommendations (no LLM analysis yet)">heuristic</span>'

session_rows = []
for s in reversed(sessions):
    dt_str  = s["start_dt"].strftime("%d %b %Y %H:%M")
    w       = windows[s["window_idx"]]
    pct_win = min(s["eff_tokens"] / LIMIT * 100, 100)
    recs_li = "".join(f"<li>{r}</li>" for r in s["recs"])
    note    = f'<div class="analysis-note">{s["analysis_note"]}</div>' if s["analysis_note"] else ""

    row = f"""
    <tr class="session-row" onclick="toggleRow(this)">
      <td class="td-date">{dt_str}</td>
      <td>{type_badge(s['type'])} {model_badge(s.get('model',''))}</td>
      <td class="td-title" title="{s['title']}">{s['title']}</td>
      <td class="td-num">{s['eff_tokens']:,}</td>
      <td class="td-num">{s['output_tokens']:,}</td>
      <td class="td-num">{s['api_calls']}</td>
      <td>{score_badge(s['efficiency_score'])}</td>
      <td class="td-pct">{pct_bar(pct_win)}</td>
    </tr>
    <tr class="detail-row hidden">
      <td colspan="8">
        <div class="detail-panel">
          {note}
          <div class="rec-header">{source_tag(s['has_llm_analysis'])} Recommendations</div>
          <ul class="rec-list">{recs_li}</ul>
          <div class="tok-breakdown">
            Input: {s['input_tokens']:,} &nbsp;|&nbsp;
            Cache created: {s['cache_create']:,} &nbsp;|&nbsp;
            Cache read: {s['cache_read']:,} &nbsp;|&nbsp;
            Output: {s['output_tokens']:,}
          </div>
        </div>
      </td>
    </tr>"""
    session_rows.append(row)

session_rows_html = "\n".join(session_rows)

# Daily banner HTML
banner_html = ""
if daily_banner:
    improvements = "".join(f"<li>{i}</li>" for i in top_imps)
    imps_block = f"<ul class='top-imps'>{improvements}</ul>" if improvements else ""
    banner_html = f"""
    <div class="daily-banner">
      <div class="banner-label">TODAY'S ANALYSIS — {latest_date}</div>
      <p>{daily_banner}</p>
      {imps_block}
    </div>"""

# ── Timestamps ────────────────────────────────────────────────────────────────
exp_str = (datetime.fromisoformat(exported_at.replace("Z","+00:00"))
           .strftime("%d %b %Y %H:%M UTC") if exported_at else "unknown")
gen_str = now_utc.strftime("%d %b %Y %H:%M UTC")

# ── Assemble HTML ─────────────────────────────────────────────────────────────
html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Claude Token Dashboard</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg:#0f172a;--card:#1e293b;--border:#334155;
      --text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          background:var(--bg);color:var(--text);min-height:100vh}}
    header{{background:var(--card);border-bottom:1px solid var(--border);
            padding:1rem 2rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap}}
    header h1{{font-size:1.1rem;font-weight:600;color:var(--accent)}}
    .meta{{font-size:.7rem;color:var(--muted);margin-left:auto}}
    .container{{max-width:1440px;margin:0 auto;padding:1.25rem 1.5rem}}
    .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.75rem;margin-bottom:1rem}}
    .card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:1rem}}
    .card-label{{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}}
    .card-value{{font-size:1.6rem;font-weight:700;margin-top:.2rem}}
    .card-sub{{font-size:.65rem;color:var(--muted);margin-top:.2rem}}
    .daily-banner{{background:#1a2e1a;border:1px solid #166534;border-radius:8px;
                   padding:1rem 1.25rem;margin-bottom:1rem}}
    .banner-label{{font-size:.65rem;color:#4ade80;text-transform:uppercase;
                   letter-spacing:.05em;margin-bottom:.4rem}}
    .daily-banner p{{font-size:.85rem;color:#d1fae5;line-height:1.5}}
    .top-imps{{margin:.5rem 0 0 1.25rem}}
    .top-imps li{{font-size:.8rem;color:#86efac;margin-bottom:.25rem}}
    .chart-card,.table-card{{background:var(--card);border:1px solid var(--border);
                              border-radius:8px;padding:1.25rem;margin-bottom:1rem}}
    .chart-card h2,.table-card h2{{font-size:.75rem;font-weight:600;color:var(--muted);margin-bottom:.75rem}}
    .chart-wrap{{position:relative;height:220px}}
    .legend{{display:flex;gap:1rem;font-size:.7rem;color:var(--muted);margin-bottom:.5rem;flex-wrap:wrap}}
    .dot{{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:4px}}
    table{{width:100%;border-collapse:collapse;font-size:.78rem}}
    th{{text-align:left;padding:.45rem .6rem;color:var(--muted);
        border-bottom:1px solid var(--border);font-weight:500;white-space:nowrap}}
    .session-row{{cursor:pointer;transition:background .12s}}
    .session-row:hover{{background:rgba(56,189,248,.05)}}
    td{{padding:.45rem .6rem;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}}
    .td-date{{white-space:nowrap;color:var(--muted);font-size:.72rem}}
    .td-title{{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .td-num{{text-align:right;font-variant-numeric:tabular-nums}}
    .td-pct{{min-width:120px}}
    .badge{{display:inline-block;font-size:.62rem;padding:1px 5px;border-radius:3px;
             background:rgba(148,163,184,.15);color:var(--muted)}}
    .badge-cowork{{background:rgba(56,189,248,.15);color:var(--accent)}}
    .badge-cli{{background:rgba(168,85,247,.15);color:#c084fc}}
    .score-badge{{display:inline-block;font-size:.7rem;padding:1px 6px;border-radius:4px;font-weight:600}}
    .score-none{{color:var(--muted);font-size:.7rem}}
    .pct-wrap{{display:flex;align-items:center;gap:5px}}
    .pct-bar{{height:5px;border-radius:3px;min-width:2px}}
    .pct-lbl{{font-size:.68rem;color:var(--muted);white-space:nowrap}}
    .detail-row{{background:rgba(15,23,42,.5)}}
    .detail-row.hidden{{display:none}}
    .detail-panel{{padding:.75rem 1rem}}
    .analysis-note{{font-size:.82rem;color:#bae6fd;font-style:italic;margin-bottom:.5rem}}
    .rec-header{{font-size:.72rem;color:var(--accent);margin-bottom:.3rem;display:flex;align-items:center;gap:.5rem}}
    .rec-list{{margin:.25rem 0 .5rem 1.25rem}}
    .rec-list li{{font-size:.78rem;color:var(--text);margin-bottom:.25rem;line-height:1.4}}
    .tok-breakdown{{font-size:.68rem;color:var(--muted);margin-top:.4rem}}
    .src-llm{{font-size:.65rem;background:rgba(34,197,94,.15);color:#4ade80;
               padding:1px 5px;border-radius:3px}}
    .src-heur{{font-size:.65rem;background:rgba(148,163,184,.1);color:var(--muted);
                padding:1px 5px;border-radius:3px}}
  </style>
</head>
<body>

<header>
  <h1>Claude Token Usage Dashboard</h1>
  <div class="meta">
    Data: {exp_str} &nbsp;|&nbsp;
    Generated: {gen_str} &nbsp;|&nbsp;
    Limit: ~{LIMIT:,} tokens / 5-hr window
  </div>
</header>

<div class="container">

  <div class="cards">
    <div class="card">
      <div class="card-label">Sessions (7 days)</div>
      <div class="card-value">{len(recent_sessions)}</div>
      <div class="card-sub">{llm_analysed} with LLM analysis</div>
    </div>
    <div class="card">
      <div class="card-label">Tokens this week</div>
      <div class="card-value">{total_recent_eff:,}</div>
      <div class="card-sub">effective (input + output + cache-create)</div>
    </div>
    <div class="card">
      <div class="card-label">Windows &ge;90% limit</div>
      <div class="card-value" style="color:#ef4444">{windows_at_limit}</div>
      <div class="card-sub">of {len(windows)} total 5-hr windows</div>
    </div>
    <div class="card">
      <div class="card-label">All-time sessions</div>
      <div class="card-value">{len(sessions)}</div>
      <div class="card-sub">{cowork_count} Cowork</div>
    </div>
  </div>

  {banner_html}

  <div class="chart-card">
    <h2>TOKEN USAGE PER 5-HOUR WINDOW — LAST 60 DAYS</h2>
    <div class="legend">
      <span><span class="dot" style="background:#22c55e"></span>&lt;50% of limit</span>
      <span><span class="dot" style="background:#eab308"></span>50–80%</span>
      <span><span class="dot" style="background:#ef4444"></span>&ge;80%</span>
      <span style="color:#38bdf8">— % of limit (right axis)</span>
    </div>
    <div class="chart-wrap"><canvas id="trendChart"></canvas></div>
  </div>

  <div class="table-card">
    <h2>INDIVIDUAL SESSIONS — click a row to expand recommendations</h2>
    <table>
      <thead><tr>
        <th>Start</th><th>Type</th><th>Session</th>
        <th style="text-align:right">Eff.&nbsp;Tokens</th>
        <th style="text-align:right">Output</th>
        <th style="text-align:right">Calls</th>
        <th>Score</th><th>% of Window</th>
      </tr></thead>
      <tbody>{session_rows_html}</tbody>
    </table>
  </div>

</div>

<script>
const ctx = document.getElementById("trendChart").getContext("2d");
new Chart(ctx, {{
  type:"bar",
  data:{{
    labels:{json.dumps(chart_labels)},
    datasets:[
      {{label:"Effective tokens",data:{json.dumps(chart_tokens)},
        backgroundColor:{json.dumps(chart_colors)},borderRadius:3,yAxisID:"y"}},
      {{label:"% of limit",data:{json.dumps(chart_pct)},type:"line",
        borderColor:"rgba(56,189,248,0.7)",backgroundColor:"transparent",
        pointRadius:2,borderWidth:1.5,yAxisID:"y2"}}
    ]
  }},
  options:{{
    responsive:true,maintainAspectRatio:false,
    interaction:{{mode:"index",intersect:false}},
    plugins:{{
      legend:{{labels:{{color:"#94a3b8",font:{{size:10}}}}}},
      tooltip:{{callbacks:{{label:c=>c.dataset.label==="% of limit"
        ?`  ${{c.parsed.y.toFixed(1)}}% of limit`
        :`  ${{c.parsed.y.toLocaleString()}} tokens`}}}}
    }},
    scales:{{
      x:{{ticks:{{color:"#64748b",maxRotation:45,font:{{size:9}}}},
           grid:{{color:"rgba(255,255,255,.04)"}}}},
      y:{{position:"left",ticks:{{color:"#64748b",font:{{size:9}},
           callback:v=>v.toLocaleString()}},
           grid:{{color:"rgba(255,255,255,.04)"}},
           title:{{display:true,text:"Tokens",color:"#64748b",font:{{size:9}}}},
           suggestedMax:{LIMIT}}},
      y2:{{position:"right",min:0,max:100,
           ticks:{{color:"#38bdf8",font:{{size:9}},callback:v=>v+"%"}},
           grid:{{drawOnChartArea:false}},
           title:{{display:true,text:"% of limit",color:"#38bdf8",font:{{size:9}}}}}}
    }}
  }}
}});

function toggleRow(row){{
  const next=row.nextElementSibling;
  if(next&&next.classList.contains("detail-row"))
    next.classList.toggle("hidden");
}}
</script>
</body></html>
"""

OUTPUT_FILE.write_text(html, encoding="utf-8")
print(f"Dashboard written → {OUTPUT_FILE}")
print(f"Sessions: {len(sessions)}  |  Windows: {len(windows)}  |  LLM-analysed: {sum(1 for s in sessions if s['has_llm_analysis'])}")
