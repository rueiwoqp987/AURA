# AURA Implementation Details

Last updated: 2026-06-04

This document describes the current implementation state of AURA after the collector refactor, normalized database work, audit review generation, profile-driven system UI handling, WhatsApp Bluetooth export stabilization, Telegram attachment identity updates, and WeChat OCR termination improvements.

It is written as a development handoff document for another engineer or review assistant. It intentionally avoids personal data, device identifiers, host paths, chat names, phone numbers, and run-specific values. Example paths use placeholders such as `<RUN_ROOT>`, `<PHASE>`, `<CHAT_ID>`, and `<CHAT_NAME>`.

AURA now uses the term **acquisition-context linkage** for the relationship between collected artifacts, the UI or host actions that produced them, and the collection context in which those actions occurred. Some internal database/table/function names still use historical identifiers such as `artifact_action_context_links`; those are implementation identifiers, not the preferred paper terminology.

## 1. Project Summary

AURA is a Windows-hosted, Android UI-mediated mobile messenger acquisition research prototype.

It drives messenger applications through `uiautomator2`, captures app-visible data and supporting evidence, records an append-only audit ledger, stores normalized acquisition results in SQLite, and creates review-oriented JSON/HTML reports plus integrity manifests.

The acquisition primitives are general and profile-selected. The current AURA instantiation is:

| Target profile | Package | Configured primitive | Collector | Current acquisition route |
|---|---|---|---|---|
| Telegram | `org.telegram.messenger` | `S1` | `TelegramCollector` | UI hierarchy-mediated account/chat/message/attachment acquisition. Attachments are detected through device file snapshot diffs. |
| WhatsApp | `com.whatsapp` | `S2` | `WhatsAppCollector` | WhatsApp Export Chat through Android UI, share sheet, Windows Bluetooth receive, export parsing, and attachment mention validation. |
| WeChat | `com.tencent.mm` | `S3` | `WeChatCollector` | OCR-based chat list and chat history acquisition because reliable node-tree access is unavailable. |

Current design goals:

- Drive app-visible acquisition routes rather than hidden extraction paths.
- Keep acquisition actions visible, bounded, and reviewable.
- Record meaningful UI, host, acquisition, recovery, and failure events in `AURA_audit.log`.
- Preserve acquisition-context linkage through SQLite and review reports.
- Track attachment acquisition attempts, including reached UI targets, successful materialization, missing files, failed downloads, and ambiguous cases.
- Preserve integrity through SHA-256 hashing, size metadata, and archive manifests.
- Keep raw evidence, normalized records, audit events, and review summaries separate but linked.
- Prefer stable predicates, visual/state confirmation, and bounded loops over blind sleeps and unbounded traversal.

## 2. High-Level Architecture

Top-level files and directories:

| Path | Role |
|---|---|
| `main.py` | CLI entry point, profile loading, run directory creation, setup, orchestrator invocation, packaging, audit import, review generation, summary report generation. |
| `orchestrator.py` | Resolves target adapter and primitive engine, executes configured primitives, records method-level audit events. |
| `adapters/` | Target-to-collector adapters for Telegram, WhatsApp, and WeChat. |
| `engines/` | Thin primitive execution layer for `S1`, `S2`, and `S3`. |
| `platforms/base.py` | Base collector infrastructure: audit logging, artifact registration, screenshot/XML capture, hash finalization, storage lifecycle. |
| `collectors/common/ui_state.py` | Shared XML parsing, bounds, safe-area, click-target, and observation ID helpers. |
| `collectors/telegram/` | Telegram S1 implementation. |
| `collectors/whatsapp/` | WhatsApp S2 implementation, Bluetooth flow, export parser integration. |
| `collectors/wechat/` | WeChat S3 OCR implementation. |
| `utils/storage.py` | SQLite schema, normalized upsert operations, views, acquisition-context link storage, acquisition attempts. |
| `utils/audit_review.py` | Audit JSONL import, acquisition-context link backfill, review JSON, review HTML. |
| `utils/utils.py` | ADB/UI helpers, DND handling, Bluetooth host helpers, scrolling, setup/teardown support. |
| `utils/network_state.py` | Network/DND/airplane/Wi-Fi state snapshot and policy evaluation. |
| `utils/system_ui_profiles.py` | System UI profile loading and manufacturer/profile resolution. |
| `utils/device_info.py` | Device metadata collection. |
| `utils/evidence.py` | File hashing helper. |
| `profiles/apps/*.json` | Target-specific primitive, phase, stability, Bluetooth, and OCR configuration. |
| `profiles/system_ui/*.json` | Generic, Samsung, and Huawei system UI profiles for DND, airplane, Wi-Fi, recent apps, and Bluetooth differences. |
| `tests/` | Unit and source-level regression tests for audit linkage, phase transition, Bluetooth teardown, WhatsApp artifact metadata, Telegram identity, and WeChat termination. |

