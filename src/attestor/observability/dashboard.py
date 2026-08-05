"""A static page built from the run records. No server, no build step, no JavaScript.

The audience is not an engineer. It is whoever has to decide whether to file, and the two
questions they have are *can we issue?* and *what are we admitting to?* Those are answered
above the fold or the page has failed.

Everything else follows from that:

**Blockers are red and first.** A run that cannot issue is a run whose most important fact is
the reason. Burying it under a chart of successful datapoints would be a design that flatters
the system rather than informing the reader.

**Accepted defects show their signatures and their expiry.** "Somebody approved it" is not
an answer to an auditor; a name and a date are. The dashboard shows what the report shows,
because a dashboard that is softer than the filing is worse than no dashboard.

**Nothing is fetched.** One HTML file, inline CSS, self-contained. It opens from a folder, it
survives being emailed, and it works after the estate is destroyed — which is exactly when
somebody will want to look at it.
"""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from attestor.observability.run_record import OmissionRecord, RunRecord

#: Days before an acceptance lapses at which the page starts warning. Matched to
#: `scripts/check_overrides.py --warn-days`: two surfaces disagreeing about when something
#: is "about to expire" is how one of them stops being believed.
WARN_WITHIN_DAYS = 30

_STYLE = """
:root {
  --ink: #14181d; --muted: #5b6672; --line: #dfe4ea; --panel: #ffffff; --bg: #f5f7f9;
  --ok: #1a7f4b; --warn: #a06a00; --bad: #b3261e; --accent: #2f4858;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e8ecf1; --muted: #9aa5b1; --line: #2b323b; --panel: #171c22; --bg: #0f1317;
    --ok: #4cc38a; --warn: #e0a340; --bad: #f2635a; --accent: #9db4c0;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 { font-size: 1.05rem; margin: 2.25rem 0 .75rem; }
h3 { font-size: .95rem; margin: 0 0 .5rem; }
.sub { color: var(--muted); margin: 0 0 2rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: .9rem; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 1rem 1.1rem;
}
.card .who { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; }
.card .verdict { font-size: 1.25rem; font-weight: 650; margin: .35rem 0 .1rem; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
.counts { color: var(--muted); font-size: .85rem; }
table { border-collapse: collapse; width: 100%; background: var(--panel); font-size: .875rem; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }
th, td { text-align: left; padding: .55rem .75rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; }
tr:last-child td { border-bottom: 0; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85em; }
.pill {
  display: inline-block; padding: .1rem .45rem; border-radius: 5px; font-size: .75rem;
  border: 1px solid var(--line); color: var(--muted); white-space: nowrap;
}
.pill.bad { border-color: var(--bad); color: var(--bad); }
.pill.warn { border-color: var(--warn); color: var(--warn); }
.note { color: var(--muted); font-size: .85rem; margin: .5rem 0 0; }
footer { margin-top: 3rem; color: var(--muted); font-size: .8rem; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else "—"))


def _verdict(record: RunRecord) -> tuple[str, str]:
    if not record.issued:
        return "bad", "cannot issue"
    if record.limitations:
        return "warn", "issued with limitations"
    return "ok", "issued"


def _cards(records: tuple[RunRecord, ...]) -> str:
    cards = []
    for record in records:
        tone, verdict = _verdict(record)
        cards.append(
            f'<div class="card"><div class="who">{_esc(record.tenant_name)} · '
            f"{_esc(record.standard)} · {_esc(record.period)}</div>"
            f'<div class="verdict {tone}">{_esc(verdict)}</div>'
            f'<div class="counts">{len(record.published)} disclosed · '
            f"{len(record.limitations)} limitation(s) · {len(record.blockers)} blocker(s)</div>"
            "</div>"
        )
    return f'<div class="cards">{"".join(cards)}</div>'


def _blockers(records: tuple[RunRecord, ...]) -> str:
    rows = [
        (record, entry)
        for record in records
        for entry in sorted(record.blockers, key=lambda e: e.datapoint_id)
    ]
    if not rows:
        return (
            "<h2>Blockers</h2><p class='note'>None. Every tenant's report could be issued "
            "from the evidence on file.</p>"
        )
    body = "".join(
        f"<tr><td>{_esc(record.tenant)}</td><td class='mono'>{_esc(entry.datapoint_id)}</td>"
        f"<td>{_esc(entry.reference)}</td>"
        f"<td><span class='pill bad'>{_esc(entry.reason_code)}</span></td>"
        f"<td>{_esc(entry.detail)}</td></tr>"
        for record, entry in rows
    )
    return (
        f"<h2>Blockers <span class='pill bad'>{len(rows)}</span></h2>"
        "<div class='scroll'><table><thead><tr><th>tenant</th><th>datapoint</th>"
        "<th>clause</th><th>reason</th><th>what happened</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
        "<p class='note'>A blocked report produces no artefact at all — not a draft, not a "
        "watermarked copy. Lifting a block requires a signed, expiring override, and "
        "<code>E_RESOLVER_ERROR</code> cannot be lifted by anyone.</p>"
    )


def _limitations(records: tuple[RunRecord, ...], *, today: dt.date) -> str:
    rows: list[tuple[RunRecord, OmissionRecord]] = [
        (record, entry)
        for record in records
        for entry in sorted(record.limitations, key=lambda e: e.datapoint_id)
    ]
    if not rows:
        return "<h2>Declared limitations</h2><p class='note'>None recorded.</p>"

    body = []
    for record, entry in rows:
        expiry = ""
        if entry.override_expires_on:
            remaining = (entry.override_expires_on - today).days
            tone = "bad" if remaining < 0 else "warn" if remaining <= WARN_WITHIN_DAYS else ""
            label = (
                f"expired {entry.override_expires_on}"
                if remaining < 0
                else f"expires {entry.override_expires_on} ({remaining}d)"
            )
            expiry = f"<span class='pill {tone}'>{_esc(label)}</span>"
        kind = "lawful omission" if entry.lawful else "accepted defect"
        body.append(
            f"<tr><td>{_esc(record.tenant)}</td><td class='mono'>{_esc(entry.datapoint_id)}</td>"
            f"<td><span class='pill'>{_esc(entry.reason_code)}</span></td>"
            f"<td>{_esc(kind)}</td>"
            f"<td>{_esc(', '.join(entry.approvers)) if entry.approvers else '—'}</td>"
            f"<td>{expiry or '—'}</td></tr>"
        )
    return (
        f"<h2>Declared limitations <span class='pill warn'>{len(rows)}</span></h2>"
        "<div class='scroll'><table><thead><tr><th>tenant</th><th>datapoint</th><th>reason</th>"
        "<th>kind</th><th>approved by</th><th>lapses</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
        "<p class='note'>An accepted defect is not a resolved one. The reason code is "
        "unchanged by the acceptance, and on the expiry date the finding returns and the "
        "build goes red again.</p>"
    )


def _figures(records: tuple[RunRecord, ...]) -> str:
    sections = []
    for record in records:
        if not record.published:
            continue
        body = "".join(
            f"<tr><td class='mono'>{_esc(entry.datapoint_id)}</td>"
            f"<td>{_esc(entry.reference)}</td>"
            f"<td class='num'>{_esc(entry.value) if entry.value else 'narrative'}</td>"
            f"<td>{_esc(entry.unit)}</td>"
            f"<td><span class='pill'>{_esc(entry.resolver_kind)}</span></td>"
            f"<td class='mono'>{_esc(entry.lineage_id)}</td>"
            f"<td class='mono'>{_esc(', '.join(entry.sources)) if entry.sources else '—'}</td></tr>"
            for entry in record.published
        )
        sections.append(
            f"<h3>{_esc(record.tenant_name)} — {_esc(record.standard)} {_esc(record.period)}</h3>"
            "<div class='scroll'><table><thead><tr><th>datapoint</th><th>clause</th>"
            "<th>value</th><th>unit</th><th>resolver</th><th>lineage</th><th>read from</th>"
            f"</tr></thead><tbody>{body}</tbody></table></div>"
        )
    return "<h2>Disclosed figures</h2>" + "".join(sections)


def _gate_pill(clean: bool) -> str:
    return "<span class='pill'>clean</span>" if clean else "<span class='pill bad'>FAILED</span>"


def _artefacts(records: tuple[RunRecord, ...]) -> str:
    rows = [(r, a) for r in records for a in r.artefacts]
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{_esc(record.tenant)}</td><td class='mono'>{_esc(artefact.path)}</td>"
        f"<td>{_esc(artefact.artefact)}</td>"
        f"<td class='num'>{artefact.numerals_checked}</td>"
        f"<td>{'<span class=pill>clean</span>' if artefact.provenance_clean else '<span class="pill bad">FAILED</span>'}</td>"
        f"<td class='mono'>{_esc(artefact.sha256[:16])}</td></tr>"
        for record, artefact in rows
    )
    return (
        "<h2>Artefacts</h2><div class='scroll'><table><thead><tr><th>tenant</th><th>file</th>"
        "<th>format</th><th>numerals checked</th><th>provenance gate</th><th>sha256</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
        "<p class='note'>Every numeral in each file was checked against the render manifest. "
        "A digit that no resolver produced and no reviewed template contained fails the "
        "build.</p>"
    )


def render(records: tuple[RunRecord, ...], *, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    generated = max((r.finished_at or r.started_at for r in records), default=None)
    subtitle = (
        f"{len(records)} run(s) · latest {generated.date() if generated else '—'}"
        if records
        else "No runs recorded yet."
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attestor — report runs</title>
<style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>Attestor — report runs</h1>
<p class="sub">{_esc(subtitle)}</p>
{_cards(records)}
{_blockers(records)}
{_limitations(records, today=today)}
{_figures(records)}
{_artefacts(records)}
<footer>Generated from the committed run records by <code>attestor dashboard</code>.
Self-contained: no network, no scripts. It still opens after the estate is destroyed, which
is when somebody usually wants it.</footer>
</div></body></html>
"""


def write(records: tuple[RunRecord, ...], path: Path, *, today: dt.date | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(records, today=today), encoding="utf-8")
    return path
