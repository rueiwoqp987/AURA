import time
import logging
import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from utils.utils import download_file, get_adb_device, get_file_last_modified_time, get_last_N_file
from utils.evidence import sha256_file
from collectors.telegram.mixin_base import TelegramCollectorDeps

logger = logging.getLogger(__name__)

class TelegramAttachmentsMixin(TelegramCollectorDeps):
    def _shell_single_quote(self, value):
        return "'" + str(value).replace("'", "'\\''") + "'"

    def _attachment_snapshot_dirs(self, msg_type):
        dirs = []
        configured = self.profile.get("telegram_download_snapshot_dirs") or self.profile.get("download_snapshot_dirs") or []
        for item in configured:
            if item:
                dirs.append(str(item))

        typed_dirs = getattr(self, "telegram_attachment_snapshot_dirs", {}) or {}
        if isinstance(typed_dirs, dict):
            for item in typed_dirs.get(msg_type, []) or []:
                if item:
                    dirs.append(str(item))

        # Compatibility fallbacks for older profiles/collector attributes.
        if msg_type in ("Photo", "Video") and getattr(self, "pictures_path", None):
            dirs.append(self.pictures_path)
        if msg_type == "File":
            for item in list(dict.fromkeys(getattr(self, "download_path_candidates", []) or [])):
                if item:
                    dirs.append(str(item))
            if getattr(self, "download_path", None):
                dirs.append(str(self.download_path))

        # Keep directories ordered and unique.
        return list(dict.fromkeys(dirs))

    def _telegram_attachment_loading_dialog_state(self) -> dict:
        """Detect Telegram's modal attachment loading progress dialog.

        Observed hierarchy contains only a centered Telegram dialog with
        TextView("Loading..."), a thin progress bar View, and TextView("0%").
        It does not include the underlying chatroom hierarchy, so the normal
        chatroom predicate is expected to be false while it is visible.
        """
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
        except Exception:
            return {"visible": False}

        if self._xml_package(root) not in ("", "org.telegram.messenger"):
            return {"visible": False}

        has_loading = False
        percent = None
        has_progress_bar_shape = False
        telegram_nodes = 0

        for node in root.iter():
            if self._xml_package(node) == "org.telegram.messenger":
                telegram_nodes += 1

            text = self._xml_text(node)
            node_class = self._xml_class(node)
            bounds = self._xml_bounds(node)

            if node_class == "android.widget.TextView" and text == "Loading...":
                has_loading = True
            elif node_class == "android.widget.TextView" and re.fullmatch(r"\d{1,3}%", text or ""):
                try:
                    percent = int(text.rstrip("%"))
                except Exception:
                    percent = None

            if node_class == "android.view.View" and bounds:
                left, top, right, bottom = bounds
                width = right - left
                height = bottom - top
                if width >= 300 and 1 <= height <= 24:
                    has_progress_bar_shape = True

        visible = telegram_nodes > 0 and has_loading and percent is not None and has_progress_bar_shape
        return {"visible": visible, "percent": percent if visible else None}

    def _is_telegram_attachment_loading_dialog(self) -> bool:
        return bool(self._telegram_attachment_loading_dialog_state().get("visible"))

    def _snapshot_device_files(self, dirs):
        d = get_adb_device()
        snapshot = {}

        for base_dir in dirs or []:
            if not base_dir:
                continue
            try:
                cmd = (
                    "for f in "
                    + self._shell_single_quote(base_dir)
                    + "/*; do "
                    + '[ -f "$f" ] || continue; '
                    + 'bn=$(basename "$f"); '
                    + 'sz=$(stat -c %s "$f" 2>/dev/null || echo); '
                    + 'mt=$(stat -c %Y "$f" 2>/dev/null || echo); '
                    + 'printf "%s\\t%s\\t%s\\t%s\\n" "$f" "$bn" "$sz" "$mt"; '
                    + "done"
                )
                raw = d.shell(cmd)
            except Exception:
                raw = ""

            for line in (raw or "").splitlines():
                parts = line.split("\t")
                if len(parts) != 4:
                    continue
                device_path, basename, size_raw, mtime_raw = parts
                try:
                    size = int(str(size_raw).strip())
                except Exception:
                    size = None
                try:
                    mtime = int(str(mtime_raw).strip())
                except Exception:
                    mtime = None
                snapshot[device_path] = {
                    "device_path": device_path,
                    "basename": basename,
                    "size": size,
                    "mtime": mtime,
                }

        return snapshot

    def _diff_new_device_files(self, before, after):
        candidates = []
        before = before or {}
        after = after or {}
        for device_path, meta in after.items():
            prev = before.get(device_path)
            if prev is None:
                enriched = dict(meta)
                enriched["candidate_reason"] = "new_path"
                candidates.append(enriched)
                continue
            if prev.get("size") != meta.get("size") or prev.get("mtime") != meta.get("mtime"):
                enriched = dict(meta)
                enriched["candidate_reason"] = "modified_path"
                candidates.append(enriched)
        return candidates

    def _candidate_size_is_complete_enough(self, candidate, expected_size=None):
        size = candidate.get("size")
        if not isinstance(size, int):
            return False

        if expected_size is not None:
            try:
                expected = int(expected_size)
            except Exception:
                expected = None
            if expected is not None:
                if expected <= 0:
                    return size == expected
                return size >= max(1, int(expected * 0.98))

        return size > 0

    def _wait_for_download_candidates(
        self,
        before_snapshot,
        dirs,
        timeout=10.0,
        interval=0.5,
        expected_size=None,
        progress_timeout=5.0,
    ):
        deadline = time.time() + max(timeout, interval)
        last_candidates = []
        last_signature = None
        last_incomplete_signature = None
        stable_count = 0
        final_snapshot = before_snapshot or {}
        observed_sizes = {
            path: meta.get("size")
            for path, meta in (before_snapshot or {}).items()
            if isinstance(meta.get("size"), int)
        }
        last_progress_ts = time.time()

        while time.time() < deadline:
            self._sleep(interval)
            after_snapshot = self._snapshot_device_files(dirs)
            final_snapshot = after_snapshot
            raw_candidates = self._diff_new_device_files(before_snapshot, after_snapshot)
            progress_detected = False
            for item in raw_candidates:
                path = item.get("device_path")
                size = item.get("size")
                if not path or not isinstance(size, int):
                    continue
                previous_size = observed_sizes.get(path)
                if previous_size is None:
                    progress_detected = size > 0
                elif size > previous_size:
                    progress_detected = True
                observed_sizes[path] = size
            if progress_detected:
                last_progress_ts = time.time()

            candidates = [
                item for item in raw_candidates
                if self._candidate_size_is_complete_enough(item, expected_size=expected_size)
            ]

            if not candidates:
                loading_state = self._telegram_attachment_loading_dialog_state()
                should_log_incomplete = False
                if raw_candidates:
                    incomplete_signature = tuple(
                        sorted(
                            (
                                item.get("device_path"),
                                item.get("size"),
                                item.get("mtime"),
                            )
                            for item in raw_candidates
                        )
                    )
                    if incomplete_signature != last_incomplete_signature:
                        last_incomplete_signature = incomplete_signature
                        should_log_incomplete = True
                if should_log_incomplete:
                    self.log_action(
                        "attachment_download_poll",
                        selector="device_snapshot_diff",
                        result="candidate_incomplete",
                        artifacts=[
                            f"expected_size={expected_size if expected_size is not None else ''}",
                            f"candidate_count={len(raw_candidates)}",
                            "candidate_sizes=" + ",".join(str(item.get("size")) for item in raw_candidates[:5]),
                            "candidate_paths=" + " | ".join(str(item.get("device_path")) for item in raw_candidates[:5]),
                        ],
                    )
                if loading_state.get("visible") and (time.time() - last_progress_ts) >= progress_timeout:
                    self.log_action(
                        "attachment_download_poll",
                        selector="device_snapshot_diff",
                        result="no_progress_timeout",
                        artifacts=[
                            f"expected_size={expected_size if expected_size is not None else ''}",
                            f"progress_timeout={progress_timeout}",
                            f"loading_dialog_percent={loading_state.get('percent') if loading_state.get('percent') is not None else ''}",
                            f"candidate_count={len(raw_candidates)}",
                            "candidate_sizes=" + ",".join(str(item.get("size")) for item in raw_candidates[:5]),
                            "candidate_paths=" + " | ".join(str(item.get("device_path")) for item in raw_candidates[:5]),
                        ],
                    )
                    return [], final_snapshot
                continue

            signature = tuple(
                sorted(
                    (
                        item.get("device_path"),
                        item.get("size"),
                        item.get("mtime"),
                    )
                    for item in candidates
                )
            )
            if signature == last_signature:
                stable_count += 1
            else:
                stable_count = 1
                last_signature = signature
            last_candidates = candidates

            if stable_count >= 2:
                return last_candidates, final_snapshot

        return last_candidates, final_snapshot

    def _resolve_local_artifact_path(self, directory: Path, basename: str):
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / basename
        if not candidate.exists():
            return candidate

        stem = candidate.stem
        suffix = candidate.suffix
        idx = 2
        while True:
            alt = directory / f"{stem}__collected_{idx:03d}{suffix}"
            if not alt.exists():
                return alt
            idx += 1

    def _resolve_download_target_file(self, filename, candidate_dirs=None):
        candidate_dirs = list(dict.fromkeys(candidate_dirs or getattr(self, "download_path_candidates", []) or [self.download_path]))

        if filename:
            for base_dir in candidate_dirs:
                candidate = f"{base_dir}/{filename}"
                try:
                    exists = get_adb_device().shell(f'if [ -f "{candidate}" ]; then echo True; else echo False; fi').strip()
                except Exception:
                    exists = "False"
                if exists == "True":
                    return candidate

        latest_candidates = []
        for base_dir in candidate_dirs:
            try:
                latest_name = get_last_N_file(base_dir, 1).strip()
            except Exception:
                latest_name = ""
            if not latest_name:
                continue
            latest_path = f"{base_dir}/{latest_name}"
            try:
                mtime = int(str(get_file_last_modified_time(latest_path)).strip())
            except Exception:
                mtime = -1
            latest_candidates.append((mtime, latest_path))

        if latest_candidates:
            latest_candidates.sort(key=lambda item: item[0], reverse=True)
            return latest_candidates[0][1]

        return None

    def _cleanup_downloaded_device_file(self, candidate, artifact_meta):
        device_path = (candidate or {}).get("device_path")
        candidate_reason = (candidate or {}).get("candidate_reason")
        sha256 = (artifact_meta or {}).get("sha256")
        cleanup_enabled = bool((getattr(self, "profile", None) or {}).get("telegram_cleanup_downloaded_device_files", True))

        if not cleanup_enabled:
            self.log_action(
                "attachment_device_cleanup",
                selector=str(device_path or ""),
                result="skipped_disabled",
                artifacts=[f"candidate_reason={candidate_reason or ''}", f"sha256={sha256 or ''}"],
            )
            return False

        if not device_path:
            self.log_action("attachment_device_cleanup", selector="", result="skipped_missing_device_path")
            return False

        if candidate_reason != "new_path":
            self.log_action(
                "attachment_device_cleanup",
                selector=device_path,
                result="skipped_not_new_path",
                artifacts=[f"candidate_reason={candidate_reason or ''}", f"sha256={sha256 or ''}"],
            )
            return False

        if not sha256:
            self.log_action(
                "attachment_device_cleanup",
                selector=device_path,
                result="skipped_missing_sha256",
                artifacts=[f"candidate_reason={candidate_reason or ''}"],
            )
            return False

        try:
            d = get_adb_device()
            quoted = self._shell_single_quote(device_path)
            d.shell(f"rm -f -- {quoted}")
            exists = d.shell(f"if [ -e {quoted} ]; then echo True; else echo False; fi").strip()
            if exists == "True":
                self.log_action(
                    "attachment_device_cleanup",
                    selector=device_path,
                    result="fail_still_exists",
                    artifacts=[f"candidate_reason={candidate_reason}", f"sha256={sha256}"],
                )
                return False

            self.log_action(
                "attachment_device_cleanup",
                selector=device_path,
                result="success",
                artifacts=[f"candidate_reason={candidate_reason}", f"sha256={sha256}", "reason=copied_and_hashed"],
            )
            return True
        except Exception as e:
            self.log_action(
                "attachment_device_cleanup",
                selector=device_path,
                result="fail",
                error=e,
                artifacts=[f"candidate_reason={candidate_reason}", f"sha256={sha256 or ''}"],
            )
            return False

    def _log_attachment_download_success(
        self,
        *,
        msg_type,
        copied_artifacts,
        candidates,
        start_ts,
        message_id,
        observation_id,
        record_id,
        identity_status,
        dedup_policy,
        display_filename,
        phase,
    ):
        dur = round(time.time() - start_ts, 3)
        artifacts = [
            f"duration={dur:.2f}s",
            f"message_id={message_id or ''}",
            f"observation_id={observation_id or ''}",
            f"record_id={record_id or ''}",
            f"identity_status={identity_status or ''}",
            f"dedup_policy={dedup_policy or ''}",
            f"display_filename={display_filename or ''}",
            f"candidate_count={len(candidates)}",
            "detection_method=before_after_snapshot_diff",
        ]
        copied_paths = []
        for idx, artifact_meta in enumerate(copied_artifacts, start=1):
            copied_paths.append(artifact_meta.get("artifact_path", ""))
            if artifact_meta.get("artifact_id"):
                artifacts.append(f"artifact_id[{idx}]={artifact_meta['artifact_id']}")
            if artifact_meta.get("device_path"):
                artifacts.append(f"device_path[{idx}]={artifact_meta['device_path']}")
            if artifact_meta.get("filename"):
                artifacts.append(f"file_name[{idx}]={artifact_meta['filename']}")
            if artifact_meta.get("size_bytes") is not None:
                artifacts.append(f"file_size[{idx}]={artifact_meta['size_bytes']}")
            if artifact_meta.get("sha256"):
                artifacts.append(f"sha256[{idx}]={artifact_meta['sha256']}")

        if len(copied_artifacts) > 1:
            self.log_action(
                'attachment_download_detected',
                selector='device_snapshot_diff',
                result='multiple_new_candidates',
                artifacts=[
                    f"message_id={message_id or ''}",
                    f"observation_id={observation_id or ''}",
                    f"display_filename={display_filename or ''}",
                    f"candidate_count={len(copied_artifacts)}",
                    f"copied_device_paths={copied_paths}",
                    "detection_method=before_after_snapshot_diff",
                ],
            )
        elif copied_artifacts[0].get("device_path"):
            self.log_action(
                'attachment_download_detected',
                selector='device_snapshot_diff',
                result='success',
                artifacts=[
                    f"message_id={message_id or ''}",
                    f"observation_id={observation_id or ''}",
                    f"display_filename={display_filename or ''}",
                    f"device_path={copied_artifacts[0].get('device_path')}",
                    f"sha256={copied_artifacts[0].get('sha256') or ''}",
                    "detection_method=before_after_snapshot_diff",
                ],
            )

        self.log_action('attachment_download_end', selector=str(msg_type), artifacts=artifacts)
        logger.info(
            "%s Attachment download done (count=%d, duration=%.2fs)",
            self.aura_prefix(phase),
            len(copied_artifacts),
            dur,
        )

    def _download_attachment(self, msg, msg_type, bounds, download_dir, message_id=None, observation_id=None, record_id=None, page_index=None, page_row_index=None, identity_status=None, dedup_policy=None, download_gate_key=None, policy_source=None, policy_version=None):
        start_ts = time.time()
        phase = getattr(self, 'current_phase', None)
        account = getattr(self, 'current_account', None)
        chat_id = getattr(self, 'current_chat_id', None)

        download_dir_path = Path(download_dir)
        download_dir_path.mkdir(parents=True, exist_ok=True)

        expected_size = msg.get('size') if isinstance(msg, dict) else None
        filename = msg.get('filename') if isinstance(msg, dict) else None
        display_filename = filename
        snapshot_dirs = self._attachment_snapshot_dirs(msg_type)
        try:
            progress_timeout = float(
                (getattr(self, "profile", None) or {}).get(
                    "telegram_attachment_progress_timeout_sec",
                    5.0,
                )
            )
        except Exception:
            progress_timeout = 5.0
        progress_timeout = max(1.0, progress_timeout)

        context = [
            f"type={msg_type}",
            f"message_id={message_id or ''}",
            f"observation_id={observation_id or ''}",
            f"record_id={record_id or ''}",
            f"download_gate_key={download_gate_key or ''}",
            f"account={account or ''}",
            f"chat_id={chat_id or ''}",
            f"page_index={page_index if page_index is not None else ''}",
            f"page_row_index={page_row_index if page_row_index is not None else ''}",
            f"bounds={bounds}",
            f"identity_status={identity_status or ''}",
            f"dedup_policy={dedup_policy or ''}",
            f"policy_source={policy_source or ''}",
            f"policy_version={policy_version or ''}",
            "detection_method=before_after_snapshot_diff",
            f"snapshot_dirs={snapshot_dirs}",
            f"progress_timeout={progress_timeout}",
        ]
        if display_filename:
            context.append(f"display_filename={display_filename}")
        if expected_size is not None:
            context.append(f"expected_size={expected_size}")

        logger.info("%s Attachment download start (%s)", self.aura_prefix(phase), ', '.join(context))
        self.log_action(
            'attachment_download_start',
            selector=str(msg_type),
            artifacts=context,
        )

        def _attempt_id():
            run_id = getattr(self, "run_id", None) or "run_unknown"
            seed = "|".join(
                [
                    str(run_id),
                    str(phase or ""),
                    str(account or ""),
                    str(chat_id or ""),
                    str(record_id or ""),
                    str(message_id or ""),
                    str(observation_id or ""),
                    str(download_gate_key or ""),
                    f"{start_ts:.6f}",
                ]
            )
            return hashlib.sha256(seed.encode("utf-8")).hexdigest()

        def _record_attachment_attempt(
            status: str,
            *,
            failure_reason: str | None = None,
            error: Exception | None = None,
            copied_artifacts: list[dict] | None = None,
            screenshot_path: str | None = None,
            uitree_path: str | None = None,
        ):
            copied_artifacts = copied_artifacts or []
            ended_ts = time.time()
            try:
                self.storage.upsert_attachment_attempt(
                    {
                        "run_id": getattr(self, "run_id", None) or "run_unknown",
                        "attempt_id": _attempt_id(),
                        "app": self.app_id,
                        "phase": phase,
                        "account": account,
                        "chat_id": chat_id,
                        "record_id": record_id,
                        "message_id": message_id,
                        "observation_id": observation_id,
                        "message_type": msg_type,
                        "display_filename": display_filename,
                        "status": status,
                        "failure_reason": failure_reason,
                        "error": error,
                        "started_ts": start_ts,
                        "ended_ts": ended_ts,
                        "duration_sec": round(ended_ts - start_ts, 3),
                        "bounds": bounds,
                        "snapshot_dirs": snapshot_dirs,
                        "download_detection_method": "before_after_snapshot_diff",
                        "artifact_paths": [item.get("artifact_path") for item in copied_artifacts if item.get("artifact_path")],
                        "sha256_list": [item.get("sha256") for item in copied_artifacts if item.get("sha256")],
                        "device_paths": [item.get("device_path") for item in copied_artifacts if item.get("device_path")],
                        "screenshot_path": screenshot_path,
                        "uitree_path": uitree_path,
                        "identity_status": identity_status,
                        "dedup_policy": dedup_policy,
                        "download_gate_key": download_gate_key,
                        "policy_source": policy_source,
                        "policy_version": policy_version,
                        "created_ts": ended_ts,
                    }
                )
            except Exception as e:
                self.log_action(
                    "attachment_attempt_storage",
                    selector=str(msg_type),
                    result="fail",
                    error=e,
                    artifacts=[
                        f"message_id={message_id or ''}",
                        f"observation_id={observation_id or ''}",
                        f"status={status}",
                        f"failure_reason={failure_reason or ''}",
                    ],
                )

        def _calc_download_button_loc(b):
            x = (b['right'] - 5)
            y = (b['top'] + b['bottom']) // 2
            return x, y

        def _build_download_artifact(dest_path: Path, kind: str, *, device_meta: dict | None = None):
            device_meta = device_meta or {}
            artifact_meta = self.register_artifact(
                dest_path,
                kind=kind,
                message_id=message_id,
                chat_id=chat_id,
                account=account,
                record_id=record_id,
                observation_id=observation_id,
                message_type=msg_type,
                file_name=dest_path.name,
                identity_status=identity_status,
                dedup_policy=dedup_policy,
                download_gate_key=download_gate_key,
                policy_source=policy_source,
                policy_version=policy_version,
                display_filename=display_filename,
                device_path=device_meta.get("device_path"),
                device_basename=device_meta.get("basename"),
                collected_path=str(dest_path.resolve()),
                download_detection_method="before_after_snapshot_diff",
                download_action_started_at=start_ts,
                device_file_size=device_meta.get("size"),
                device_file_mtime=device_meta.get("mtime"),
            )

            sha256 = None
            size_bytes = None
            try:
                sha256 = sha256_file(dest_path)
                size_bytes = int(dest_path.stat().st_size)
            except Exception as e:
                self.log_action(
                    'artifact_hash',
                    selector=str(dest_path),
                    result='fail',
                    error=e,
                )

            if artifact_meta:
                artifact_meta["sha256"] = sha256
                artifact_meta["size_bytes"] = size_bytes
                artifact_meta["content_group_id"] = sha256
                try:
                    self.storage.update_file_artifact_hash(
                        artifact_meta.get("run_id"),
                        artifact_meta.get("artifact_path"),
                        sha256,
                        size_bytes=size_bytes,
                    )
                except Exception as e:
                    self.log_action(
                        'artifact_hash',
                        selector=str(dest_path),
                        result='fail',
                        error=e,
                        artifacts=['storage_update'],
                    )
                artifact_meta["filename"] = dest_path.name
                return artifact_meta

            return {
                "artifact_id": f"{getattr(self, 'run_id', 'run_unknown')}:{str(dest_path.resolve())}",
                "record_id": record_id,
                "message_id": message_id,
                "observation_id": observation_id,
                "artifact_path": str(dest_path.resolve()),
                "artifact_kind": kind,
                "message_type": msg_type,
                "identity_status": identity_status,
                "dedup_policy": dedup_policy,
                "download_gate_key": download_gate_key,
                "policy_source": policy_source,
                "policy_version": policy_version,
                "display_filename": display_filename,
                "device_path": device_meta.get("device_path"),
                "device_basename": device_meta.get("basename"),
                "collected_path": str(dest_path.resolve()),
                "download_detection_method": "before_after_snapshot_diff",
                "download_action_started_at": start_ts,
                "device_file_size": device_meta.get("size"),
                "device_file_mtime": device_meta.get("mtime"),
                "sha256": sha256,
                "content_group_id": sha256,
                "size_bytes": size_bytes,
                "filename": dest_path.name,
            }

        def _copy_download_candidates(candidates, *, kind: str):
            copied = []
            for candidate in candidates:
                device_path = candidate.get("device_path")
                device_basename = candidate.get("basename") or Path(device_path or "").name
                if not device_path or not device_basename:
                    continue
                dest_path = self._resolve_local_artifact_path(download_dir_path, device_basename)
                download_file(device_path, str(dest_path))
                artifact_meta = _build_download_artifact(dest_path, kind=kind, device_meta=candidate)
                copied.append(artifact_meta)
                self._cleanup_downloaded_device_file(candidate, artifact_meta)
            return copied

        def _fail(reason: str, *, error: Exception | None = None, extra: list[str] | None = None):
            dur = round(time.time() - start_ts, 3)
            loading_state = _loading_dialog_state()
            artifacts = [
                f"reason={reason}",
                f"duration={dur:.2f}s",
                f"message_id={message_id or ''}",
                f"observation_id={observation_id or ''}",
                f"record_id={record_id or ''}",
                f"identity_status={identity_status or ''}",
                f"dedup_policy={dedup_policy or ''}",
                f"loading_dialog_visible={loading_state.get('visible')}",
                f"loading_dialog_percent={loading_state.get('percent') if loading_state.get('percent') is not None else ''}",
            ]
            if extra:
                artifacts.extend(extra)
            # Capture evidence at the point of failure for debugging.
            screenshot_path = None
            uitree_path = None
            try:
                attempt_id = _attempt_id()
                shot = download_dir_path / f"{attempt_id}_{msg_type}_attachment_fail.jpg"
                ev = self.capture_visual_evidence(
                    shot,
                    screenshot_kind='screenshot_attachment_fail',
                    uitree_kind='uitree_attachment_fail',
                    message_id=message_id,
                    chat_id=chat_id,
                    account=account,
                    record_id=record_id,
                    observation_id=observation_id,
                    message_type=msg_type,
                    display_filename=display_filename,
                    identity_status=identity_status,
                    dedup_policy=dedup_policy,
                    download_gate_key=download_gate_key,
                    policy_source=policy_source,
                    policy_version=policy_version,
                    download_detection_method="attachment_failure_evidence",
                    download_action_started_at=start_ts,
                )
                screenshot_path = ev.get('screenshot_path')
                uitree_path = ev.get('uitree_path')
                artifacts.append(screenshot_path or '')
                if uitree_path:
                    artifacts.append(uitree_path)
            except Exception:
                pass

            _record_attachment_attempt(
                "failed",
                failure_reason=reason,
                error=error,
                screenshot_path=screenshot_path,
                uitree_path=uitree_path,
            )

            self.log_action(
                'attachment_download_end',
                selector=str(msg_type),
                result='fail',
                error=error,
                artifacts=artifacts,
            )
            logger.warning("%s Attachment download failed (%s)", self.aura_prefix(phase), ', '.join(artifacts))
            _close_loading_dialog_if_visible(f"attachment_fail:{reason}")
            return None

        def _is_chatroom_screen() -> bool:
            try:
                return bool(self._is_telegram_chatroom_screen())
            except Exception:
                return False

        def _is_loading_dialog() -> bool:
            try:
                return bool(self._is_telegram_attachment_loading_dialog())
            except Exception:
                return False

        def _loading_dialog_state() -> dict:
            try:
                return self._telegram_attachment_loading_dialog_state()
            except Exception:
                return {"visible": False}

        def _press_back_after_attachment_action(reason: str, capture_on_timeout: bool = False) -> bool:
            try:
                self.device.press('back')
                self.log_action('press', selector='back', artifacts=[reason])
            except Exception as e:
                self.log_action('press', selector='back', result='fail', error=e, artifacts=[reason])
                return False

            return self.wait_for_screen_state(
                'telegram_chatroom_screen',
                _is_chatroom_screen,
                timeout=1.5,
                interval=0.2,
                capture_on_timeout=capture_on_timeout,
            )

        def _close_loading_dialog_if_visible(reason: str) -> bool:
            state = _loading_dialog_state()
            if not state.get("visible"):
                return False

            self.log_action(
                "attachment_loading_dialog",
                selector=reason,
                result="visible",
                artifacts=[
                    f"message_id={message_id or ''}",
                    f"observation_id={observation_id or ''}",
                    f"percent={state.get('percent') if state.get('percent') is not None else ''}",
                    "download_result_determined_by=device_snapshot_diff",
                ],
            )
            return _press_back_after_attachment_action(reason, capture_on_timeout=True)

        def _dismiss_attachment_menu_if_open() -> bool:
            labels = (
                'Save to Gallery',
                'Save to Downloads',
                'Save to downloads',
                'Save to Download',
                'Save to download',
            )
            menu_visible = False
            try:
                menu_visible = any(self.device(text=label).exists() for label in labels)
            except Exception:
                menu_visible = False

            if not menu_visible:
                return False

            return _press_back_after_attachment_action('attachment_menu_dismiss')

        def _is_any_menu_label_visible(labels: tuple[str, ...]) -> bool:
            try:
                return any(self.device(text=label).exists() for label in labels)
            except Exception:
                return False

        def _open_attachment_action_menu(x: int, y: int, labels: tuple[str, ...], attempts: int = 3) -> bool:
            for attempt in range(1, attempts + 1):
                self.device.click(x, y)
                self.log_action(
                    'click',
                    selector='attachment_action_hotspot',
                    artifacts=[f'x={x}', f'y={y}', f'attempt={attempt}'],
                )

                if self.wait_for_screen_state(
                    'attachment_action_menu',
                    lambda: _is_any_menu_label_visible(labels),
                    timeout=1.5,
                    interval=0.2,
                    capture_on_timeout=False,
                ):
                    return True

                if attempt < attempts:
                    if not (_is_chatroom_screen() or _is_loading_dialog()):
                        _press_back_after_attachment_action(
                            f'attachment_action_retry_reset attempt={attempt}',
                            capture_on_timeout=False,
                        )

            if not (_is_chatroom_screen() or _is_loading_dialog()):
                _press_back_after_attachment_action(
                    'attachment_action_retry_reset_final',
                    capture_on_timeout=True,
                )

            return False

        try:
            if msg_type in ('Photo', 'Video'):
                x, y = _calc_download_button_loc(bounds)
                gallery_labels = ('Save to Gallery',)

                if not _open_attachment_action_menu(x, y, gallery_labels):
                    _dismiss_attachment_menu_if_open()
                    return _fail('save_to_gallery_not_found')

                before_snapshot = self._snapshot_device_files(snapshot_dirs)
                self.device(text='Save to Gallery').click()
                self.log_action('click', selector='text=Save to Gallery')

                candidates, _ = self._wait_for_download_candidates(
                    before_snapshot,
                    snapshot_dirs,
                    timeout=15.0 if msg_type == "Photo" else 30.0,
                    interval=0.5,
                    expected_size=expected_size,
                    progress_timeout=progress_timeout,
                )
                if not candidates:
                    matched_existing = self._resolve_download_target_file(display_filename, snapshot_dirs)
                    return _fail(
                        'no_new_device_file_detected',
                        extra=[
                            f"display_filename={display_filename or ''}",
                            f"candidate_count=0",
                            f"matched_existing_device_path={matched_existing or ''}",
                            "detection_method=before_after_snapshot_diff",
                        ],
                    )

                copied_artifacts = _copy_download_candidates(candidates, kind=f"attachment_{msg_type.lower()}")
                if not copied_artifacts:
                    return _fail(
                        'download_copy_failed',
                        extra=[
                            f"display_filename={display_filename or ''}",
                            f"candidate_count={len(candidates)}",
                            "detection_method=before_after_snapshot_diff",
                        ],
                    )

                self._log_attachment_download_success(
                    msg_type=msg_type,
                    copied_artifacts=copied_artifacts,
                    candidates=candidates,
                    start_ts=start_ts,
                    message_id=message_id,
                    observation_id=observation_id,
                    record_id=record_id,
                    identity_status=identity_status,
                    dedup_policy=dedup_policy,
                    display_filename=display_filename,
                    phase=phase,
                )
                _record_attachment_attempt("success", copied_artifacts=copied_artifacts)
                _close_loading_dialog_if_visible("attachment_success")
                return copied_artifacts

            if msg_type == 'File':
                x, y = _calc_download_button_loc(bounds)
                download_labels = (
                    'Save to Downloads',
                    'Save to downloads',
                    'Save to Download',
                    'Save to download',
                )

                if not _open_attachment_action_menu(x, y, download_labels):
                    _dismiss_attachment_menu_if_open()
                    return _fail('save_to_downloads_not_found')

                def _click_save_to_downloads() -> bool:
                    for label in download_labels:
                        try:
                            node = self.device(text=label)
                            if node.exists:
                                node.click()
                                self.log_action('click', selector=f'text={label}')
                                self.wait_for_screen_state(
                                    'attachment_action_menu_closed',
                                    lambda: not _is_any_menu_label_visible(download_labels),
                                    timeout=1.0,
                                    interval=0.2,
                                    capture_on_timeout=False,
                                )
                                return True
                        except Exception as e:
                            self.log_action('click', selector=f'text={label}', result='fail', error=e)

                    try:
                        node = self.device(textMatches=r'(?i).*save.*downloads.*')
                        if node.exists:
                            node.click()
                            self.log_action('click', selector='textMatches=(?i).*save.*downloads.*')
                            self.wait_for_screen_state(
                                'attachment_action_menu_closed',
                                lambda: not _is_any_menu_label_visible(download_labels),
                                timeout=1.0,
                                interval=0.2,
                                capture_on_timeout=False,
                            )
                            return True
                    except Exception as e:
                        self.log_action('click', selector='textMatches=(?i).*save.*downloads.*', result='fail', error=e)

                    return False

                before_snapshot = self._snapshot_device_files(snapshot_dirs)
                if not _click_save_to_downloads():
                    _dismiss_attachment_menu_if_open()
                    return _fail('save_to_downloads_not_found')

                candidates, _ = self._wait_for_download_candidates(
                    before_snapshot,
                    snapshot_dirs,
                    timeout=30.0,
                    interval=0.5,
                    expected_size=expected_size,
                    progress_timeout=progress_timeout,
                )
                if not candidates:
                    matched_existing = self._resolve_download_target_file(display_filename, snapshot_dirs)
                    return _fail(
                        'no_new_device_file_detected',
                        extra=[
                            f"display_filename={display_filename or ''}",
                            f"candidate_count=0",
                            f"matched_existing_device_path={matched_existing or ''}",
                            "detection_method=before_after_snapshot_diff",
                        ],
                    )

                copied_artifacts = _copy_download_candidates(candidates, kind='attachment_file')
                if not copied_artifacts:
                    return _fail(
                        'download_copy_failed',
                        extra=[
                            f"display_filename={display_filename or ''}",
                            f"candidate_count={len(candidates)}",
                            "detection_method=before_after_snapshot_diff",
                        ],
                    )

                self._log_attachment_download_success(
                    msg_type=msg_type,
                    copied_artifacts=copied_artifacts,
                    candidates=candidates,
                    start_ts=start_ts,
                    message_id=message_id,
                    observation_id=observation_id,
                    record_id=record_id,
                    identity_status=identity_status,
                    dedup_policy=dedup_policy,
                    display_filename=display_filename,
                    phase=phase,
                )
                _record_attachment_attempt("success", copied_artifacts=copied_artifacts)
                _close_loading_dialog_if_visible("attachment_success")
                return copied_artifacts

            return _fail('unsupported_type')

        except Exception as e:
            logger.exception("%s Attachment download exception", self.aura_prefix(phase))
            return _fail('exception', error=e)
