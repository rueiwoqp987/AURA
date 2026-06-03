import json
import logging
import time
from datetime import datetime

from platforms.base import BaseCollector
from collectors.telegram.account import TelegramAccountMixin
from collectors.telegram.contacts import TelegramContactsMixin
from collectors.telegram.messages import TelegramMessagesMixin
from collectors.telegram.attachments import TelegramAttachmentsMixin
from collectors.telegram.chatrooms import TelegramChatroomsMixin
from utils.network_state import evaluate_network_policy, snapshot_network_state
from utils.system_ui_profiles import recent_apps_profile_for
from utils.utils import (
    clear_recent_apps_by_profile,
    ensure_dnd_mode,
    get_device_time,
    set_wifi_enabled,
    toggle_airplane_mode,
)

logger = logging.getLogger(__name__)


class TelegramCollector(
    TelegramAccountMixin,
    TelegramContactsMixin,
    TelegramMessagesMixin,
    TelegramAttachmentsMixin,
    TelegramChatroomsMixin,
    BaseCollector,
):
    PHASE_LABELS = {
        "phase1": "local-first",
        "phase2": "controlled-online",
        "offline": "local-first",
        "online": "controlled-online",
        "local-first": "local-first",
        "controlled-online": "controlled-online",
        "local_first": "local-first",
        "controlled_online": "controlled-online",
    }

    def __init__(self, device=None, artifact_dir=".", audit_log_path="AURA_audit.log", profile=None, serial=None):
        super().__init__(
            device=device,
            package_name="org.telegram.messenger",
            audit_log_path=audit_log_path,
            artifact_dir=artifact_dir,
        )

        self.profile = profile or {}
        self.serial = serial
        self.target_root = self.artifact_dir
        self.run_root = self.target_root.parent
        self.run_id = self.run_root.name
        self.current_phase = None
        self.current_account = None
        self.current_chat_id = None
        self.current_message_id = None
        self._ambiguous_deleted_account_counter = 0

        self.telegram_attachment_snapshot_dirs = {
            "File": [
                "/sdcard/Download",
                "/sdcard/Download/Telegram",
            ],
            "Photo": [
                "/sdcard/Pictures/Telegram",
            ],
            "Video": [
                "/sdcard/Movies/Telegram",
                "/sdcard/Pictures/Telegram",
            ],
        }
        configured_snapshot_dirs = self.profile.get("telegram_attachment_snapshot_dirs")
        if isinstance(configured_snapshot_dirs, dict):
            for key, value in configured_snapshot_dirs.items():
                if isinstance(value, list):
                    self.telegram_attachment_snapshot_dirs[str(key)] = [str(item) for item in value if item]

        self.telegram_download_backup_dirs = self.profile.get("telegram_download_backup_dirs") or []
        self.telegram_download_backup_root = f"/sdcard/AURA_Telegram_backup_{self.run_id}"
        self.telegram_download_backups = []

        self.download_path = "/sdcard/Download"
        self.download_path_candidates = list(self.telegram_attachment_snapshot_dirs.get("File", []))
        self.origin_download_path = None
        self.pictures_path = "/sdcard/Pictures/Telegram"
        self.origin_pictures_path = None

        self._reset_phase_state()
        self.input_bound = None

    def _reset_phase_state(self) -> None:
        self.accounts = []
        self.user_profiles = {}
        self.contacts = {}
        self.targets = {}
        self.completed_targets = {}
        self.chatrooms = {}
        self.chatroom_list_bounds = (None, None)
        self.recent_apps_ui_profile = recent_apps_profile_for(serial=self.serial)

    def _telegram_collection_flags(self, phase_item: dict | None = None) -> dict:
        flags = {
            "collect_accounts": True,
            "switch_accounts": True,
            "collect_user_profile": True,
            "collect_contacts": True,
            "collect_chatrooms": True,
        }
        profile_flags = self.profile.get("telegram_collection_flags") or {}
        phase_flags = (phase_item or {}).get("collection_flags") or {}
        for source in (profile_flags, phase_flags):
            for key in flags:
                if key in source:
                    flags[key] = bool(source.get(key))
        return flags

    def _restart_app(self, reason: str) -> None:
        pkg = self.profile.get("package_name") or self.packageName
        phase_label = self.current_phase or "setup"
        logger.info("%s app_restart: reason=%s", self.aura_prefix(phase_label), reason)
        self.log_action("app_restart", selector=reason)

        try:
            self.device.app_stop(pkg)
            self.log_action("app_stop", selector=pkg)
        except Exception as e:
            self.log_action("app_stop", selector=pkg, result="fail", error=e)

        try:
            self.device.press("home")
            self.log_action("press", selector="home")
        except Exception:
            pass

        self.wait_for_screen_state(
            "telegram_background_after_home",
            lambda pkg=pkg: ((self.device.app_current() or {}).get("package") or "").strip() != pkg,
            timeout=1.2,
            interval=0.2,
            capture_on_timeout=False,
        )

        try:
            self.device.app_start(pkg)
            self.log_action("app_start", selector=pkg)
            self.wait_for_screen_state(
                "telegram_app_foreground",
                lambda pkg=pkg: ((self.device.app_current() or {}).get("package") or "").strip() == pkg,
                timeout=2.5,
                interval=0.2,
                capture_on_timeout=False,
            )
        except Exception as e:
            self.log_action("app_start", selector=pkg, result="fail", error=e)
            return

        try:
            self.input_bound = None
            self.device(scrollable=True).scroll.toBeginning(max_swipes=12, steps=6)
            self.log_action("scroll", selector="toBeginning")
        except Exception as e:
            self.log_action("scroll", selector="toBeginning", result="fail", error=e)

    def _clear_recent_apps_for_phase(self, reason: str) -> None:
        pkg = self.profile.get("package_name") or self.packageName
        phase_label = self.current_phase or "phase"
        logger.info("%s phase_recent_cleanup: reason=%s", self.aura_prefix(phase_label), reason)
        self.log_action("phase_recent_cleanup", selector=reason)

        try:
            self.device.press("recent")
            self.log_action("press", selector="recent", artifacts=[f"reason={reason}"])
        except Exception as e:
            self.log_action("press", selector="recent", result="fail", error=e, artifacts=[f"reason={reason}"])
            return

        recent_profile = dict(self.recent_apps_ui_profile or {})
        recent_profile.setdefault("settle_sec", self.profile.get("phase_recent_cleanup_settle_sec", 0.5))

        def audit(action, selector=None, result="success", error=None, artifacts=None):
            self.log_action(action, selector=selector, result=result, error=error, artifacts=artifacts)

        clear_recent_apps_by_profile(
            self.device,
            recent_profile,
            audit=audit,
            sleep_fn=self._sleep,
            reason=reason,
        )

        self.wait_for_screen_state(
            "telegram_phase_recent_cleanup_done",
            lambda pkg=pkg: ((self.device.app_current() or {}).get("package") or "").strip() != pkg,
            timeout=1.5,
            interval=0.2,
            capture_on_timeout=False,
        )

    def _phase_preflight_path(self, phase_key: str):
        phase_label = self.PHASE_LABELS.get((phase_key or "").lower(), phase_key or "phase")
        return self.run_root / f"preflight_{phase_label.lower()}.json"

    def _phase_label(self, phase_key: str) -> str:
        return self.PHASE_LABELS.get((phase_key or "").lower(), phase_key)

    def _write_preflight(self, phase_key: str, payload: dict) -> None:
        path = self._phase_preflight_path(phase_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _write_collection_timing(self, payload: dict) -> None:
        path = self.run_root / "collection_timing.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_device_info(self) -> dict:
        path = self.run_root / "device_info.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def _switch_phase_artifacts(self, phase_key: str) -> None:
        phase_label = self._phase_label(phase_key)
        phase_dir = self.target_root / phase_label
        phase_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = phase_dir

    def _enforce_network_policy(self, policy: dict) -> None:
        mode = (policy or {}).get("mode", "offline_airplane")
        enforce_dnd = (policy or {}).get("enforce_dnd", True)
        phase_label = self.current_phase or "phase"

        toggle_airplane_mode(True, serial=self.serial)
        self.log_action("phase_enforce", selector="airplane_mode", artifacts=["on"])
        logger.info("%s phase_enforce: airplane_mode=On", self.aura_prefix(phase_label))

        if enforce_dnd:
            ret = ensure_dnd_mode(
                self.device,
                target_mode="1",
                serial=self.serial,
                timeout_sec=10.0,
                audit=lambda action, selector=None, result="success", error=None, artifacts=None: self.log_action(
                    action,
                    selector=selector,
                    result=result,
                    error=error,
                    artifacts=artifacts,
                ),
            )
            self.log_action(
                "phase_enforce",
                selector="dnd_mode",
                result="success" if ret.get("ok") else "fail",
                artifacts=[
                    "on",
                    f"method={ret.get('method')}",
                    f"current={ret.get('current')}",
                ],
            )
            logger.info(
                "%s phase_enforce: dnd_mode=On method=%s ok=%s",
                self.aura_prefix(phase_label),
                ret.get("method"),
                ret.get("ok"),
            )

        if mode == "offline_airplane":
            set_wifi_enabled(False, serial=self.serial)
            self.log_action("phase_enforce", selector="wifi", artifacts=["off"])
            logger.info("%s phase_enforce: wifi=off", self.aura_prefix(phase_label))

        elif mode == "online_wifi":
            if (policy or {}).get("clear_recent_before_wifi", True):
                self._clear_recent_apps_for_phase(reason="before_wifi_on")
            set_wifi_enabled(True, serial=self.serial)
            self.log_action("phase_enforce", selector="wifi", artifacts=["on"])
            logger.info("%s phase_enforce: wifi=on", self.aura_prefix(phase_label))

            wait_sec = int((policy or {}).get("wifi_connect_wait_sec", 20))
            stable_required = int((policy or {}).get("wifi_connected_stable_checks", 3))
            post_connect_wait = float((policy or {}).get("wifi_post_connect_stabilize_sec", 3.0))
            connected, _, stable_count = self.wait_for_consecutive_match(
                action="phase_enforce",
                selector="wifi_connect_check",
                sample_fn=lambda: snapshot_network_state(serial=self.serial),
                match_fn=lambda snap: bool((snap or {}).get("wifi_connected")),
                timeout=max(1, wait_sec),
                stable_polls=max(1, stable_required),
                interval=1.0,
                success_result="success",
                timeout_result="fail",
                timeout_artifacts_fn=lambda _snap, stable: [
                    f"wait_sec={wait_sec}",
                    f"stable_required={stable_required}",
                    f"stable_count={stable}",
                ],
                success_artifacts_fn=lambda _snap, stable: [
                    f"wait_sec={wait_sec}",
                    f"stable_required={stable_required}",
                    f"stable_count={stable}",
                ],
            )

            if connected and post_connect_wait > 0:
                self._sleep(post_connect_wait)
                self.log_action(
                    "phase_enforce",
                    selector="wifi_connect_stabilize",
                    artifacts=[f"sleep={post_connect_wait}s"],
                )

        current = snapshot_network_state(serial=self.serial)
        self.log_action(
            "phase_enforce_state",
            selector="network_state",
            artifacts=[json.dumps(current, ensure_ascii=False)],
        )
        logger.info("%s phase_enforce_state: %s", self.aura_prefix(phase_label), json.dumps(current, ensure_ascii=False))

    def _run_phase_preflight(self, phase_key: str, policy: dict) -> dict:
        phase_label = self._phase_label(phase_key)
        checks = []
        failures = []
        before_state = snapshot_network_state(serial=self.serial)
        try:
            _ = int(get_device_time(serial=self.serial))
            checks.append({"check": "adb_shell_time", "ok": True})
        except Exception as e:
            checks.append({"check": "adb_shell_time", "ok": False, "error": str(e)})
            failures.append("adb_shell_unavailable")

        try:
            probe = self.run_root / ".preflight_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            checks.append({"check": "output_dir_writable", "ok": True})
        except Exception as e:
            checks.append({"check": "output_dir_writable", "ok": False, "error": str(e)})
            failures.append("output_dir_not_writable")

        self._enforce_network_policy(policy)
        current_state = snapshot_network_state(serial=self.serial)
        policy_failures = evaluate_network_policy(current_state, policy)
        failures.extend(policy_failures)
        checks.append({"check": "network_policy", "ok": len(policy_failures) == 0, "failures": policy_failures})

        preflight_ok = all(c.get("ok") for c in checks)
        if policy.get("enforce", True):
            preflight_ok = preflight_ok and len(policy_failures) == 0

        payload = {
            "phase": phase_label,
            "ok": preflight_ok,
            "policy": policy,
            "before_state": before_state,
            "current_state": current_state,
            "checks": checks,
            "failures": failures,
            "ts": time.time(),
        }
        self._write_preflight(phase_key, payload)
        return payload

    def _restore_global_state(self, initial_state: dict) -> None:
        runtime_state = ((self.profile.get("_runtime") or {}).get("initial_state") or {})
        runtime_dnd = runtime_state.get("dnd_mode")
        fallback_dnd = (initial_state or {}).get("dnd_mode")
        target_dnd = runtime_dnd if runtime_dnd not in {None, ""} else fallback_dnd
        if target_dnd in {None, ""}:
            self.log_action("restore_dnd_mode", selector="global", result="skip", artifacts=["target=none"])
            logger.info("%s restore_dnd_mode skipped: no target state", self.aura_prefix("setup"))
            return
        target_dnd_str = str(target_dnd)
        try:

            ret = ensure_dnd_mode(
                self.device,
                target_mode=target_dnd_str,
                serial=self.serial,
                timeout_sec=10.0,
                restore_hide_all=True,
                audit=lambda action, selector=None, result="success", error=None, artifacts=None: self.log_action(
                    action,
                    selector=selector,
                    result=result,
                    error=error,
                    artifacts=artifacts,
                ),
            )

            self.log_action(
                "restore_dnd_mode",
                selector="global",
                result="success" if ret.get("ok") else "fail",
                artifacts=[
                    f"target={ret.get('target')}",
                    f"current={ret.get('current')}",
                    f"method={ret.get('method')}",
                    f"source={'runtime_initial_state' if runtime_dnd not in {None, ''} else 'collector_initial_state'}",
                ],
            )
            logger.info(
                "%s restore_dnd_mode: target=%s current=%s method=%s ok=%s",
                self.aura_prefix("setup"),
                ret.get("target"),
                ret.get("current"),
                ret.get("method"),
                ret.get("ok"),
            )
        except Exception as e:
            self.log_action("restore_dnd_mode", selector="global", result="fail", error=e)
            logger.exception("%s restore_dnd_mode failed: %s", self.aura_prefix("setup"), e)

    def _post_online_app_sync_settle(self, policy: dict) -> None:
        phase_label = self.current_phase or "phase"
        settle_sec = float((policy or {}).get("online_app_sync_settle_sec", 4.0))
        if settle_sec <= 0:
            return

        self.wait_for_screen_state(
            "telegram_chat_list_screen",
            self._is_telegram_chat_list_screen,
            timeout=2.0,
            interval=0.2,
            capture_on_timeout=False,
        )
        self._sleep(settle_sec)
        self.log_action(
            "phase_enforce",
            selector="online_app_sync_settle",
            artifacts=[f"sleep={settle_sec}s"],
        )
        logger.info(
            "%s phase_enforce: online_app_sync_settle=%.2fs",
            self.aura_prefix(phase_label),
            settle_sec,
        )

    def _collect_common(self, phase_key: str, phase_item: dict | None = None):
        phase_label = self._phase_label(phase_key)
        self.current_phase = phase_label
        self._switch_phase_artifacts(phase_key)
        self._reset_phase_state()
        self.storage.begin_batch()

        overall_start = time.time()
        account_timings = []
        flags = self._telegram_collection_flags(phase_item)
        self.init_download_path()
        try:
            self.log_action(
                "telegram_collection_flags",
                selector=f"phase={phase_label}",
                artifacts=[f"{key}={value}" for key, value in flags.items()],
            )

            if flags["collect_accounts"]:
                self.collect_user_account()
                self.wait_for_screen_state(
                    "telegram_chat_list_screen",
                    self._is_telegram_chat_list_screen,
                    timeout=1.5,
                    interval=0.2,
                    capture_on_timeout=False,
                )
            else:
                fallback_account = self.profile.get("current_account_name") or "current"
                self.accounts = [{"user_name": fallback_account, "bounds": None}]
                logger.info("%s Account collection skipped: using current account label=%s", self.aura_prefix(phase_label), fallback_account)
                self.log_action(
                    "collect_user_account",
                    selector="current_account",
                    result="skip",
                    artifacts=[f"user_name={fallback_account}"],
                )

            accounts_to_process = list(self.accounts)
            if not flags["switch_accounts"] and len(accounts_to_process) > 1:
                skipped_accounts = [
                    (item.get("user_name") if isinstance(item, dict) else item)
                    for item in accounts_to_process[1:]
                ]
                accounts_to_process = accounts_to_process[:1]
                self.log_action(
                    "select_user_account",
                    selector="batch",
                    result="skip_disabled",
                    artifacts=[f"skipped={json.dumps(skipped_accounts, ensure_ascii=False)}"],
                )
                logger.info(
                    "%s Account switching disabled: only current account will be processed (skipped=%s)",
                    self.aura_prefix(phase_label),
                    skipped_accounts,
                )

            for account in accounts_to_process:
                account_name = account.get("user_name") if isinstance(account, dict) else account
                if account_name is None:
                    account_name = "current"
                elif not isinstance(account_name, str):
                    account_name = str(account_name)
                self.current_account = account_name
                account_started_at = datetime.now().astimezone().isoformat()
                account_start = time.time()

                if flags["switch_accounts"]:
                    self.select_user_account(account)
                else:
                    self.log_action("select_user_account", selector=f"account={account_name}", result="skip_disabled")

                if flags["collect_user_profile"]:
                    self.collect_user_profile(account_name)
                else:
                    self.log_action("collect_user_profile", selector=f"account={account_name}", result="skip_disabled")

                if flags["collect_contacts"]:
                    self.collect_contacts(account_name)
                else:
                    self.log_action("collect_contacts", selector=f"account={account_name}", result="skip_disabled")

                if flags["collect_chatrooms"]:
                    self.collect_chatrooms(account_name)
                else:
                    self.log_action("collect_chatrooms", selector=f"account={account_name}", result="skip_disabled")

                account_duration = time.time() - account_start
                account_ended_at = datetime.now().astimezone().isoformat()
                account_timings.append(
                    {
                        "account": account_name,
                        "started_at": account_started_at,
                        "ended_at": account_ended_at,
                        "duration_sec": round(account_duration, 3),
                    }
                )
                logger.info("%s Account chat history collected: account=%s finished_at=%s duration=%.2fs", self.aura_prefix(phase_label),
                    account_name,
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                    account_duration,
                )
                self.log_action(
                    "collect_account_chat_history",
                    selector=f"phase={phase_label};account={account_name}",
                    result="done",
                    artifacts=[f"chatroom_count={len(self.chatrooms.get(account_name, {}))}"],
                )
                self.flush_artifact_hashes()
                self.current_account = None

            duration = time.time() - overall_start
            self.flush_artifact_hashes()
            return {
                "status": "done",
                "phase": phase_label,
                "duration_sec": duration,
                "accounts": self.accounts,
                "telegram_collection_flags": flags,
                "user_profiles": self.user_profiles,
                "contacts": self.contacts,
                "chatrooms": self.chatrooms,
                "account_timings": account_timings,
                "artifact_dir": str(self.artifact_dir),
                "db_path": str(self.storage.db_path.resolve()),
            }
        finally:
            self.flush_artifact_hashes()
            self.storage.end_batch()
            self.current_account = None
            self.current_chat_id = None
            self.current_message_id = None
            self.restore_download_path()

    def _default_phase_plan(self) -> list[dict]:
        return [
            {
                "name": "local-first",
                "enabled": True,
                "policy": {
                    "mode": "offline_airplane",
                    "enforce": True,
                    "enforce_dnd": True,
                    "collect": True,
                },
                "collection_flags": {
                    "collect_accounts": True,
                    "switch_accounts": True,
                    "collect_user_profile": True,
                    "collect_contacts": True,
                    "collect_chatrooms": True,
                },
            },
            {
                "name": "controlled-online",
                "enabled": True,
                "policy": {
                    "mode": "online_wifi",
                    "enforce": True,
                    "enforce_dnd": True,
                    "collect": True,
                    "disable_wifi_after": True,
                    "wifi_connect_wait_sec": 20,
                    "wifi_connected_stable_checks": 3,
                    "wifi_post_connect_stabilize_sec": 3,
                    "online_app_sync_settle_sec": 4.0,
                },
                "collection_flags": {
                    "collect_accounts": True,
                    "switch_accounts": True,
                    "collect_user_profile": True,
                    "collect_contacts": True,
                    "collect_chatrooms": True,
                },
            },
        ]

    def collect(self):
        run_started_at = datetime.now().astimezone().isoformat()
        run_start_ts = time.time()
        initial_state = snapshot_network_state(serial=self.serial)
        phase_plan = self.profile.get("phase_plan") or self._default_phase_plan()
        phase_results = []

        try:
            for phase_index, item in enumerate(phase_plan):
                phase_key = item.get("name", "phase")
                policy = item.get("policy", {})
                phase_label = self._phase_label(phase_key)
                phase_enabled = item.get("enabled", True)
                self.current_phase = phase_label
                phase_started_at = datetime.now().astimezone().isoformat()
                phase_start_ts = time.time()

                if not phase_enabled:
                    phase_duration = round(time.time() - phase_start_ts, 3)
                    self.log_action("phase_disabled", selector=phase_label, artifacts=["enabled=false"])
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "disabled",
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": phase_duration,
                            },
                        }
                    )
                    logger.info("%s SKIP (disabled=false, duration=%.2fs)", self.aura_prefix(phase_label), phase_duration)
                    continue

                logger.info("%s START", self.aura_prefix(phase_label))
                self.log_action("phase_start", selector=f"{phase_label}_preflight")
                preflight = self._run_phase_preflight(phase_key, policy)

                if not preflight["ok"]:
                    phase_duration = round(time.time() - phase_start_ts, 3)
                    self.log_action(
                        "phase_fail",
                        selector=f"{phase_label}_preflight",
                        result="fail",
                        artifacts=preflight.get("failures", []),
                    )
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "preflight_failed",
                            "preflight": preflight,
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": phase_duration,
                            },
                        }
                    )
                    if policy.get("enforce", True):
                        logger.info("%s END (preflight_failed, duration=%.2fs)", self.aura_prefix(phase_label), phase_duration)
                        return {"status": "preflight_failed", "phases": phase_results}
                    continue


                if policy.get("mode") == "online_wifi":
                    self._restart_app(reason=f"after_wifi_connected phase={phase_label}")
                    self._post_online_app_sync_settle(policy)
                else:
                    self._restart_app(reason=f"after_preflight phase={phase_label}")

                self.log_action("phase_end", selector=f"{phase_label}_preflight")

                if item.get("skip_collect", False) or not policy.get("collect", True):
                    phase_duration = round(time.time() - phase_start_ts, 3)
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "skipped",
                            "preflight": preflight,
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": phase_duration,
                            },
                        }
                    )
                    logger.info("%s END (skipped, duration=%.2fs)", self.aura_prefix(phase_label), phase_duration)
                    continue

                self.log_action("phase_start", selector=f"{phase_label}_acquire")
                try:
                    ret = self._collect_common(phase_key, phase_item=item)
                    phase_duration = round(time.time() - phase_start_ts, 3)
                    self.log_action(
                        "phase_end",
                        selector=f"{phase_label}_acquire",
                        artifacts=[f"duration={ret.get('duration_sec', 0):.2f}s"],
                    )
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "done",
                            "preflight": preflight,
                            "result": ret,
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": phase_duration,
                            },
                        }
                    )
                    logger.info("%s END (done, duration=%.2fs)", self.aura_prefix(phase_label), phase_duration)
                except Exception as e:
                    self.log_action("phase_fail", selector=f"{phase_label}_acquire", result="fail", error=e)
                    logger.info(
                        "%s END (failed, duration=%.2fs)",
                        self.aura_prefix(phase_label),
                        round(time.time() - phase_start_ts, 3),
                    )
                    raise
                finally:
                    if policy.get("disable_wifi_after", False):
                        try:
                            set_wifi_enabled(False, serial=self.serial)
                            self.log_action("phase_finalize", selector=f"{phase_label}_wifi", artifacts=["off"])
                        except Exception as e:
                            self.log_action("phase_finalize", selector=f"{phase_label}_wifi", result="fail", error=e)

            return {"status": "done", "phases": phase_results}
        finally:
            app_info = {
                "app_name": self.profile.get("app_name", "Telegram"),
                "package_name": self.profile.get("package_name", self.packageName),
                "app_version": self.profile.get("app_version", ""),
                "collection_methods": self.profile.get("collection_methods", ["S1"]),
                "phase_plan": self.profile.get("phase_plan", []),
            }
            timing_payload = {
                "run_id": self.run_id,
                "target": app_info.get("app_name", "Telegram"),
                "method": "S1",
                "started_at": run_started_at,
                "ended_at": datetime.now().astimezone().isoformat(),
                "duration_sec": round(time.time() - run_start_ts, 3),
                "device_info": self._load_device_info(),
                "app_info": app_info,
                "phases": [
                    {
                        "phase": p.get("phase"),
                        "status": p.get("status"),
                        "timing": p.get("timing"),
                        "account_timings": (p.get("result") or {}).get("account_timings", []),
                    }
                    for p in phase_results
                ],
            }
            self._write_collection_timing(timing_payload)
            self.current_phase = None
            self.current_account = None
            self.current_chat_id = None
            self.current_message_id = None
            self._restore_global_state(initial_state)


Telegram = TelegramCollector