Runtime model:

```text
main.py
  -> parse CLI
  -> load app profile from profiles/apps/
  -> resolve system UI profile from profiles/system_ui/
  -> create run root
  -> initialize device and audit log
  -> AURAOrchestrator
      -> target adapter
      -> configured primitive engine
      -> collector.collect()
          -> phase preflight
          -> policy enforcement
          -> app-specific acquisition
          -> artifact registration
          -> message/contact/chatroom storage
          -> acquisition attempt storage
  -> finalize artifact hashes
  -> import audit JSONL into aura.db
  -> backfill acquisition-context links
  -> build review JSON and HTML
  -> package ZIP
  -> write summary report JSON
```

## 3. Runtime Entry Point

The primary entry point is `main.py`.

Typical commands:

```powershell
python main.py --target telegram --serial <ADB_SERIAL>
python main.py --target whatsapp --serial <ADB_SERIAL>
python main.py --target wechat --serial <ADB_SERIAL>
```

Important CLI options:

| Option | Current default | Meaning |
|---|---|---|
| `--target` | `telegram` | Selects `telegram`, `whatsapp`, or `wechat`. |
| `--serial` | not set | ADB serial. Omit only when exactly one device is connected. |
| `--runs-dir` | `runs` | Output root for run directories, ZIP bundles, and summary reports. |
| `--keep-run-dir` | disabled | Keeps the unpacked run directory after ZIP creation. Useful for review/debugging. |
| `--log-level` | `INFO` | Python logger level. `DEBUG` enables lower-level diagnostic logs. |

`main.py` creates:

```text
<OUTPUT_ROOT>/
  AURA_YYYYMMDD_HHMMSS/
    AURA_audit.log
    AURA_audit_review.json
    AURA_audit_timeline.html
    aura.db
    collection_timing.json
    device_info.json
    preflight_local-first.json
    preflight_controlled-online.json
    <target>/
      <phase>/
        ...
  AURA_YYYYMMDD_HHMMSS.zip
  AURA_YYYYMMDD_HHMMSS_report.json
```

Packaging runs in `finally`, so failed or partial runs still produce reviewable outputs when possible.

## 4. Profile Layout

Profiles are split into app acquisition profiles and system UI profiles.

```text
profiles/
  apps/
    Telegram.json
    WhatsApp.json
    WeChat.json
  system_ui/
    generic.json
    samsung.json
    huawei.json
```

Current app profile highlights:

| Profile | Configured primitive | Active phase plan | Notes |
|---|---|---|---|
| `profiles/apps/Telegram.json` | `S1` | `local-first` and `controlled-online` enabled | Includes attachment identity policy, snapshot directories, controlled-online Wi-Fi/app-sync settle, and chat scroll tuning. |
| `profiles/apps/WhatsApp.json` | `S2` | `local-first` enabled, `controlled-online` disabled | Includes Bluetooth preconnect, host receiver preparation, export pass count, unpair, and initial state restoration flags. |
| `profiles/apps/WeChat.json` | `S3` | `local-first` enabled, `controlled-online` disabled | Includes EasyOCR profile, chat list limits, chat history max page cap, and visual/OCR stagnation thresholds. |

Current system UI profiles:

| Profile | Intended devices | Coverage |
|---|---|---|
| `generic` | Google Pixel / AOSP-like Android | Generic DND, recent apps, network, and share sheet behavior. |
| `samsung` | Samsung Galaxy devices | Samsung DND Hide notifications/Hide all handling, recent apps, Bluetooth, share sheet behavior. |
| `huawei` | Huawei EMUI devices | Huawei Sounds/Do Not Disturb, recent-apps trash action, Bluetooth/settings behavior. |

