#!/usr/bin/env python3
"""Build the test dashboard: a static page published to GitHub Pages by CI.

  python tools/test_dashboard.py --junit test-results.xml --junit e2e-results.xml \\
      --coverage coverage.xml --htmlcov htmlcov --out site

Reads JUnit XML (pytest's, and the demo's end-to-end checks from DEMO_JUNIT) and
coverage.py's XML report, and writes:

  site/index.html          headline numbers, tests by layer, coverage by component, every test
  site/coverage/           the line-by-line coverage report (copied from --htmlcov)
  site/badges/*.svg        README badges: tests, coverage, end-to-end checks

Standard library only. Missing inputs are shown as "no data", so a partial CI run still
publishes a page. GITHUB_* variables, when present, add the commit and a link to the run.
"""
import argparse
import html
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LAYERS = {
    "unit": ("Unit", "Single functions and classes, no I/O: message rules, verification, "
                     "rollout decisions"),
    "component": ("Component", "One real dependency: PostgreSQL, real firmware processes, "
                               "a local package server"),
    "e2e": ("End-to-end", "The whole system in containers: control plane, 5 devices, "
                          "real updates, crashes and rollbacks"),
}
COMPONENTS = {
    "deploy.device-agent": "Device agent",
    "deploy.orchestrator": "Orchestrator",
    "deploy.registries.software": "Software registry",
    "tools": "Release and CI tools",
}


@dataclass
class Case:
    layer: str
    group: str
    name: str
    status: str          # passed, failed, skipped
    seconds: float
    message: str


# --------------------------------------------------------------------------- inputs

def humanize(test_name: str) -> str:
    name = re.sub(r"^test_", "", test_name)
    params = ""
    if "[" in name:
        name, params = name.split("[", 1)
        params = f" [{params.rstrip(']')}]"
    return name.replace("_", " ") + params


def read_junit(path: Path) -> list[Case]:
    cases = []
    for tc in ET.parse(path).getroot().iter("testcase"):
        classname = tc.get("classname", "")
        if classname.startswith("e2e."):
            layer, group, name = "e2e", classname[4:], tc.get("name", "")
        else:
            parts = classname.split(".")
            layer = parts[1] if len(parts) > 2 and parts[1] in LAYERS else "unit"
            group = f"{parts[-1]}.py" if parts else classname
            name = humanize(tc.get("name", ""))
        status, message = "passed", ""
        for tag, result in (("failure", "failed"), ("error", "failed"), ("skipped", "skipped")):
            el = tc.find(tag)
            if el is not None:
                status = result
                message = (el.get("message") or el.text or "").strip()
                break
        cases.append(Case(layer, group, name, status, float(tc.get("time") or 0), message))
    return cases


def read_coverage(path: Path) -> tuple[float, list[tuple[str, int, int]]]:
    """Total line rate and (component, covered, total) per package."""
    root = ET.parse(path).getroot()
    rows = []
    for pkg in root.iter("package"):
        lines = list(pkg.iter("line"))
        covered = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
        rows.append((COMPONENTS.get(pkg.get("name"), pkg.get("name")), covered, len(lines)))
    return float(root.get("line-rate", 0)), rows


# --------------------------------------------------------------------------- badges

