import time
import inspect
import logging
import hashlib
from io import BytesIO
from pathlib import Path
import uiautomator2 as u2
from abc import ABC, abstractmethod
from utils.utils import get_audit_max_seq, write_audit
from utils.storage import SQLiteStorage
from utils.evidence import sha256_file

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    def __init__(self, device=None, package_name: str | None = None, audit_log_path: str = "AURA_audit.log", artifact_dir: str = ".", db_path: str | None = None):
        self.device = device or u2.connect()
        self.packageName = package_name
        self.package_name = package_name
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        audit_path = Path(audit_log_path)
        self.audit_log_path = audit_path if audit_path.is_absolute() else self.artifact_dir.joinpath(audit_path)
        default_db_root = self.artifact_dir if self.artifact_dir.name == "" else self.artifact_dir.parent
        default_db = default_db_root / "aura.db"
        db_file = Path(db_path) if db_path else default_db
        self.app_id = self.packageName or "unknown"
        self.storage = SQLiteStorage(db_file)
        self.audit_seq = get_audit_max_seq(self.audit_log_path)
        self._artifact_hash_queue = []
        self._artifact_hash_seen = set()

    def launch_app(self, reason: str | None = None):
        pkg = self.packageName
        if not pkg:
            return
        sel = pkg if not reason else f"{pkg} reason={reason}"
        try:
            self.device.app_start(pkg)
            self.log_action("app_start", selector=sel)
        except Exception as e:
            self.log_action("app_start", selector=sel, result="fail", error=e)
            raise
        self._sleep(1.0)

    def _sleep(self, seconds: float | None = None, *, key: str | None = None):
        profile = getattr(self, "profile", None) or {}
        force_one = bool(profile.get("sleep_force_one_sec", False))
        default_sec = float(profile.get("sleep_default_sec", 1.0))
        min_sec = float(profile.get("sleep_min_sec", 0.0))

        if force_one:
            delay = 1.0
        else:
            if key:
                key_name = f"sleep_{key}_sec"
                if key_name in profile:
                    delay = float(profile.get(key_name, default_sec))
                else:
                    delay = default_sec if seconds is None else float(seconds)
            else:
                delay = default_sec if seconds is None else float(seconds)
            if delay < min_sec:
                delay = min_sec

        time.sleep(max(0.0, delay))


    def _screen_hash(self) -> str:
        try:
            shot = self.device.screenshot()
            if hasattr(shot, "save"):
                bio = BytesIO()
                shot.save(bio, format="PNG")
                data = bio.getvalue()
            elif isinstance(shot, (bytes, bytearray)):
                data = bytes(shot)
            else:
                return ""
            return hashlib.sha1(data).hexdigest()
        except Exception:
            return ""

    def wait_for_visual_stable(
        self,
        selector: str,
        *,
        predicate=None,
        timeout: float = 1.4,
        stable_polls: int = 2,
        interval: float = 0.18,
    ) -> bool:
        started_ts = time.time()
        deadline = time.time() + max(0.2, float(timeout))
        prev_hash = None
        stable = 0
        last_hash = ""
        attempts = 0

        while time.time() < deadline:
            attempts += 1
            if predicate is not None and not predicate():
                stable = 0
                prev_hash = None
                self._sleep(interval)
                continue

            cur_hash = self._screen_hash()
            last_hash = cur_hash or last_hash
            if cur_hash and prev_hash and cur_hash == prev_hash:
                stable += 1
                if stable >= max(1, int(stable_polls)):
                    self.log_action(
                        "screen_settled",
                        selector=selector,
                        artifacts=[
                            f"stable_polls={stable}",
                            f"interval={interval:.2f}s",
                            f"attempts={attempts}",
                            f"elapsed={time.time() - started_ts:.2f}s",
                        ],
                    )
                    return True
            else:
                stable = 0
            prev_hash = cur_hash or prev_hash
            self._sleep(interval)

        self.log_action(
            "screen_settled",
            selector=selector,
            result="timeout",
            artifacts=[
                f"stable_polls={stable}",
                f"last_hash={last_hash[:8] if last_hash else ''}",
                f"attempts={attempts}",
                f"elapsed={time.time() - started_ts:.2f}s",
                f"timeout={timeout:.2f}s",
            ],
        )
        return False

    def wait_for_consecutive_match(
        self,
        *,
        action: str,
        selector: str,
        sample_fn,
        match_fn,
        timeout: float,
        stable_polls: int,
        interval: float,
        success_result: str = "stable",
        timeout_result: str = "timeout",
        success_artifacts_fn=None,
        timeout_artifacts_fn=None,
        on_poll=None,
    ):
        deadline = time.time() + max(0.2, float(timeout))
        stable_needed = max(1, int(stable_polls))
        stable_count = 0
        last_sample = None

        while time.time() < deadline:
            if on_poll is not None:
                try:
                    on_poll()
                except Exception:
                    pass

            sample = sample_fn()
            last_sample = sample
            if bool(match_fn(sample)):
                stable_count += 1
                if stable_count >= stable_needed:
                    artifacts = []
                    if success_artifacts_fn is not None:
                        try:
                            artifacts = list(success_artifacts_fn(sample, stable_count) or [])
                        except Exception:
                            artifacts = []
                    artifacts.extend(
                        [
                            f"stable_count={stable_count}",
                            f"required={stable_needed}",
                            f"elapsed={time.time() - (deadline - max(0.2, float(timeout))):.2f}s",
                        ]
                    )
                    self.log_action(action, selector=selector, result=success_result, artifacts=artifacts)
                    return True, sample, stable_count
            else:
                stable_count = 0
            self._sleep(interval)

        artifacts = []
        if timeout_artifacts_fn is not None:
            try:
                artifacts = list(timeout_artifacts_fn(last_sample, stable_count) or [])
            except Exception:
                artifacts = []
        artifacts.extend([f"stable_count={stable_count}", f"required={stable_needed}", f"timeout={timeout:.2f}s"])
        self.log_action(action, selector=selector, result=timeout_result, artifacts=artifacts)
        return False, last_sample, stable_count

    def wait_for_consecutive_same_sample(
        self,
        *,
        action: str,
        selector: str,
        sample_fn,
        timeout: float,
        stable_polls: int,
        interval: float,
        valid_fn=None,
        success_result: str = "stable",
        timeout_result: str = "timeout",
        success_artifacts_fn=None,
        timeout_artifacts_fn=None,
        on_poll=None,
    ):
        deadline = time.time() + max(0.2, float(timeout))
        stable_needed = max(1, int(stable_polls))
        stable_count = 0
        last_sample = None

        while time.time() < deadline:
            if on_poll is not None:
                try:
                    on_poll()
                except Exception:
                    pass

            sample = sample_fn()
            is_valid = True if valid_fn is None else bool(valid_fn(sample))
            if is_valid:
                if sample == last_sample:
                    stable_count += 1
                else:
                    stable_count = 1
                    last_sample = sample
                if stable_count >= stable_needed:
                    artifacts = []
                    if success_artifacts_fn is not None:
                        try:
                            artifacts = list(success_artifacts_fn(sample, stable_count) or [])
                        except Exception:
                            artifacts = []
                    artifacts.extend(
                        [
                            f"stable_count={stable_count}",
                            f"required={stable_needed}",
                            f"elapsed={time.time() - (deadline - max(0.2, float(timeout))):.2f}s",
                        ]
                    )
                    self.log_action(action, selector=selector, result=success_result, artifacts=artifacts)
                    return True, sample, stable_count
            else:
                stable_count = 0
                last_sample = sample
            self._sleep(interval)

        artifacts = []
        if timeout_artifacts_fn is not None:
            try:
                artifacts = list(timeout_artifacts_fn(last_sample, stable_count) or [])
            except Exception:
                artifacts = []
        artifacts.extend([f"stable_count={stable_count}", f"required={stable_needed}", f"timeout={timeout:.2f}s"])
        self.log_action(action, selector=selector, result=timeout_result, artifacts=artifacts)
        return False, last_sample, stable_count

    def press_back_to_state(
        self,
        *,
        reason: str,
        state_name: str,
        predicate,
        max_back: int = 3,
        timeout: float = 2.0,
        interval: float | None = None,
        on_success=None,
    ) -> bool:
        if predicate():
            if on_success is not None:
                try:
                    on_success()
                except Exception as e:
                    self.log_action("chat_list_return", selector=reason, result="post_success_fail", error=e)
                    return False
            self.log_action("chat_list_return", selector=reason, result="already_on_chat_list")
            return True

        for attempt in range(1, max_back + 1):
            try:
                self.device.press("back")
                self.log_action("press", selector="back", artifacts=[f"reason={reason}", f"attempt={attempt}"])
            except Exception as e:
                self.log_action("press", selector="back", result="fail", error=e, artifacts=[f"reason={reason}", f"attempt={attempt}"])
                return False

            if self.wait_for_screen_state(
                state_name,
                predicate,
                timeout=timeout,
                interval=interval,
                capture_on_timeout=attempt == max_back,
            ):
                if on_success is not None:
                    try:
                        on_success()
                    except Exception as e:
                        self.log_action("chat_list_return", selector=reason, result="post_success_fail", error=e, artifacts=[f"attempt={attempt}"])
                        return False
                self.log_action("chat_list_return", selector=reason, result="success", artifacts=[f"attempt={attempt}"])
                return True

        self.log_action("chat_list_return", selector=reason, result="fail", artifacts=[f"max_back={max_back}"])
        return False

    def log_screen_transition(self, selector: str, *, result: str = "success", artifacts=None, error: Exception | None = None):
        self.log_action("screen_transition", selector=selector, result=result, artifacts=artifacts, error=error)

    def wait_for_screen_state(
        self,
        name: str,
        predicate,
        timeout: float | None = None,
        interval: float | None = None,
        capture_on_timeout: bool = True,
    ) -> bool:
        profile = getattr(self, "profile", None) or {}
        wait_timeout = float(profile.get("screen_state_timeout_sec", 3.0)) if timeout is None else float(timeout)
        wait_interval = float(profile.get("screen_state_interval_sec", 0.3)) if interval is None else float(interval)
        started_ts = time.time()
        end_ts = time.time() + max(0.0, wait_timeout)
        last_error = None
        attempts = 0

        logger.debug(
            "wait_for_screen_state start: name=%s timeout=%.2fs interval=%.2fs capture_on_timeout=%s phase=%s account=%s chat_id=%s",
            name,
            wait_timeout,
            wait_interval,
            capture_on_timeout,
            getattr(self, "current_phase", None),
            getattr(self, "current_account", None),
            getattr(self, "current_chat_id", None),
        )

        while time.time() <= end_ts:
            attempts += 1
            try:
                matched = bool(predicate())
                logger.debug(
                    "wait_for_screen_state poll: name=%s attempt=%d elapsed=%.2fs matched=%s",
                    name,
                    attempts,
                    time.time() - started_ts,
                    matched,
                )
                if matched:
                    self.log_action(
                        "screen_state",
                        selector=name,
                        result="confirmed",
                        artifacts=[
                            f"attempts={attempts}",
                            f"elapsed={time.time() - started_ts:.2f}s",
                            f"timeout={wait_timeout:.2f}s",
                        ],
                    )
                    logger.debug(
                        "wait_for_screen_state confirmed: name=%s attempts=%d elapsed=%.2fs",
                        name,
                        attempts,
                        time.time() - started_ts,
                    )
                    return True
            except Exception as e:
                last_error = e
                logger.debug(
                    "wait_for_screen_state predicate_error: name=%s attempt=%d elapsed=%.2fs error=%r",
                    name,
                    attempts,
                    time.time() - started_ts,
                    e,
                )

            self._sleep(max(0.0, wait_interval))

        artifacts = []
        if capture_on_timeout:
            try:
                safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(name))[:80]
                if not safe_name:
                    safe_name = "screen_state"
                evidence_dir = self.artifact_dir / "_screen_state_timeouts"
                screenshot_path = evidence_dir / f"{int(time.time() * 1000)}_{safe_name}.jpg"
                logger.debug(
                    "wait_for_screen_state timeout_capture: name=%s screenshot=%s",
                    name,
                    screenshot_path,
                )
                evidence = self.capture_visual_evidence(
                    screenshot_path,
                    screenshot_kind="screenshot_screen_state_timeout",
                    uitree_kind="uitree_screen_state_timeout",
                    dump_uitree=True,
                )
                artifacts.extend(
                    str(value)
                    for key, value in evidence.items()
                    if key.endswith("_path") and value
                )
            except Exception as e:
                artifacts.append(f"capture_error={e}")
                logger.debug(
                    "wait_for_screen_state capture_error: name=%s error=%r",
                    name,
                    e,
                )

        self.log_action(
            "screen_state",
            selector=name,
            result="timeout",
            error=last_error,
            artifacts=artifacts
            + [
                f"attempts={attempts}",
                f"elapsed={time.time() - started_ts:.2f}s",
                f"timeout={wait_timeout:.2f}s",
            ],
        )
        logger.debug(
            "wait_for_screen_state timeout: name=%s attempts=%d elapsed=%.2fs last_error=%r artifacts=%s",
            name,
            attempts,
            time.time() - started_ts,
            last_error,
            artifacts,
        )
        return False

    def wait_for_list_changed(
        self,
        name: str,
        before_signature,
        signature_fn,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> bool:
        profile = getattr(self, "profile", None) or {}
        wait_timeout = float(profile.get("list_changed_timeout_sec", 1.5)) if timeout is None else float(timeout)
        wait_interval = float(profile.get("list_changed_interval_sec", profile.get("screen_state_interval_sec", 0.2))) if interval is None else float(interval)
        started_ts = time.time()
        end_ts = time.time() + max(0.0, wait_timeout)
        last_error = None
        attempts = 0

        logger.debug(
            "wait_for_list_changed start: name=%s timeout=%.2fs interval=%.2fs phase=%s account=%s chat_id=%s",
            name,
            wait_timeout,
            wait_interval,
            getattr(self, "current_phase", None),
            getattr(self, "current_account", None),
            getattr(self, "current_chat_id", None),
        )

        while time.time() <= end_ts:
            attempts += 1
            try:
                current_signature = signature_fn()
                changed = current_signature != before_signature
                logger.debug(
                    "wait_for_list_changed poll: name=%s attempt=%d elapsed=%.2fs changed=%s",
                    name,
                    attempts,
                    time.time() - started_ts,
                    changed,
                )
                if changed:
                    self.log_action(
                        "list_state",
                        selector=name,
                        result="changed",
                        artifacts=[
                            f"attempts={attempts}",
                            f"elapsed={time.time() - started_ts:.2f}s",
                        ],
                    )
                    return True
            except Exception as e:
                last_error = e
                logger.debug(
                    "wait_for_list_changed signature_error: name=%s attempt=%d elapsed=%.2fs error=%r",
                    name,
                    attempts,
                    time.time() - started_ts,
                    e,
                )

            self._sleep(max(0.0, wait_interval))

        self.log_action(
            "list_state",
            selector=name,
            result="unchanged_timeout",
            error=last_error,
            artifacts=[
                f"attempts={attempts}",
                f"elapsed={time.time() - started_ts:.2f}s",
            ],
        )
        return False

    def safe_click(
        self,
        action_name: str,
        click_fn,
        expected_state_name: str | None = None,
        expected_predicate=None,
        timeout: float | None = None,
        recovery_fn=None,
        settle_sec: float = 0.2,
    ) -> bool:
        started_ts = time.time()
        logger.debug(
            "safe_click start: action=%s expected_state=%s timeout=%s settle=%.2fs has_recovery=%s phase=%s account=%s chat_id=%s",
            action_name,
            expected_state_name or action_name,
            timeout,
            settle_sec,
            recovery_fn is not None,
            getattr(self, "current_phase", None),
            getattr(self, "current_account", None),
            getattr(self, "current_chat_id", None),
        )
        self.log_action("click_attempt", selector=action_name)

        try:
            click_fn()
            logger.debug("safe_click invoked click_fn: action=%s", action_name)
        except Exception as e:
            self.log_action("click", selector=action_name, result="failed_exception", error=e)
            logger.debug(
                "safe_click click_exception: action=%s elapsed=%.2fs error=%r",
                action_name,
                time.time() - started_ts,
                e,
            )
            return False

        self._sleep(settle_sec)
        logger.debug("safe_click settle_done: action=%s elapsed=%.2fs", action_name, time.time() - started_ts)

        if expected_predicate is None:
            self.log_action(
                "click",
                selector=action_name,
                result="done_no_state_check",
                artifacts=[f"elapsed={time.time() - started_ts:.2f}s", f"settle={settle_sec:.2f}s"],
            )
            logger.debug("safe_click done_no_state_check: action=%s elapsed=%.2fs", action_name, time.time() - started_ts)
            return True

        state_name = expected_state_name or action_name
        if self.wait_for_screen_state(state_name, expected_predicate, timeout=timeout):
            self.log_action(
                "click",
                selector=action_name,
                result="success",
                artifacts=[
                    f"expected_state={state_name}",
                    f"timeout={timeout if timeout is not None else 'default'}",
                    f"elapsed={time.time() - started_ts:.2f}s",
                ],
            )
            logger.debug(
                "safe_click success: action=%s state=%s elapsed=%.2fs",
                action_name,
                state_name,
                time.time() - started_ts,
            )
            return True

        self.log_action(
            "click",
            selector=action_name,
            result="unexpected_state",
            artifacts=[
                f"expected_state={state_name}",
                f"timeout={timeout if timeout is not None else 'default'}",
                f"elapsed={time.time() - started_ts:.2f}s",
            ],
        )
        logger.debug(
            "safe_click unexpected_state: action=%s state=%s elapsed=%.2fs",
            action_name,
            state_name,
            time.time() - started_ts,
        )

        if recovery_fn is None:
            return False

        try:
            logger.debug("safe_click recovery_start: action=%s elapsed=%.2fs", action_name, time.time() - started_ts)
            recovered = bool(recovery_fn())
        except Exception as e:
            self.log_action("recovery", selector=action_name, result="failed_exception", error=e)
            logger.debug(
                "safe_click recovery_exception: action=%s elapsed=%.2fs error=%r",
                action_name,
                time.time() - started_ts,
                e,
            )
            return False

        self.log_action(
            "recovery",
            selector=action_name,
            result="success" if recovered else "fail",
            artifacts=[f"elapsed={time.time() - started_ts:.2f}s"],
        )
        logger.debug(
            "safe_click recovery_done: action=%s recovered=%s elapsed=%.2fs",
            action_name,
            recovered,
            time.time() - started_ts,
        )
        return recovered

    def log_primitive_boundary(self, primitive: str, stage: str, *, selector=None, result: str = "success", artifacts=None, error: Exception | None = None):
        primitive_name = str(primitive or "").strip() or "unknown"
        stage_name = str(stage or "").strip() or "event"
        action = f"primitive_{stage_name}"
        context = [f"primitive={primitive_name}", f"stage={stage_name}"]
        self.log_action(action, selector=selector, result=result, artifacts=context + list(artifacts or []), error=error)

    def log_route_checkpoint(self, checkpoint: str, *, result: str = "success", artifacts=None, error: Exception | None = None):
        self.log_action("route_checkpoint", selector=checkpoint, result=result, artifacts=list(artifacts or []), error=error)

    def log_action(self, action: str, selector=None, result: str = "success", error: Exception | None = None, side_effect=None, artifacts=None):
        self.audit_seq += 1
        caller_func = None
        caller_class = None
        frame = inspect.currentframe()
        try:
            caller = frame.f_back if frame else None
            if caller:
                caller_func = caller.f_code.co_name
                owner = caller.f_locals.get("self")
                if owner is not None:
                    caller_class = owner.__class__.__name__
        finally:
            del frame
        return write_audit(
            log_path=self.audit_log_path,
            package_name=self.packageName,
            action=action,
            selector=selector,
            result=result,
            error=error,
            artifacts=artifacts,
            side_effect=side_effect,
            seq=self.audit_seq,
            run_id=getattr(self, "run_id", None),
            phase=getattr(self, "current_phase", None),
            account=getattr(self, "current_account", None),
            chat_id=getattr(self, "current_chat_id", None),
            source_func=caller_func,
            source_class=caller_class,
        )

    def aura_prefix(self, phase: str | None = None) -> str:
        profile = getattr(self, "profile", None) or {}
        app = (profile.get("app_name") or "").strip() or (self.app_id or self.packageName or "unknown")
        ph = (phase or getattr(self, "current_phase", None) or "setup")
        return f"[AURA][{app}][{ph}]"

    def register_artifact(self, path, kind: str, account=None, chat_id=None, message_id=None, **metadata):
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None

        abs_path = str(p.resolve())
        run_id = getattr(self, "run_id", None) or getattr(self.artifact_dir, "name", None) or "run_unknown"
        payload = {
            "run_id": run_id,
            "artifact_id": f"{run_id}:{abs_path}",
            "app": self.app_id,
            "phase": getattr(self, "current_phase", None),
            "account": account if account is not None else getattr(self, "current_account", None),
            "chat_id": chat_id if chat_id is not None else getattr(self, "current_chat_id", None),
            "message_id": message_id if message_id is not None else getattr(self, "current_message_id", None),
            "artifact_path": abs_path,
            "artifact_kind": kind,
            "sha256": None,
            "size_bytes": int(p.stat().st_size),
            "created_ts": time.time(),
        }
        payload.update(metadata or {})
        self.storage.upsert_file_artifact(payload)
        self.log_action(
            "artifact_context_registered",
            selector=kind,
            artifacts=[
                f"path={abs_path}",
                f"kind={kind}",
                f"phase={payload.get('phase') or ''}",
                f"account={payload.get('account') or ''}",
                f"chat_id={payload.get('chat_id') or ''}",
                f"message_id={payload.get('message_id') or ''}",
                f"observation_id={payload.get('observation_id') or ''}",
                f"source_action={payload.get('source_action') or ''}",
                f"source_screen={payload.get('source_screen') or ''}",
                f"size_bytes={payload.get('size_bytes') or ''}",
            ],
        )
        key = (payload.get("run_id"), abs_path)
        if key not in self._artifact_hash_seen:
            self._artifact_hash_seen.add(key)
            self._artifact_hash_queue.append(
                {
                    "run_id": payload.get("run_id"),
                    "artifact_path": abs_path,
                }
            )
        return payload

    def capture_visual_evidence(
        self,
        screenshot_path,
        screenshot_kind: str,
        uitree_kind: str | None = None,
        account=None,
        chat_id=None,
        message_id=None,
        dump_uitree: bool = True,
        **metadata,
    ):
        shot_path = Path(screenshot_path)
        shot_path.parent.mkdir(parents=True, exist_ok=True)
        self.device.screenshot(str(shot_path))
        shot_metadata = dict(metadata or {})
        shot_metadata.setdefault("file_name", shot_path.name)
        shot_meta = self.register_artifact(
            shot_path,
            kind=screenshot_kind,
            account=account,
            chat_id=chat_id,
            message_id=message_id,
            **shot_metadata,
        )

        xml_path = None
        xml_meta = None
        if dump_uitree and uitree_kind:
            tmp_xml = shot_path.with_suffix('.xml')
            try:
                hierarchy = self.device.dump_hierarchy()
                tmp_xml.write_text(hierarchy, encoding='utf-8')
                xml_path = tmp_xml
                xml_metadata = dict(metadata or {})
                xml_metadata["file_name"] = xml_path.name
                xml_meta = self.register_artifact(
                    xml_path,
                    kind=uitree_kind,
                    account=account,
                    chat_id=chat_id,
                    message_id=message_id,
                    **xml_metadata,
                )
            except Exception as e:
                self.log_action('dump_hierarchy', selector=str(tmp_xml), result='fail', error=e)

        return {
            'screenshot_path': str(shot_path),
            'screenshot_artifact': shot_meta,
            'uitree_path': str(xml_path) if xml_path else None,
            'uitree_artifact': xml_meta,
        }

    def flush_artifact_hashes(self):
        if not self._artifact_hash_queue:
            return

        pending = self._artifact_hash_queue
        self._artifact_hash_queue = []

        for item in pending:
            run_id = item.get("run_id")
            artifact_path = item.get("artifact_path")
            if not run_id or not artifact_path:
                continue
            p = Path(artifact_path)
            if not p.exists() or not p.is_file():
                continue
            try:
                digest = sha256_file(p)
                size_bytes = int(p.stat().st_size)
                self.storage.update_file_artifact_hash(run_id, artifact_path, digest, size_bytes=size_bytes)
            except Exception as e:
                self.log_action(
                    "artifact_hash",
                    selector=artifact_path,
                    result="fail",
                    error=e,
                )

    def close(self):
        try:
            self.storage.flush()
        except Exception:
            pass
        try:
            self.storage.close()
        except Exception:
            pass

    @abstractmethod
    def collect(self):
        raise NotImplementedError

    def collect_user_profile(self, *args, **kwargs):
        raise NotImplementedError("collect_user_profile not implemented for this collector")

    def collect_contacts(self, *args, **kwargs):
        raise NotImplementedError("collect_contacts not implemented for this collector")

    def collect_chatrooms(self, *args, **kwargs):
        raise NotImplementedError("collect_chatrooms not implemented for this collector")