The system UI profile layer exists because DND, airplane mode, Wi-Fi, recent-apps, and Bluetooth screens differ by OEM. The app profile remains focused on messenger acquisition behavior.

## 5. Phase Model

AURA uses two normalized phase names:

| Phase | Meaning |
|---|---|
| `local-first` | Acquire locally visible data under constrained network conditions. Typically airplane mode on, DND on, Wi-Fi off. |
| `controlled-online` | Re-enable controlled Wi-Fi, restart the app, wait for synchronization stabilization, and acquire app-visible data again. |

Legacy aliases normalize internally:

| Legacy | Normalized |
|---|---|
| `offline` | `local-first` |
| `online` | `controlled-online` |
| `phase1` | `local-first` |
| `phase2` | `controlled-online` |
| `local_first` | `local-first` |
| `controlled_online` | `controlled-online` |

Each collector phase typically:

1. Sets `current_phase`.
2. Switches artifact directory to `<target>/<phase>`.
3. Runs phase preflight.
4. Enforces airplane/DND/Wi-Fi policy.
5. Restarts the target app when required.
6. Runs app-specific acquisition.
7. Optionally disables Wi-Fi after controlled-online collection.
8. Records timing and phase result.
9. Restores global state in `finally`.

Telegram currently includes additional controlled-online stabilization: clear recent apps, turn Wi-Fi on, reconnect, restart Telegram, and wait briefly for app synchronization. This reduces transient rows such as channel update placeholders being captured as logical chatrooms.

## 6. Device State And System UI Handling

Global setup is implemented in `utils/utils.py` and system UI profile helpers.

Setup currently:

1. Sets active ADB serial.
2. Dismisses the lock screen if possible.
3. Opens recent apps.
4. Closes recent apps through the configured system UI profile.
5. Checks airplane mode, Wi-Fi, and DND.
6. Enables DND when required.
7. Enables airplane mode when required.
8. Writes setup audit events.

DND handling is UI-driven because Android Settings behavior differs across devices and can affect notification popups during acquisition.

Current DND behavior:

- Opens Android Settings or the configured DND route.
- Enters Do Not Disturb.
- Turns DND on when required.
- Enters `Hide notifications` when supported.
- Ensures `Hide all` is on.
- Reads switch state from UI XML where available.
- Clicks text/row bounds when direct switch clicks are unreliable.
- Avoids blind toggles that would turn an already-on switch off.
- Restores DND according to the initial device state at teardown.

This behavior is shared by Telegram, WhatsApp, and WeChat through common helpers.

## 7. Base Collector Infrastructure

All collectors inherit from `platforms/base.py::BaseCollector`.

Important responsibilities:

| Functionality | Description |
|---|---|
| `log_action()` | Writes structured JSONL records to `AURA_audit.log`. Includes run, phase, account, chat, source class/function, action, selector, result, error, artifacts, and source context. |
| `register_artifact()` | Inserts or updates `artifacts` rows, logs `artifact_context_registered`, and queues the artifact for hash finalization. |
| `capture_visual_evidence()` | Captures screenshot and optional UI XML, registers both as artifacts, and links them to current context. |
| `flush_artifact_hashes()` | Computes SHA-256 and size metadata for queued artifacts. |
| `aura_prefix()` | Creates consistent console/log prefixes. |

`register_artifact()` is the main gateway for acquisition-context linkage. It records:

- run ID
- app
- phase
- account
- chat ID
- message ID
- observation ID
- artifact path
- artifact kind
- source action
- source screen
- size

The audit log records the procedural event. SQLite records normalized artifact and linkage rows.

## 8. Audit Logging Model

The raw audit ledger is `AURA_audit.log`.

Each line is a JSON object with fields such as:

```json
{
  "ts": 1779880000.123,
  "seq": 42,
  "run_id": "AURA_YYYYMMDD_HHMMSS",
  "app": "com.whatsapp",
  "phase": "local-first",
  "account": "default",
  "chat_id": "chat_123",
  "source_class": "WhatsAppCollector",
  "source_func": "_export_current_chat",
  "action": "export_chat_stage",
  "selector": "<CHAT_DISPLAY_NAME>",
  "result": "success",
  "error": null,
  "artifacts": ["share_sheet_ready"],
  "side_effect_hint": null
}
```