def badge(label: str, value: str, color: str) -> str:
    """A flat badge like shields.io's, sized from an estimate of the text width."""
    def width(text):
        return 10 + round(sum(7.4 if c.isupper() or c in "mwMW%" else 6.2 for c in text))
    lw, vw = width(label), width(value)
    total = lw + vw
    esc = html.escape
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{esc(label)}: {esc(value)}">
<title>{esc(label)}: {esc(value)}</title>
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>
<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/><rect width="{total}" height="20" fill="url(#s)"/></g>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
<text x="{lw / 2}" y="15" fill="#010101" fill-opacity=".3">{esc(label)}</text><text x="{lw / 2}" y="14">{esc(label)}</text>
<text x="{lw + vw / 2}" y="15" fill="#010101" fill-opacity=".3">{esc(value)}</text><text x="{lw + vw / 2}" y="14">{esc(value)}</text>
</g></svg>
'''


GOOD, WARN, BAD, NONE = "#2e7d32", "#b26a00", "#c62828", "#757575"


# --------------------------------------------------------------------------- page

CSS = """
:root {
  color-scheme: light;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --border: #e2e0da;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77756f;
  --series-1: #2a78d6; --track: #e9e7e2; --floor: #52514e;
  --good: #0a7f0a; --critical: #c42f2f; --warning: #8a5a00;
  --good-bg: #e6f4e6; --critical-bg: #fbe9e9; --warning-bg: #fbf1dc;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0: #121211; --surface-1: #1a1a19; --border: #2f2f2c;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
    --series-1: #3987e5; --track: #2a2a28; --floor: #c3c2b7;
    --good: #3fbf3f; --critical: #ef6b6b; --warning: #f0b84a;
    --good-bg: #16301a; --critical-bg: #3a1a1a; --warning-bg: #352a12;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-0: #121211; --surface-1: #1a1a19; --border: #2f2f2c;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --text-muted: #9a998f;
  --series-1: #3987e5; --track: #2a2a28; --floor: #c3c2b7;
  --good: #3fbf3f; --critical: #ef6b6b; --warning: #f0b84a;
  --good-bg: #16301a; --critical-bg: #3a1a1a; --warning-bg: #352a12;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text-primary);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 16px 64px; }
header h1 { margin: 0; font-size: 26px; letter-spacing: -0.01em; }
header p { margin: 4px 0 0; color: var(--text-secondary); }
a { color: var(--series-1); }
.status { display: inline-flex; align-items: center; gap: 8px; margin-top: 16px; padding: 6px 14px;
          border-radius: 999px; font-weight: 600; }
.status.good { background: var(--good-bg); color: var(--good); }
.status.bad { background: var(--critical-bg); color: var(--critical); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;
         margin: 24px 0; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
        padding: 14px 16px; }
.tile .label { color: var(--text-secondary); font-size: 13px; }
.tile .value { font-size: 32px; font-weight: 650; line-height: 1.2; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--text-muted); font-size: 12px; }
section { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
          padding: 20px; margin-top: 16px; }
section h2 { margin: 0 0 4px; font-size: 18px; }
section > p { margin: 0 0 16px; color: var(--text-secondary); font-size: 14px; }
.bars { display: grid; gap: 14px; }
.bar-row { display: grid; grid-template-columns: 170px 1fr; gap: 12px; align-items: center; }
.bar-row .name { font-weight: 600; }
.bar-row .name small { display: block; font-weight: 400; color: var(--text-muted); font-size: 12px;
                       line-height: 1.3; }
.track { position: relative; height: 22px; }
.fill { position: relative; height: 22px; background: var(--series-1); border-radius: 0 4px 4px 0;
        min-width: 2px; }
.fill:hover { filter: brightness(1.1); }
.fill[data-tip]:hover::after { content: attr(data-tip); position: absolute; left: 0; bottom: 30px;
        white-space: nowrap; background: var(--text-primary); color: var(--surface-1);
        font-size: 12px; padding: 4px 8px; border-radius: 6px; z-index: 2; }
.bar-value { position: absolute; top: 1px; font-size: 13px; font-variant-numeric: tabular-nums;
             color: var(--text-primary); white-space: nowrap; }
.coverage .track { background: var(--track); border-radius: 0 4px 4px 0; }
.coverage .bar-row { grid-template-columns: 170px 1fr 52px; }
.coverage .pct { font-variant-numeric: tabular-nums; font-weight: 600; text-align: right; }
.coverage .total { padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.floor { position: absolute; top: -4px; bottom: -4px; width: 2px; background: var(--floor); }
.floor-label { font-size: 12px; color: var(--text-muted); margin-top: 8px; }
.icon-good { color: var(--good); } .icon-bad { color: var(--critical); }
.icon-skip { color: var(--warning); }
.filters { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.filters button { font: inherit; font-size: 13px; padding: 4px 12px; border-radius: 999px;
                  border: 1px solid var(--border); background: var(--surface-0);
                  color: var(--text-primary); cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--text-primary); color: var(--surface-1); }
details { border-top: 1px solid var(--border); }
details:last-of-type { border-bottom: 1px solid var(--border); }
summary { cursor: pointer; padding: 10px 4px; display: flex; gap: 10px; align-items: baseline; }
summary .layer { font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
                 color: var(--text-muted); min-width: 84px; }
summary .group { font-weight: 600; flex: 1; overflow-wrap: anywhere; }
summary .counts { color: var(--text-secondary); font-size: 13px; white-space: nowrap; }
table { width: 100%; border-collapse: collapse; font-size: 14px; margin: 0 0 12px; }
td { padding: 4px; vertical-align: top; border-top: 1px solid var(--border); }
td.st { width: 28px; text-align: center; } td.t { width: 70px; text-align: right;
        color: var(--text-muted); font-variant-numeric: tabular-nums; }
td .msg { display: block; color: var(--critical); font-size: 12px; overflow-wrap: anywhere; }
footer { margin-top: 24px; color: var(--text-muted); font-size: 13px; }
@media (max-width: 640px) {
  .bar-row, .coverage .bar-row { grid-template-columns: 1fr; gap: 4px; }
  .coverage .pct { text-align: left; }
  .tile .value { font-size: 26px; }
}
"""

JS = """
document.querySelectorAll('.filters button').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.filters button').forEach(b => b.setAttribute('aria-pressed', b === btn));
  const want = btn.dataset.filter;
  document.querySelectorAll('details.suite').forEach(d => {
    let shown = 0;
    d.querySelectorAll('tr').forEach(tr => {
      const ok = want === 'all' || tr.dataset.status === want;
      tr.hidden = !ok; shown += ok;
    });
    d.hidden = shown === 0;
    if (want !== 'all' && shown) d.open = true;
  });
}));
"""

ICONS = {"passed": ('<span class="icon-good" aria-label="passed">✔</span>'),
         "failed": ('<span class="icon-bad" aria-label="failed">✘</span>'),
         "skipped": ('<span class="icon-skip" aria-label="skipped">–</span>')}


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def build_page(cases: list[Case], coverage, floor: int | None, meta: dict) -> str:
    esc = html.escape
    by_status = defaultdict(int)
    for c in cases:
        by_status[c.status] += 1
    total = len(cases)
    failed = by_status["failed"]
    e2e = [c for c in cases if c.layer == "e2e"]
    e2e_passed = sum(c.status == "passed" for c in e2e)

    status = (f'<div class="status bad">✘ {failed} failing</div>' if failed else
              '<div class="status good">✔ All tests passing</div>' if total else
              '<div class="status bad">No test results</div>')

    cov_value = pct(coverage[0]) if coverage else "no data"
    tiles = [
        ("Tests run", str(total), f"{by_status['skipped']} skipped" if by_status["skipped"] else "none skipped"),
        ("Passed", str(by_status["passed"]), f"{by_status['passed'] / total * 100:.0f}% pass rate" if total else ""),
        ("Failed", str(failed), "must stay at 0 in CI"),
        ("Code coverage", cov_value, f"minimum {floor}%" if floor else "unit + component tests"),
        ("End-to-end checks", f"{e2e_passed}/{len(e2e)}" if e2e else "no data",
         "real system, real devices"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
        f'<div class="sub">{esc(sub)}</div></div>' for label, value, sub in tiles)

    # Tests by layer: one series (count), so one color and direct labels.
    layer_counts = {k: [c for c in cases if c.layer == k] for k in LAYERS}
    biggest = max((len(v) for v in layer_counts.values()), default=1) or 1
    layer_rows = []
    for key, (title, desc) in LAYERS.items():
        items = layer_counts[key]
        n = len(items)
        bad = sum(c.status == "failed" for c in items)
        width = n / biggest * 82
        state = f"{bad} failing" if bad else "all passing" if n else "no data"
        layer_rows.append(
            f'<div class="bar-row"><div class="name">{esc(title)}<small>{esc(desc)}</small></div>'
            f'<div class="track"><div class="fill" style="width:{width:.1f}%" '
            f'data-tip="{esc(title)}: {n} tests, {state}"></div>'
            f'<span class="bar-value" style="left:calc({width:.1f}% + 8px)">{n} · {esc(state)}</span>'
            f'</div></div>')

    # Coverage by component: magnitude 0-100, single hue, floor marker.
    if coverage:
        def cov_row(name, covered, lines, extra="", cls=""):
            rate = covered / lines if lines else 0
            return (f'<div class="bar-row{cls}"><div class="name">{esc(name)}'
                    f'<small>{covered} of {lines} lines</small></div>'
                    f'<div class="track"><div class="fill" style="width:{rate * 100:.1f}%" '
                    f'data-tip="{esc(name)}: {covered} of {lines} lines run by tests"></div>{extra}</div>'
                    f'<div class="pct">{pct(rate)}</div></div>')

        rows = sorted(coverage[1], key=lambda r: r[0])
        all_covered, all_lines = sum(r[1] for r in rows), sum(r[2] for r in rows)
        marker = (f'<div class="floor" style="left:{floor}%" title="minimum {floor}%"></div>'
                  if floor else "")
        cov_rows = [cov_row("Total", all_covered, all_lines, marker, " total")]
        cov_rows += [cov_row(name, covered, lines) for name, covered, lines in rows]
        cov_html = ('<div class="bars coverage">' + "".join(cov_rows) + "</div>"
                    + (f'<div class="floor-label">The line on the total is the minimum CI enforces '
                       f'({floor}%); the run fails below it.</div>' if floor else "")
                    + ('<p style="margin:12px 0 0"><a href="coverage/index.html">Line-by-line coverage '
                       'report →</a> (green lines run during tests, red lines do not)</p>'
                       if meta.get("htmlcov") else ""))
    else:
        cov_html = "<p>No coverage data in this run.</p>"

    # Every test, grouped by file / demo step.
    groups = defaultdict(list)
    for c in cases:
        groups[(list(LAYERS).index(c.layer), c.layer, c.group)].append(c)
    suites = []
    for (_, layer, group), items in sorted(groups.items()):
        bad = sum(c.status == "failed" for c in items)
        skipped = sum(c.status == "skipped" for c in items)
        counts = f"{len(items) - bad - skipped} passed" + (f", {bad} failed" if bad else "") \
            + (f", {skipped} skipped" if skipped else "")
        rows = "".join(
            f'<tr data-status="{c.status}"><td class="st">{ICONS[c.status]}</td><td>{esc(c.name)}'
            + (f'<span class="msg">{esc(c.message[:300])}</span>' if c.status == "failed" and c.message else "")
            + f'</td><td class="t">{c.seconds:.2f} s</td></tr>' for c in items)
        suites.append(
            f'<details class="suite"{" open" if bad else ""}><summary>'
            f'<span class="layer">{esc(LAYERS[layer][0])}</span><span class="group">{esc(group)}</span>'
            f'<span class="counts">{ICONS["failed"] if bad else ICONS["passed"]} {counts}</span>'
            f'</summary><table>{rows}</table></details>')

    run = ""
    if meta.get("sha"):
        commit = f'<a href="{esc(meta["repo_url"])}/commit/{esc(meta["sha"])}">{esc(meta["sha"][:7])}</a>'
        run_link = (f' · <a href="{esc(meta["run_url"])}">CI run</a>' if meta.get("run_url") else "")
        run = f"Commit {commit} on {esc(meta.get('ref', ''))}{run_link} · "
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Dashboard</title>
<style>{CSS}</style></head>
<body><main>
<header>
  <h1>OTA Update Fleet Manager · Tests</h1>
  <p>{run}updated {generated}</p>
  {status}
</header>
<div class="tiles">{tiles_html}</div>
<section>
  <h2>Tests by layer</h2>
  <p>From many small, fast tests to a few that run the whole system. Every layer runs on every pull request.</p>
  <div class="bars">{"".join(layer_rows)}</div>
</section>
<section>
  <h2>Code coverage by component</h2>
  <p>Share of each component's code lines that the unit and component tests run. The end-to-end
  demo runs on top of this, so the orchestrator's MQTT wiring and command-line tool are tested there.</p>
  {cov_html}
</section>
<section>
  <h2>Every test</h2>
  <p>Grouped by test file, and by demo step for the end-to-end checks.</p>
  <div class="filters" role="group" aria-label="Show">
    <button data-filter="all" aria-pressed="true">All ({total})</button>
    <button data-filter="passed" aria-pressed="false">Passed ({by_status['passed']})</button>
    <button data-filter="failed" aria-pressed="false">Failed ({failed})</button>
    <button data-filter="skipped" aria-pressed="false">Skipped ({by_status['skipped']})</button>
  </div>
  {"".join(suites) or "<p>No test results in this run.</p>"}
</section>
<footer>Built by <code>tools/test_dashboard.py</code> from the CI run's JUnit and coverage reports.
{f'<a href="{esc(meta["repo_url"])}">Repository</a>' if meta.get("repo_url") else ""}</footer>
</main><script>{JS}</script></body></html>
"""


# --------------------------------------------------------------------------- main

def read_floor(path: Path) -> int | None:
    try:
        m = re.search(r"^fail_under\s*=\s*(\d+)", path.read_text(), re.MULTILINE)
    except OSError:
        return None
    return int(m.group(1)) if m else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--junit", action="append", default=[], type=Path, help="JUnit XML (repeatable)")
    ap.add_argument("--coverage", type=Path, help="coverage.py XML report")
    ap.add_argument("--htmlcov", type=Path, help="coverage.py HTML report folder")
    ap.add_argument("--coveragerc", type=Path, default=Path(".coveragerc"))
    ap.add_argument("--out", type=Path, default=Path("site"))
    args = ap.parse_args(argv)

    cases = []
    for path in args.junit:
        if path.is_file():
            cases += read_junit(path)
        else:
            print(f"warning: {path} not found; skipped", file=sys.stderr)
    coverage = read_coverage(args.coverage) if args.coverage and args.coverage.is_file() else None

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "badges").mkdir(exist_ok=True)
    htmlcov = bool(args.htmlcov and args.htmlcov.is_dir())
    if htmlcov:
        shutil.copytree(args.htmlcov, args.out / "coverage", dirs_exist_ok=True)

    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    meta = {"htmlcov": htmlcov, "sha": os.environ.get("GITHUB_SHA", ""),
            "ref": os.environ.get("GITHUB_REF_NAME", ""),
            "repo_url": f"{server}/{repo}" if repo else "",
            "run_url": (f"{server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                        if repo and os.environ.get("GITHUB_RUN_ID") else "")}
    floor = read_floor(args.coveragerc)
    (args.out / "index.html").write_text(build_page(cases, coverage, floor, meta))

    failed = sum(c.status == "failed" for c in cases)
    passed = sum(c.status == "passed" for c in cases)
    e2e = [c for c in cases if c.layer == "e2e"]
    e2e_ok = sum(c.status == "passed" for c in e2e)
    badges = {
        "tests": badge("tests", f"{failed} failed" if failed else f"{passed} passed" if cases else "no data",
                       BAD if failed else GOOD if cases else NONE),
        "coverage": badge("coverage", pct(coverage[0]) if coverage else "no data",
                          NONE if not coverage else GOOD if not floor or coverage[0] * 100 >= floor + 5
                          else WARN if coverage[0] * 100 >= floor else BAD),
        "e2e": badge("end-to-end", f"{e2e_ok}/{len(e2e)} checks" if e2e else "no data",
                     NONE if not e2e else GOOD if e2e_ok == len(e2e) else BAD),
    }
    for name, svg in badges.items():
        (args.out / "badges" / f"{name}.svg").write_text(svg)
    print(f"Dashboard: {args.out / 'index.html'} ({len(cases)} tests, {failed} failed, "
          f"coverage {pct(coverage[0]) if coverage else 'n/a'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
