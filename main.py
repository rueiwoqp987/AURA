import argparse
import json
import logging
import sqlite3
import os
import shutil
import time
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import uiautomator2 as u2

from orchestrator import AURAOrchestrator
from utils.audit_review import build_audit_review_outputs, import_audit_jsonl_to_db
from utils.device_info import collect_device_identifiers
from utils.evidence import sha256_file
from utils.network_state import snapshot_network_state
from utils.utils import init_tool, write_audit

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
PROFILE_REGISTRY = {
    "telegram": "Telegram.json",
    "whatsapp": "WhatsApp.json",
    "wechat": "WeChat.json",
}

def _load_aura_version() -> str:
    try:
        if VERSION_FILE.exists():
            v = VERSION_FILE.read_text(encoding="utf-8-sig").strip()
            return v or "1.0"
    except Exception:
        pass
    return "1.0"

def _load_json_if_exists(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None

def _is_sidecar_metadata(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == "device_info.json"
        or name == "collection_timing.json"
        or (name.startswith("preflight_") and name.endswith(".json"))
    )

def _normalize_path(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))

def _write_lifecycle_audit(
    audit_path: Path,
    *,
    package_name: str,
    run_id: str,
    action: str,
    selector: str | None = None,
    result: str = "success",
    artifacts: list[str] | None = None,
    error: Exception | str | None = None,
    phase: str = "setup",
) -> None:
    write_audit(
        log_path=str(audit_path),
        package_name=package_name or "system",
        action=action,
        selector=selector,
        result=result,
        error=error,
        artifacts=list(artifacts or []),
        run_id=run_id,
        phase=phase,
    )

def _load_artifact_hash_index(run_root: Path) -> dict[str, dict]:
    """Load (path -> sha256/size) from aura.db to avoid re-hashing artifacts."""
    db_path = run_root / 'aura.db'
    if not db_path.exists():
        return {}

    run_id = run_root.name
    index: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT artifact_path, sha256, size_bytes
                FROM artifacts
                WHERE run_id = ? AND sha256 IS NOT NULL AND sha256 != ''
                """,
                (run_id,),
            )
            for artifact_path, digest, size_bytes in cur.fetchall():
                try:
                    key = os.path.normcase(os.path.normpath(str(artifact_path)))
                    index[key] = {
                        'sha256': digest,
                        'size': int(size_bytes) if size_bytes is not None else None,
                    }
                except Exception:
                    continue
            cur.close()
        finally:
            conn.close()
    except Exception:
        return {}
    return index

def _build_file_manifest(run_root: Path, include_sidecar_metadata: bool, artifact_index: dict[str, dict] | None = None) -> list[dict]:
    manifest = []
    artifact_index = artifact_index or {}
    for path in sorted(run_root.rglob("*")):
        if not path.is_file():
            continue
        if not include_sidecar_metadata and _is_sidecar_metadata(path):
            continue

        rel_path = str(path.relative_to(run_root)).replace("\\", "/")
        manifest.append(
            {
                "path": rel_path,
                "size": path.stat().st_size,
                "sha256": (artifact_index.get(_normalize_path(path.resolve())) or {}).get("sha256") or sha256_file(path),
            }
        )
    return manifest

def _create_run_archive(run_root: Path) -> tuple[Path, list[dict]]:
    archive_path = run_root.with_suffix(".zip")
    artifact_index = _load_artifact_hash_index(run_root)
    archived_manifest = _build_file_manifest(run_root, include_sidecar_metadata=False, artifact_index=artifact_index)

    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as zf:
        for item in archived_manifest:
            abs_path = run_root / item["path"]
            zf.write(abs_path, arcname=item["path"])

    return archive_path, archived_manifest

def _finalize_pending_artifact_hashes(run_root: Path) -> int:
    db_path = run_root / "aura.db"
    if not db_path.exists():
        return 0

    updated = 0
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT run_id, artifact_path
            FROM artifacts
            WHERE sha256 IS NULL OR sha256 = ''
            """
        )
        rows = cur.fetchall()
        for run_id, artifact_path in rows:
            p = Path(artifact_path)
            if not p.exists() or not p.is_file():
                continue
            try:
                digest = sha256_file(p)
                size_bytes = int(p.stat().st_size)
                cur.execute(
                    """
                    UPDATE artifacts
                    SET sha256 = ?, size_bytes = COALESCE(?, size_bytes)
                    WHERE run_id = ? AND artifact_path = ?
                    """,
                    (digest, size_bytes, run_id, artifact_path),
                )
                updated += 1
            except Exception:
                continue
        conn.commit()
    finally:
        conn.close()
    return updated