AURA logs events when they change state, create evidence, derive records, validate a relationship, or produce a reviewer-relevant warning.

Examples of audit-worthy events:

- Run lifecycle.
- Orchestrator start/end.
- Primitive execution start/end.
- Phase start/end.
- Network and DND setup.
- Screen transitions for critical UI states.
- Safe-click success/failure for meaningful actions.
- Artifact registration.
- Host Bluetooth receiver preparation.
- Export file copy/materialization.
- Archive extraction.
- Parser input and parser output.
- Attachment validation.
- Missing, ambiguous, duplicate, skipped, or recovered acquisition targets.
- Packaging and report generation.

Non-audit-worthy events are kept out of the audit log unless they become reviewer-relevant:

- Internal directory enumeration.
- Normal loop iteration.
- DB lookups.
- Debug-only intermediate state.
- Trivial UI movement that does not change acquisition state.

## 9. Review JSON And HTML

`utils/audit_review.py` imports `AURA_audit.log` into `audit_events`, backfills acquisition-context links, generates `AURA_audit_review.json`, and renders `AURA_audit_timeline.html`.

Review outputs are designed to make the raw audit log navigable:

| Review section | Purpose |
|---|---|
| Phase Summary | Event counts, attention-required events, recovered non-success events, benign non-success events, and action chips. |
| Artifact Trace Coverage | Highlights attachment trace coverage and attempt coverage, with lower-level linkage diagnostics folded under advanced details. |
| Artifact Summary | Artifact groups by phase, kind, source action, and source screen. |
| Artifact Acquisition Trace | Per-artifact context, source action, audit event, and acquisition attempt details for explaining how a file was obtained or why a related attempt failed. |
| Key Events | Run, primitive, package, and artifact milestones. |
| Attention Required | Unresolved non-success events. |
| Recovered Non-Success | Transient failures that were later recovered by a success marker. |
| Benign Non-Success | Expected optional waits or already-open states that do not require reviewer action. |
| Raw JSON Details | Collapsible JSON payloads for exact sequence reconstruction. |

The HTML report includes a collapsible interpretation guide explaining how to read each section.

A public, anonymized example of the generated HTML review report is available at [`docs/examples/AURA_audit_timeline_sample.html`](examples/AURA_audit_timeline_sample.html).

Coverage values are intentionally separated by linkage type. Attachment attempt coverage is the primary reviewer-facing measure for content-bearing attachments. Advanced diagnostics such as "artifacts linked to acquisition attempts" can be lower because screenshots, UI trees, OCR sidecars, and other support files are traceable evidence artifacts but are not acquisition targets with attempt records.

## 10. SQLite Database Model

The database intentionally separates logical records from observations and artifacts.

Core tables:

| Table | Purpose |
|---|---|
| `runs` | Run-level metadata. |
| `collection_contexts` | App + phase + account context. |
| `contacts` | Contacts observed in a context. |
| `chatrooms` | Logical chatroom entities. |
| `chatroom_observations` | UI-observed chatroom rows. |
| `messages` | Logical message records. |
| `message_observations` | Page/row/bounds/raw-text observations for messages. |
| `artifacts` | Screenshots, XML, OCR sidecars, exports, extracted files, downloaded attachments. |
| `audit_events` | Normalized import of `AURA_audit.log`. |
| `artifact_action_context_links` | Internal linkage table for artifact/action/context relationships. Name is historical; conceptually this supports acquisition-context linkage. |
| `derived_record_links` | Reserved link table for derived records. |
| `acquisition_attempts` | Attempt-level attachment/export acquisition outcomes. |

Convenience views:

| View | Purpose |
|---|---|
| `messages_compact` | Readable joined message view. |
| `attachment_artifacts` | Attachment-focused artifact view. |
| `attachment_attempt_summary` | Attempt status, paths, hashes, and failure reasons. |
| `message_attachment_summary` | Message-level attachment collection summary. |
| `audit_review_events` | Ordered audit event view. |

