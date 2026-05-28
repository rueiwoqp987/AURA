import html
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from utils.storage import SQLiteStorage


def _stable_link_id(*parts) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return "aac_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]


def _read_audit_jsonl(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    records = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def import_audit_jsonl_to_db(audit_path, db_path) -> int:
    """Import the raw JSONL audit ledger into normalized audit_events rows."""
    audit_file = Path(audit_path)
    db_file = Path(db_path)
    records = _read_audit_jsonl(audit_file)
    if not records:
        SQLiteStorage(db_file).close()
        return 0

    storage = SQLiteStorage(db_file)
    storage.close()

    conn = sqlite3.connect(db_file)
    try:
        cur = conn.cursor()
        rows = []
        for record in records:
            run_id = record.get("run_id") or db_file.parent.name
            seq = record.get("seq")
            if not isinstance(seq, int):
                continue
            rows.append(
                (
                    run_id,
                    seq,
                    record.get("ts"),
                    record.get("app"),
                    record.get("phase"),
                    record.get("account"),
                    record.get("chat_id"),
                    record.get("source_class"),
                    record.get("source_func"),
                    record.get("action"),
                    record.get("selector"),
                    record.get("result"),
                    record.get("error"),
                    json.dumps(record.get("artifacts") or [], ensure_ascii=False),
                    record.get("side_effect_hint"),
                )
            )
        cur.executemany(
            """
            INSERT OR REPLACE INTO audit_events (
                run_id, seq, ts, app, phase, account, chat_id, source_class, source_func,
                action, selector, result, error, artifacts_json, side_effect_hint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        _backfill_artifact_action_context_links(cur, records, db_file.parent.name)
        _dedupe_artifact_action_context_links(cur)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _parse_artifact_annotations(artifacts) -> dict:
    parsed = {}
    for item in artifacts or []:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            parsed[key] = value
    return parsed


def _backfill_artifact_action_context_links(cur, records: list[dict], fallback_run_id: str) -> None:
    for record in records:
        if record.get("action") != "artifact_context_registered":
            continue
        artifacts = record.get("artifacts") or []
        annotations = _parse_artifact_annotations(artifacts)
        artifact_path = annotations.get("path")
        if not artifact_path:
            continue
        run_id = record.get("run_id") or fallback_run_id
        cur.execute(
            """
            SELECT artifact_id, context_id, chatroom_id, observed_chat_id, message_pk, record_id, message_id, observation_id
            FROM artifacts
            WHERE run_id = ? AND artifact_path = ?
            """,
            (run_id, artifact_path),
        )
        row = cur.fetchone()
        if not row:
            continue
        artifact_id, context_id, chatroom_id, observed_chat_id, message_pk, record_id, message_id, observation_id = row
        source_action = annotations.get("source_action") or None
        source_screen = annotations.get("source_screen") or None
        cur.execute(
            """
            SELECT link_id
            FROM artifact_action_context_links
            WHERE run_id = ?
              AND artifact_id = ?
              AND COALESCE(context_id, '') = COALESCE(?, '')
              AND COALESCE(chatroom_id, '') = COALESCE(?, '')
              AND COALESCE(message_pk, '') = COALESCE(?, '')
              AND COALESCE(observation_id, '') = COALESCE(?, '')
              AND COALESCE(source_action, '') = COALESCE(?, '')
              AND COALESCE(source_screen, '') = COALESCE(?, '')
            LIMIT 1
            """,
            (run_id, artifact_id, context_id, chatroom_id, message_pk, observation_id, source_action, source_screen),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE artifact_action_context_links
                SET audit_seq = ?, record_id = COALESCE(record_id, ?), message_id = COALESCE(message_id, ?), created_ts = ?
                WHERE link_id = ?
                """,
                (record.get("seq"), record_id, message_id, time.time(), existing[0]),
            )
            continue
        link_id = _stable_link_id(run_id, artifact_id, record.get("seq"), context_id, chatroom_id, message_pk, observation_id)
        cur.execute(
            """
            INSERT OR REPLACE INTO artifact_action_context_links (
                link_id, run_id, artifact_id, audit_seq, context_id, chatroom_id, observed_chat_id,
                message_pk, record_id, message_id, observation_id, link_type, source_action,
                source_screen, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link_id,
                run_id,
                artifact_id,
                record.get("seq"),
                context_id,
                chatroom_id,
                observed_chat_id,
                message_pk,
                record_id,
                message_id,
                observation_id,
                "artifact--action--context",
                source_action,
                source_screen,
                time.time(),
            ),
        )


def _dedupe_artifact_action_context_links(cur) -> None:
    cur.execute(
        """
        SELECT rowid, run_id, artifact_id, context_id, chatroom_id, message_pk, observation_id,
               source_action, source_screen, audit_seq
        FROM artifact_action_context_links
        ORDER BY rowid
        """
    )
    keep_by_key = {}
    delete_rowids = []
    for row in cur.fetchall():
        (
            rowid,
            run_id,
            artifact_id,
            context_id,
            chatroom_id,
            message_pk,
            observation_id,
            source_action,
            source_screen,
            audit_seq,
        ) = row
        key = (
            run_id or "",
            artifact_id or "",
            context_id or "",
            chatroom_id or "",
            message_pk or "",
            observation_id or "",
            source_action or "",
            source_screen or "",
        )
        existing = keep_by_key.get(key)
        if existing is None:
            keep_by_key[key] = (rowid, audit_seq)
            continue
        existing_rowid, existing_audit_seq = existing
        if existing_audit_seq is None and audit_seq is not None:
            delete_rowids.append(existing_rowid)
            keep_by_key[key] = (rowid, audit_seq)
        else:
            delete_rowids.append(rowid)

    if delete_rowids:
        cur.executemany(
            "DELETE FROM artifact_action_context_links WHERE rowid = ?",
            [(rowid,) for rowid in delete_rowids],
        )


def _fetch_audit_events(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT run_id, seq, ts, app, phase, account, chat_id, action, selector, result, error, artifacts_json
            FROM audit_events
            ORDER BY seq
            """
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _event_artifacts(event: dict) -> list:
    raw = event.get("artifacts_json")
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _is_success_result(result: str | None) -> bool:
    return (result or "") in ("", "success", "confirmed", "stable", "changed")


def _is_benign_non_success(event: dict) -> bool:
    action = event.get("action") or ""
    result = event.get("result") or ""
    benign_pairs = {
        ("screen_settled", "timeout"),
        ("bluetooth_pairing_dialog", "timeout"),
        ("screen_transition", "already_open"),
        ("list_state", "unchanged_timeout"),
    }
    return (action, result) in benign_pairs


def _recovery_phase_for_event(event: dict) -> str | None:
    action = event.get("action") or ""
    result = event.get("result") or ""
    phase = event.get("phase") or ""
    if not phase or not _is_success_result(result):
        return None
    if action == "bluetooth_connected":
        return phase
    if action == "bluetooth_preconnect_end":
        if any(str(item) == "failures=0" for item in _event_artifacts(event)):
            return phase
    return None


def _build_recovery_phases_after(events: list[dict]) -> list[set[str]]:
    recovery_phases: set[str] = set()
    recovery_after: list[set[str]] = [set() for _ in events]
    for index in range(len(events) - 1, -1, -1):
        recovery_after[index] = set(recovery_phases)
        phase = _recovery_phase_for_event(events[index])
        if phase:
            recovery_phases.add(phase)
    return recovery_after


def _is_recovered_non_success(event: dict, recovery_phases_after: set[str]) -> bool:
    action = event.get("action") or ""
    selector = event.get("selector") or ""
    phase = event.get("phase") or ""
    if "bluetooth" not in f"{action} {selector}".lower():
        return False

    recoverable_actions = {"screen_state", "click", "bluetooth_connect_settle"}
    if action not in recoverable_actions:
        return False

    return phase in recovery_phases_after


def build_audit_review_outputs(db_path, review_json_path, review_html_path) -> dict:
    """Build human-oriented audit review JSON and HTML from audit_events."""
    db_file = Path(db_path)
    json_path = Path(review_json_path)
    html_path = Path(review_html_path)
    events = _fetch_audit_events(db_file)

    run_id = events[0]["run_id"] if events else db_file.parent.name
    phases = defaultdict(lambda: {"event_count": 0, "actions": Counter(), "failures": 0, "recovered_events": 0, "benign_events": 0})
    actions = Counter()
    failures = []
    recovered_events = []
    benign_events = []
    key_events = []
    artifact_groups = Counter()
    artifact_samples = {}
    recovery_phases_after = _build_recovery_phases_after(events)

    for index, event in enumerate(events):
        phase = event.get("phase") or "unscoped"
        action = event.get("action") or "unknown"
        result = event.get("result") or ""
        phases[phase]["event_count"] += 1
        phases[phase]["actions"][action] += 1
        actions[action] += 1
        if (not _is_success_result(result)) and _is_benign_non_success(event):
            phases[phase]["benign_events"] += 1
            benign_events.append(
                {
                    "seq": event.get("seq"),
                    "phase": phase,
                    "action": action,
                    "selector": event.get("selector"),
                    "result": result,
                    "error": event.get("error"),
                }
            )
        elif (not _is_success_result(result)) and _is_recovered_non_success(event, recovery_phases_after[index]):
            phases[phase]["recovered_events"] += 1
            recovered_events.append(
                {
                    "seq": event.get("seq"),
                    "phase": phase,
                    "action": action,
                    "selector": event.get("selector"),
                    "result": result,
                    "error": event.get("error"),
                }
            )
        elif not _is_success_result(result):
            phases[phase]["failures"] += 1
            failures.append(
                {
                    "seq": event.get("seq"),
                    "phase": phase,
                    "action": action,
                    "selector": event.get("selector"),
                    "result": result,
                    "error": event.get("error"),
                }
            )
        if action in {
            "run_start",
            "orchestrator_start",
            "method_start",
            "method_end",
            "artifact_context_registered",
            "packaging_start",
            "orchestrator_end",
        }:
            key_events.append(
                {
                    "seq": event.get("seq"),
                    "phase": phase,
                    "action": action,
                    "selector": event.get("selector"),
                    "result": result,
                    "artifacts": _event_artifacts(event)[:12],
                }
            )
        if action == "artifact_context_registered":
            annotations = _parse_artifact_annotations(_event_artifacts(event))
            key = (
                phase,
                annotations.get("kind") or event.get("selector") or "artifact",
                annotations.get("source_action") or "unknown",
                annotations.get("source_screen") or "unknown",
            )
            artifact_groups[key] += 1
            artifact_samples.setdefault(key, annotations.get("path") or "")

    review = {
        "review_schema_version": "v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_id": run_id,
        "event_count": len(events),
        "actions": dict(sorted(actions.items())),
        "phases": {
            phase: {
                "event_count": payload["event_count"],
                "failures": payload["failures"],
                "recovered_events": payload["recovered_events"],
                "benign_events": payload["benign_events"],
                "actions": dict(sorted(payload["actions"].items())),
            }
            for phase, payload in sorted(phases.items())
        },
        "failures": failures,
        "recovered_events": recovered_events,
        "benign_events": benign_events,
        "key_events": key_events,
        "artifact_summary": [
            {
                "phase": phase,
                "kind": kind,
                "source_action": source_action,
                "source_screen": source_screen,
                "count": count,
                "sample_path": artifact_samples.get((phase, kind, source_action, source_screen), ""),
            }
            for (phase, kind, source_action, source_screen), count in sorted(
                artifact_groups.items(),
                key=lambda item: (item[0][0], item[0][1], item[0][2], item[0][3]),
            )
        ],
    }

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8-sig")
    html_path.write_text(_render_review_html(review), encoding="utf-8")
    return review


def _render_review_html(review: dict) -> str:
    def esc(value) -> str:
        return html.escape("" if value is None else str(value))

    def json_details(summary: str, payload) -> str:
        raw = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
        return f"<details><summary>{esc(summary)}</summary><pre><code>{esc(raw)}</code></pre></details>"

    def action_chips(actions: dict, limit: int = 14) -> str:
        if not actions:
            return "<span class='muted'>none</span>"
        sorted_actions = sorted(actions.items(), key=lambda item: (-item[1], item[0]))
        shown = sorted_actions[:limit]
        hidden = sorted_actions[limit:]
        rendered = " ".join(
            f"<span class='chip'>{esc(action)} <span class='count'>{esc(count)}</span></span>"
            for action, count in shown
        )
        if hidden:
            rendered += f" <span class='muted'>+{len(hidden)} more</span>"
        return rendered

    def artifact_preview(artifacts: list) -> str:
        if not artifacts:
            return "<span class='muted'>none</span>"
        preview = artifacts[:3]
        extra = len(artifacts) - len(preview)
        rendered = " ".join(f"<span class='chip'>{esc(item)}</span>" for item in preview)
        if extra > 0:
            rendered += f" <span class='muted'>+{extra} more</span>"
        return rendered

    phase_rows = "\n".join(
        "<tr>"
        f"<td>{esc(phase)}</td>"
        f"<td>{payload['event_count']}</td>"
        f"<td>{payload['failures']}</td>"
        f"<td>{payload.get('recovered_events', 0)}</td>"
        f"<td>{payload.get('benign_events', 0)}</td>"
        f"<td>{action_chips(payload.get('actions') or {})}{json_details('Raw phase JSON', {'phase': phase, **payload})}</td>"
        "</tr>"
        for phase, payload in review.get("phases", {}).items()
    )
    event_rows = "\n".join(
        "<tr>"
        f"<td>{esc(event.get('seq'))}</td>"
        f"<td>{esc(event.get('phase'))}</td>"
        f"<td>{esc(event.get('action'))}</td>"
        f"<td>{esc(event.get('selector'))}</td>"
        f"<td>{esc(event.get('result'))}</td>"
        f"<td>{artifact_preview(event.get('artifacts') or [])}{json_details('Raw event JSON', event)}</td>"
        "</tr>"
        for event in review.get("key_events", [])
    )
    artifact_rows = "\n".join(
        "<tr>"
        f"<td>{esc(item.get('phase'))}</td>"
        f"<td>{esc(item.get('kind'))}</td>"
        f"<td>{esc(item.get('source_action'))}</td>"
        f"<td>{esc(item.get('source_screen'))}</td>"
        f"<td>{esc(item.get('count'))}</td>"
        f"<td><code>{esc(item.get('sample_path'))}</code>{json_details('Raw artifact group JSON', item)}</td>"
        "</tr>"
        for item in review.get("artifact_summary", [])
    ) or "<tr><td colspan='6'>No artifact context records found.</td></tr>"
    failure_rows = "\n".join(
        "<tr>"
        f"<td>{esc(event.get('seq'))}</td>"
        f"<td>{esc(event.get('phase'))}</td>"
        f"<td>{esc(event.get('action'))}</td>"
        f"<td>{esc(event.get('selector'))}</td>"
        f"<td>{esc(event.get('result'))}</td>"
        f"<td>{esc(event.get('error'))}{json_details('Raw failure JSON', event)}</td>"
        "</tr>"
        for event in review.get("failures", [])
    ) or "<tr><td colspan='6'>No failures recorded.</td></tr>"
    recovered_rows = "\n".join(
        "<tr>"
        f"<td>{esc(event.get('seq'))}</td>"
        f"<td>{esc(event.get('phase'))}</td>"
        f"<td>{esc(event.get('action'))}</td>"
        f"<td>{esc(event.get('selector'))}</td>"
        f"<td>{esc(event.get('result'))}</td>"
        f"<td>{json_details('Raw recovered event JSON', event)}</td>"
        "</tr>"
        for event in review.get("recovered_events", [])
    ) or "<tr><td colspan='6'>No recovered non-success outcomes recorded.</td></tr>"
    benign_rows = "\n".join(
        "<tr>"
        f"<td>{esc(event.get('seq'))}</td>"
        f"<td>{esc(event.get('phase'))}</td>"
        f"<td>{esc(event.get('action'))}</td>"
        f"<td>{esc(event.get('selector'))}</td>"
        f"<td>{esc(event.get('result'))}</td>"
        f"<td>{json_details('Raw benign event JSON', event)}</td>"
        "</tr>"
        for event in review.get("benign_events", [])
    ) or "<tr><td colspan='6'>No benign non-success outcomes recorded.</td></tr>"
    review_guide = """
  <details class="review-guide">
    <summary><span class="guide-title">AURA Review Guide</span><span class="guide-subtitle">How to read this report</span></summary>
    <div class="guide-grid">
      <section class="guide-card">
        <h3>Phase Summary</h3>
        <p>Start here to compare phases. <strong>Attention Required</strong> means unresolved non-success, <strong>Recovered</strong> means a later success marker closed the issue, and <strong>Benign</strong> means expected optional waits or already-open states.</p>
      </section>
      <section class="guide-card">
        <h3>Artifact Summary</h3>
        <p>Use kind, source action, and source screen to understand how each evidence group was produced. This is the primary artifact--action--context linkage overview.</p>
      </section>
      <section class="guide-card">
        <h3>Key Events</h3>
        <p>Review run, method, package, and artifact registration milestones without reading every audit row.</p>
      </section>
      <section class="guide-card">
        <h3>Attention Required</h3>
        <p>Inspect these first. They are non-success outcomes that did not have a known recovery or benign classification.</p>
      </section>
      <section class="guide-card">
        <h3>Recovered Non-Success</h3>
        <p>These events looked unsuccessful at the primitive level but were followed by a successful recovery or phase-level completion marker.</p>
      </section>
      <section class="guide-card">
        <h3>Benign Non-Success</h3>
        <p>These are expected non-critical outcomes such as optional visual settle timeouts, already-open screens, or unchanged list checks.</p>
      </section>
      <section class="guide-card wide">
        <h3>Raw JSON Details</h3>
        <p>Every summary row keeps the original payload in folded JSON details. Expand these when you need the exact audit values used to build the report.</p>
      </section>
    </div>
  </details>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AURA Review Report - {esc(review.get('run_id'))}</title>
  <style>
    body {{ font-family: Cambria, Georgia, serif; margin: 32px; color: #17201a; background: #f8f4ec; }}
    h1, h2 {{ font-family: Bahnschrift, Segoe UI, sans-serif; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0 28px; background: #fffdf8; }}
    th, td {{ border: 1px solid #d8cdbb; padding: 8px 10px; vertical-align: top; }}
    th {{ background: #24382f; color: #fff; text-align: left; }}
    .metric {{ display: inline-block; padding: 10px 14px; background: #e5dbc8; margin-right: 8px; border-radius: 8px; }}
    .chip {{ display: inline-block; margin: 2px 4px 2px 0; padding: 3px 7px; border-radius: 999px; background: #e9efe7; border: 1px solid #cbd8c8; font-family: Bahnschrift, Segoe UI, sans-serif; font-size: 12px; }}
    .count {{ font-weight: 700; color: #284d38; }}
    .muted {{ color: #69736c; }}
    .review-guide {{ margin: 18px 0 24px; border: 1px solid #bdc8b6; border-radius: 16px; background: linear-gradient(135deg, #fffdf8 0%, #edf3e8 100%); box-shadow: 0 12px 28px rgba(36, 56, 47, 0.10); overflow: hidden; }}
    .review-guide > summary {{ list-style: none; padding: 18px 22px; background: #24382f; color: #f7f0e3; display: flex; align-items: baseline; gap: 12px; }}
    .review-guide > summary::-webkit-details-marker {{ display: none; }}
    .review-guide > summary::after {{ content: "expand"; margin-left: auto; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #cbd8c8; }}
    .review-guide[open] > summary::after {{ content: "collapse"; }}
    .guide-title {{ font-family: Bahnschrift, Segoe UI, sans-serif; font-size: 18px; font-weight: 700; }}
    .guide-subtitle {{ color: #d8cdbb; font-size: 13px; }}
    .guide-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; padding: 16px; }}
    .guide-card {{ border: 1px solid #d8cdbb; border-radius: 12px; background: rgba(255, 253, 248, 0.92); padding: 14px; }}
    .guide-card.wide {{ grid-column: 1 / -1; }}
    .guide-card h3 {{ margin: 0 0 8px; font-family: Bahnschrift, Segoe UI, sans-serif; color: #24382f; }}
    .guide-card p {{ margin: 0; line-height: 1.45; color: #415045; }}
    code {{ background: #efe6d6; padding: 1px 4px; border-radius: 4px; }}
    details {{ margin-top: 8px; }}
    summary {{ cursor: pointer; color: #385444; font-family: Bahnschrift, Segoe UI, sans-serif; font-size: 12px; }}
    pre {{ max-height: 260px; overflow: auto; background: #18221d; color: #f4f0e8; padding: 12px; border-radius: 8px; }}
    pre code {{ background: transparent; padding: 0; color: inherit; }}
  </style>
</head>
<body>
  <h1>AURA Review Report</h1>
  <p><span class="metric">Run: <code>{esc(review.get('run_id'))}</code></span><span class="metric">Events: {esc(review.get('event_count'))}</span></p>
{review_guide}
  <h2>Phase Summary</h2>
  <table><thead><tr><th>Phase</th><th>Events</th><th>Attention Required</th><th>Recovered Non-Success</th><th>Benign Non-Success</th><th>Actions</th></tr></thead><tbody>{phase_rows}</tbody></table>
  <h2>Artifact Summary</h2>
  <table><thead><tr><th>Phase</th><th>Kind</th><th>Source Action</th><th>Source Screen</th><th>Count</th><th>Sample</th></tr></thead><tbody>{artifact_rows}</tbody></table>
  <h2>Key Events</h2>
  <table><thead><tr><th>Seq</th><th>Phase</th><th>Action</th><th>Selector</th><th>Result</th><th>Artifacts</th></tr></thead><tbody>{event_rows}</tbody></table>
  <h2>Attention Required</h2>
  <table><thead><tr><th>Seq</th><th>Phase</th><th>Action</th><th>Selector</th><th>Result</th><th>Error</th></tr></thead><tbody>{failure_rows}</tbody></table>
  <h2>Recovered Non-Success Outcomes</h2>
  <table><thead><tr><th>Seq</th><th>Phase</th><th>Action</th><th>Selector</th><th>Result</th><th>Details</th></tr></thead><tbody>{recovered_rows}</tbody></table>
  <h2>Benign Non-Success Outcomes</h2>
  <table><thead><tr><th>Seq</th><th>Phase</th><th>Action</th><th>Selector</th><th>Result</th><th>Details</th></tr></thead><tbody>{benign_rows}</tbody></table>
</body>
</html>
"""