def _remove_run_dir(run_root: Path) -> bool:
    attempts = 4
    delay_sec = 0.5

    for idx in range(attempts):
        try:
            shutil.rmtree(run_root)
            break
        except Exception as e:
            label = 'first try' if idx == 0 else f'retry_{idx}'
            logging.warning('Run directory removal failed (%s): %s', label, e)
            time.sleep(delay_sec)
            delay_sec = min(delay_sec * 1.6, 2.0)

    if run_root.exists():
        try:
            remaining = []
            for rp in sorted(run_root.rglob('*')):
                remaining.append(str(rp.relative_to(run_root)).replace('\\', '/'))
                if len(remaining) >= 20:
                    break
            if remaining:
                logging.warning('Run directory still not empty; remaining sample: %s', remaining)
        except Exception:
            pass
    return not run_root.exists()

def _checkpoint_sqlite_wal(run_root: Path) -> None:
    """
    Attempt to checkpoint and truncate the SQLite WAL to reduce deletion failures on Windows.
    """
    db_path = run_root / 'aura.db'
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute('PRAGMA wal_checkpoint(TRUNCATE);')
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception:
        return

def _write_summary(
    run_root: Path,
    profile: dict,
    orchestrator_results: dict,
    archive_path: Path,
    archived_manifest: list[dict],
    aura_version: str,
) -> Path:
    run_id = run_root.name
    report_path = run_root.parent / f"{run_id}_report.json"
    device_info_path = run_root / 'device_info.json'
    timing_path = run_root / 'collection_timing.json'
    preflight_paths = sorted(run_root.glob('preflight_*.json'))

    timing_data = _load_json_if_exists(timing_path) or {}
    collection_runtime = {
        "started_at": timing_data.get("started_at"),
        "ended_at": timing_data.get("ended_at"),
        "duration_sec": timing_data.get("duration_sec"),
    }

    method_status = {}
    if isinstance(orchestrator_results, dict):
        for method, payload in orchestrator_results.items():
            if isinstance(payload, dict):
                method_status[method] = payload.get("status")
            else:
                method_status[method] = str(payload)

    archive_sha256 = sha256_file(archive_path)
    archive_size = archive_path.stat().st_size

    phase_preflight = {}
    for pp in preflight_paths:
        stem = pp.stem
        lower = stem.lower()
        key = stem
        marker = 'preflight_'
        if marker in lower:
            key = stem[lower.index(marker) + len(marker):]
        else:
            key = stem.replace('preflight_', '')
        phase_preflight[key] = _load_json_if_exists(pp)

    summary = {
        "summary_schema_version": "v2",
        "aura_version": aura_version,
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "target_app": {
            "name": profile.get("app_name"),
            "package_name": profile.get("package_name"),
            "version": profile.get("app_version"),
        },
        "execution": {
            "enabled_methods": profile.get("collection_methods", []),
            "phase_execution_plan": profile.get("phase_plan", []),
            "device_serial": (profile.get("_runtime") or {}).get("serial"),
            "method_execution_status": method_status,
            "run_duration": collection_runtime,
        },
        "device_info": ((profile.get("_runtime") or {}).get("device_info") or _load_json_if_exists(device_info_path)),
        "phase_preflight": phase_preflight,
        "archive_bundle": {
            "path": archive_path.name,
            "size": archive_size,
            "sha256": archive_sha256,
            "file_count": len(archived_manifest),
            "files": archived_manifest,
        },
    }
    # Use UTF-8 with BOM for better default rendering in Windows tools while
    # preserving non-ASCII artifact paths in the JSON manifest.
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8-sig")
    return report_path

def load_target_profile(target: str):
    profile_file = PROFILE_REGISTRY.get(target)
    if not profile_file:
        raise SystemExit(f"Unsupported target: {target}")

    profile_path = PROFILE_DIR / profile_file
    if not profile_path.exists():
        raise SystemExit(f"Profile not found: {profile_path}")

    with profile_path.open("r", encoding="utf-8-sig") as f:
        profile = json.load(f)

    if not isinstance(profile, dict):
        raise SystemExit(f"Invalid profile format: {profile_path}")

    return profile, profile_path