Important acquisition attempt fields:

| Field | Meaning |
|---|---|
| `method` | Primitive or acquisition method context. |
| `primitive` | Configured primitive where available. |
| `route_id` | Route/action identifier. |
| `ui_reached` | Whether the target UI state was reached. |
| `artifact_materialized` | Whether a file/artifact was materialized. |
| `status` | Attempt status such as `success`, `missing`, `failed`, or `ambiguous`. |
| `failure_reason` | Reviewer-readable reason for failed or missing attempts. |
| `screenshot_artifact_id` / `uitree_artifact_id` | Visual evidence references when captured. |
| `audit_operation_id` | Links post-acquisition validation/processing operations to audit events. |

## 11. Telegram Profile Configured With S1

Telegram uses UI hierarchy parsing and state-aware navigation.

Current behavior:

- Runs `local-first` and `controlled-online` phases.
- Restarts the app after phase preflight.
- Uses controlled-online Wi-Fi connection and app sync settle.
- Discovers chatrooms with bounded passes and duplicate-safe logic.
- Reads profile/chatroom type details to classify direct, group, channel, bot/service, and related chat types.
- Traverses chat history with safe bounds.
- Parses messages from UI hierarchy.
- Captures screenshots/XML for visual evidence and failed state checks.
- Acquires attachments by before/after Android filesystem snapshot diff.
- Uses `telegram_attachment_snapshot_dirs` by attachment type.
- Cleans up newly downloaded Telegram device files after successful copy/hash when configured.

Attachment identity is deliberately split:

| Identity | Meaning |
|---|---|
| `display_filename` | Filename or label shown in Telegram UI. |
| `device_path` / `device_basename` | Actual Android filesystem artifact created by Telegram/Android. |
| `sha256` / `content_group_id` | Content identity after acquisition. |
| `observation_id` | Run-local UI observation/action identity. |
| `record_id` | Storage identity for the message row. |

The current `attachment_identity_policy_version` is `telegram_attachment_identity_v1`.

Important reliability decisions:

- Consecutive photos/videos can be distinguished by observation/action context rather than only by weak visual labels.
- Attachment re-download suppression uses policy-aware keys.
- Failed attachment attempts can carry screenshot/XML references and acquisition attempt metadata.
- Chat history scroll distance is profile-tuned to reduce boundary misses on different devices.

## 12. WhatsApp Profile Configured With S2

WhatsApp uses the official Export Chat UI route and Windows Bluetooth receive.

Current profile status:

- `local-first` enabled.
- `controlled-online` disabled.
- Bluetooth preconnect enabled.
- Host receiver preparation enabled before export.
- Bluetooth unpair and initial state restoration enabled after collection.

Current behavior:

- Discovers chat list with screenshots and UI XML.
- Preconnects Bluetooth before export.
- Resolves host PC Bluetooth target name.
- Pairs Android device with the host PC through OEM-specific Bluetooth UI.
- Confirms pairing/connection before proceeding.
- Enters each target chat.
- Opens Export Chat and selects Include Media when available.
- Prepares Windows `fsquirt.exe -receive` receiver at the correct export stage.
- Handles Android share sheet selection for Bluetooth.
- Searches horizontally first and expands the sheet only when horizontal movement does not expose Bluetooth.
- Captures share sheet screenshot/XML.
- Captures Windows receiver progress/finish screenshots when available.
- Detects received export files from host Bluetooth exchange directories.
- Copies received export files into the run evidence package.
- Registers copied files with size/SHA-256 metadata.
- Extracts ZIP archives.
- Registers extracted text and media artifacts.
- Parses WhatsApp text export into normalized messages.
- Validates text-referenced attachments against extracted files.
- Inserts missing attachment attempts when the text export references media that is absent from the archive.

Important WhatsApp post-acquisition audit events:

| Event | Purpose |
|---|---|
| `copy_received_export` | Records the transfer from host receive location into the run evidence package. |
| `register_export_archive_artifact` | Records export ZIP/file artifact registration and immediate archive metadata. |
| `extract_export_archive` | Records archive extraction input/output summary. |
| `register_extracted_artifacts` | Records extracted artifact count and registration summary. |
| `parse_chat_text` | Records parser input, parser version/context, message count, and attachment mention count. |
| `validate_attachment_mentions` | Records found/missing/ambiguous attachment validation summary. |
| `attachment_validation_warning` | Records individual missing or ambiguous attachment warnings when reviewer-relevant. |
| `commit_whatsapp_export_records` | Records DB insert/update summary for messages, artifacts, and attempts. |
| `whatsapp_export_analysis_summary` | Records final export processing summary. |

Missing media behavior:

- If the text export references an attachment but the extracted archive does not contain the file, AURA records an acquisition attempt with `status=missing`.
- Missing attempts preserve message context, expected filename, validation operation ID, and audit linkage.
- Missing media is treated as a reviewable acquisition result, not silently ignored.

## 13. WeChat Profile Configured With S3

WeChat uses OCR because reliable node tree extraction is unavailable.

Current profile status:

- `local-first` enabled.
- `controlled-online` disabled.
- OCR backend: `easyocr`.
- OCR languages: English and Korean.
- Chat history page cap: `300`.
- Termination uses combined visual/OCR stagnation rather than only a fixed page count.

Current behavior:

- Performs OCR preflight.
- Captures chat list screenshots.
- Generates OCR sidecars.
- Computes heuristic safe areas without relying on node tree access.
- Guards against Recent overlay entry.
- Captures chat history pages while moving through history.
- Stops when primary/secondary termination conditions indicate history stagnation, bounded by a high max-page cap.
- Stores OCR-derived message observations.

Termination model:

- Primary condition: visual stagnation threshold.
- Secondary condition: OCR token overlap/stagnation.
- Hard cap: `chat_history_max_pages`.

This balances OCR variability against the need to avoid infinite traversal.

## 14. Stability Primitives

AURA collectors share a small set of reliability primitives:

| Primitive | Purpose |
|---|---|
| `safe_click()` | Execute a UI click and verify the expected screen state. |
| `wait_for_screen_state()` | Poll a predicate until a screen is confirmed or times out. |
| `wait_for_visual_stable()` | Confirm visual stability through repeated screenshot hashes. |
| `wait_for_consecutive_match()` | Require a sampled condition to match for N consecutive polls. |
| `wait_for_consecutive_same_sample()` | Require a sampled value to remain stable for N consecutive polls. |
| `press_back_to_state()` | Bounded back navigation with state confirmation. |
| `capture_visual_evidence()` | Screenshot and optional UI XML capture with artifact registration. |
| `register_artifact()` | Insert artifact metadata, write audit linkage event, and queue hashing. |

The system intentionally avoids logging every gesture. It logs gestures and transitions when they establish state, recover from uncertainty, or produce reviewer-relevant evidence.

## 15. Output Review Workflow

Recommended review order:

```text
AURA_audit_timeline.html
  -> attention required / recovered / benign events
  -> artifact groups
  -> aura.db views
  -> raw AURA_audit.log only when exact sequence reconstruction is needed
```

Start with:

| Output | What to check |
|---|---|
| `AURA_audit_timeline.html` | Human-readable phase summary, failure classification, artifact summary, and raw JSON details. |
| `AURA_audit_review.json` | Structured review summary generated from SQLite audit events. |
| `aura.db` | Normalized messages, artifacts, attempts, audit events, and linkage tables. |
| `AURA_audit.log` | Canonical chronological event stream. |
| `AURA_*_report.json` | Archive manifest and run summary. |
| `_screen_state_timeouts/` | Screenshots/XML for failed screen-state checks. |
| `preflight_*.json` | Network/DND/Wi-Fi and method-specific preflight status. |

Useful SQLite views for attachment review:

```sql
SELECT *
FROM attachment_attempt_summary
ORDER BY phase, chat_id, message_id, attempt_ts;
```

```sql
SELECT *
FROM message_attachment_summary
WHERE missing_attempts > 0 OR failed_attempts > 0;
```

```sql
SELECT *
FROM attachment_artifacts
ORDER BY phase, chat_id, message_id, artifact_path;
```

## 16. Validation Environment

AURA has been exercised on representative Android devices to validate profile-driven app acquisition and OEM/system UI handling. These devices describe the current validation environment, not a fixed device requirement.