def _parse_log_level(value: str) -> int:
    level_name = (value or "INFO").upper().strip()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise SystemExit(f"Unsupported log level: {value}")
    return level

def main() -> None:
    overall_started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    overall_start_ts = time.time()
    aura_version = _load_aura_version()

    parser = argparse.ArgumentParser(
        description="AURA PoC collector",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Example:\n"
            "  python main.py --target telegram --runs-dir [OUTPUT] --keep-run-dir"
        ),
    )
    #parser.add_argument("--target", default="telegram", help="Target app (telegram/whatsapp/wechat)")
    parser.add_argument("--target", default="telegram", help="Target app (telegram/whatsapp/wechat)")
    #parser.add_argument("--serial", default="ce021712d5c950d80c", help="ADB device serial (optional)") # Galaxy S8
    parser.add_argument("--serial", default="R3CR705MQLY", help="ADB device serial (optional)") # Galaxy S21 5G
    parser.add_argument("--runs-dir", default="C:\\Users\\Junki\\Desktop\\AURA_Result", help="Run output root")
    parser.add_argument("--keep-run-dir", action="store_true", help="Keep raw run directory after creating zip")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Console log level",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=_parse_log_level(args.log_level),
        format="[%(asctime)s] [%(levelname)s] [%(name)s:%(funcName)s]: %(message)s",
        force=True,
    )

    target = args.target.lower().strip()
    profile, profile_path = load_target_profile(target)
    profile = dict(profile)
    pre_init_state = snapshot_network_state(serial=args.serial)
    profile["_runtime"] = {
        "serial": args.serial,
        "initial_state": pre_init_state,
        "log_level": args.log_level,
    }

    logging.info("Loaded profile: %s", profile_path)
    logging.info("AURA version: %s", aura_version)
    logging.info(
        "Target profile: app=%s package=%s methods=%s version=%s",
        profile.get("app_name", target),
        profile.get("package_name", "unknown"),
        ",".join(profile.get("collection_methods", [])),
        profile.get("app_version", "unknown"),
    )

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_root = Path(args.runs_dir).resolve() / f"AURA_{ts}"
    target_dir = run_root / target
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_path = run_root / "AURA_audit.log"
    package_name = profile.get("package_name", "system")
    _write_lifecycle_audit(
        audit_path,
        package_name=package_name,
        run_id=run_root.name,
        action="run_start",
        selector=target,
        artifacts=[
            f"target={target}",
            f"run_root={run_root}",
            f"target_dir={target_dir}",
            f"profile={profile_path}",
            f"serial={args.serial or ''}",
            f"aura_version={aura_version}",
            f"methods={','.join(profile.get('collection_methods', []))}",
        ],
    )

    _write_lifecycle_audit(
        audit_path,
        package_name=package_name,
        run_id=run_root.name,
        action="device_connect_start",
        selector=args.serial or "default",
    )
    device = u2.connect(args.serial) if args.serial else u2.connect()
    _write_lifecycle_audit(
        audit_path,
        package_name=package_name,
        run_id=run_root.name,
        action="device_connect_end",
        selector=args.serial or "default",
    )
    device_info = collect_device_identifiers(serial=args.serial)
    profile["_runtime"]["device_info"] = device_info
    write_audit(
        log_path=str(audit_path),
        package_name=profile.get("package_name", "system"),
        action="device_identify",
        selector="adb_getprop",
        artifacts=[json.dumps(device_info, ensure_ascii=False)],
        run_id=run_root.name,
        phase="setup",
    )
    write_audit(
        log_path=str(audit_path),
        package_name=profile.get("package_name", "system"),
        action="initial_state_snapshot",
        selector="network_state_before_init_tool",
        artifacts=[json.dumps(pre_init_state, ensure_ascii=False)],
        run_id=run_root.name,
        phase="setup",
    )
    init_tool(
        device,
        audit_log_path=str(audit_path),
        package_name=profile.get("package_name", "system"),
        serial=args.serial,
        run_id=run_root.name,
        phase="setup",
    )
    orchestrator = AURAOrchestrator(
        target=target,
        profile=profile,
        device=device,
        run_root=run_root,
        target_dir=target_dir,
        audit_log_path=audit_path,
    )
    run_results = {}
    run_error = None
    try:
        run_results = orchestrator.run()
    except Exception as e:
        run_error = e
        run_results = {"status": "failed", "error": str(e)}
        logging.exception("Orchestrator execution failed; packaging partial artifacts")
    finally:
        _write_lifecycle_audit(
            audit_path,
            package_name=package_name,
            run_id=run_root.name,
            action="artifact_hash_finalize_start",
            selector="artifacts",
            phase="package",
        )
        updated_hashes = _finalize_pending_artifact_hashes(run_root)
        _write_lifecycle_audit(
            audit_path,
            package_name=package_name,
            run_id=run_root.name,
            action="artifact_hash_finalize_end",
            selector="artifacts",
            artifacts=[f"updated_hashes={updated_hashes}"],
            phase="package",
        )
        if updated_hashes > 0:
            logging.info("Finalized pending artifact hashes: %d", updated_hashes)

        _write_lifecycle_audit(
            audit_path,
            package_name=package_name,
            run_id=run_root.name,
            action="packaging_start",
            selector="archive",
            artifacts=[
                f"run_root={run_root}",
                f"archive={run_root.with_suffix('.zip')}",
                f"updated_hashes={updated_hashes}",
            ],
            phase="package",
        )
        try:
            _write_lifecycle_audit(
                audit_path,
                package_name=package_name,
                run_id=run_root.name,
                action="audit_review_build_start",
                selector="audit_events",
                phase="package",
            )
            audit_event_count = import_audit_jsonl_to_db(audit_path, run_root / "aura.db")
            review_json_path = run_root / "AURA_audit_review.json"
            review_html_path = run_root / "AURA_audit_timeline.html"
            audit_review = build_audit_review_outputs(
                db_path=run_root / "aura.db",
                review_json_path=review_json_path,
                review_html_path=review_html_path,
            )
            _write_lifecycle_audit(
                audit_path,
                package_name=package_name,
                run_id=run_root.name,
                action="audit_review_build_end",
                selector="audit_events",
                artifacts=[
                    f"imported_events={audit_event_count}",
                    f"review_events={audit_review.get('event_count')}",
                    f"review_json={review_json_path}",
                    f"review_html={review_html_path}",
                ],
                phase="package",
            )
            import_audit_jsonl_to_db(audit_path, run_root / "aura.db")
            build_audit_review_outputs(
                db_path=run_root / "aura.db",
                review_json_path=review_json_path,
                review_html_path=review_html_path,
            )
        except Exception as e:
            logging.warning("Audit review generation failed: %s", e)
            _write_lifecycle_audit(
                audit_path,
                package_name=package_name,
                run_id=run_root.name,
                action="audit_review_build_end",
                selector="audit_events",
                result="fail",
                error=e,
                phase="package",
            )
        archive_path, archived_manifest = _create_run_archive(run_root)
        summary_path = _write_summary(
            run_root=run_root,
            profile=profile,
            orchestrator_results=run_results,
            archive_path=archive_path,
            archived_manifest=archived_manifest,
            aura_version=aura_version,
        )

        if not args.keep_run_dir:
            _checkpoint_sqlite_wal(run_root)
            _write_lifecycle_audit(
                audit_path,
                package_name=package_name,
                run_id=run_root.name,
                action="run_dir_cleanup_start",
                selector=str(run_root),
                phase="package",
            )
            removed = _remove_run_dir(run_root)
            if not removed:
                logging.warning("Run directory still exists: %s", run_root)
        else:
            _write_lifecycle_audit(
                audit_path,
                package_name=package_name,
                run_id=run_root.name,
                action="run_dir_cleanup_skip",
                selector=str(run_root),
                artifacts=["keep_run_dir=true"],
                phase="package",
            )

        logging.info("Packaging complete: archive=%s report=%s", archive_path, summary_path)

        overall_duration = time.time() - overall_start_ts
        logging.info("Total run time: started_at=%s ended_at=%s (duration=%.2fs)", overall_started_at, time.strftime("%Y-%m-%d %H:%M:%S"), overall_duration)

    if run_error is not None:
        raise run_error


if __name__ == "__main__":
    main()