| Device | Android | OEM / UI | System UI profile | Notes |
|---|---:|---|---|---|
| Samsung Galaxy S8 | 9 | One UI 1.0 | `samsung` | Legacy Samsung DND, Bluetooth, and Settings behavior. |
| Samsung Galaxy S21 5G | 14 | One UI 6.1 | `samsung` | Modern Samsung DND, share sheet, Bluetooth, and recent-apps behavior. |
| Google Pixel 5 | 14 | Pixel / AOSP-like UI | `generic` | Generic Android/Pixel DND, recent-apps, and share sheet behavior. |
| Huawei P30 Lite | 9 | EMUI 9.1.0 | `huawei` | Huawei Settings, DND, recent-apps, and Bluetooth behavior. |

Validated routes:

- Telegram profile configured with S1 on Pixel/Huawei/Samsung representative devices.
- WhatsApp profile configured with S2 on Samsung/Pixel/Huawei representative devices.
- WeChat profile configured with S3 after OCR termination and safe-area tuning.

## 17. Test And Verification State

Current automated tests cover:

| Test file | Coverage |
|---|---|
| `tests/test_audit_linkage_logging.py` | Normalized audit DB import, review HTML, linkage backfill, audit classification. |
| `tests/test_dnd_pixel_ui_support.py` | Pixel/generic system UI DND support behavior. |
| `tests/test_telegram_attachment_identity.py` | Telegram attachment identity, acquisition attempts, and visual evidence references. |
| `tests/test_telegram_chat_history_scroll.py` | Telegram chat history scroll tuning/source-level guard. |
| `tests/test_telegram_phase_transition.py` | Telegram controlled-online phase restart/stabilization and chat type classification behavior. |
| `tests/test_wechat_history_stop.py` | WeChat visual/OCR stagnation termination behavior. |
| `tests/test_whatsapp_artifact_metadata.py` | WhatsApp artifact metadata, missing attachment parser behavior, missing media attempt summary, audit operation ID persistence. |
| `tests/test_whatsapp_bluetooth_teardown.py` | Bluetooth unpair/teardown behavior. |

Recent verification commands:

```powershell
python -m py_compile <repo python files>
python -m unittest discover tests
```

Known non-fatal warning:

- Some tests can emit an ADB socket `ResourceWarning` from `utils/utils.py` when mocked or local ADB socket checks do not close before interpreter warning collection. The test suite still passes.

## 18. Current Reliability Boundaries

Known boundaries:

- WeChat remains OCR-dependent and therefore less deterministic than node-tree-driven Telegram/WhatsApp paths.
- WhatsApp export depends on Android share sheet behavior and Windows Bluetooth receiver state.
- Android/OEM Settings screens vary across devices; new models may require new system UI profile entries.
- Bluetooth pairing often requires user-visible confirmation on the device and host.
- Time values are preserved, but AURA emphasizes acquisition-context linkage and integrity more than timestamp precision.
- The prototype is designed for authorized, controlled acquisition experiments, not covert or hardened commercial acquisition.

## 19. Public Hygiene State

Current public-facing hygiene decisions:

- README uses AURA naming and acquisition-context terminology.
- App profiles are under `profiles/apps/`.
- System UI profiles are under `profiles/system_ui/`.
- `runs/`, `backups/`, `.venv/`, `__pycache__/`, `desktop.ini`, temporary Codex files, and `aura.db` are ignored.
- Source code and README avoid personal paths, host usernames, device serials, phone numbers, and run-specific values.
- Test fixtures may contain synthetic placeholder host names or file paths; these are dummy test values, not real operator or device data.

## 20. Handoff Summary

The current implementation has moved from app-specific ad-hoc flows toward a profile-driven architecture:

- App profiles select acquisition primitives.
- System UI profiles isolate OEM Settings differences.
- Collectors share audit, artifact registration, visual evidence, and storage primitives.
- WhatsApp S2 records both collected files and expected-but-missing attachments.
- Telegram S1 distinguishes weak UI media observations from materialized content identity.
- WeChat S3 remains OCR-based but uses bounded visual/OCR termination.
- The raw JSONL audit log remains the chronological record, while SQLite and HTML/JSON review outputs make acquisition-context linkage queryable and reviewable.
