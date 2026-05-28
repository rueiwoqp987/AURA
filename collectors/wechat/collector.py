import json
import logging
import time
from datetime import datetime
from pathlib import Path

from platforms.base import BaseCollector
from collectors.wechat.chat_list import ChatCandidate, extract_chat_candidates
from collectors.wechat.chatrooms import WeChatChatroomsMixin
from collectors.wechat.mixin_ocr import WeChatOcrMixin
from utils.network_state import evaluate_network_policy, snapshot_network_state
from utils.utils import ensure_dnd_mode, get_device_time, set_wifi_enabled, toggle_airplane_mode

logger = logging.getLogger(__name__)


class WeChatCollector(WeChatOcrMixin, WeChatChatroomsMixin, BaseCollector):
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

    NAV_BLACKLIST = {
        "WeChat",
        "Chats",
        "Contacts",
        "Discover",
        "Me",
        "Search",
        "Settings",
        "Moments",
        "Channels",
        "Official Accounts",
    }

    def __init__(self, device=None, artifact_dir=".", audit_log_path="AURA_audit.log", profile=None, serial=None):
        super().__init__(
            device=device,
            package_name="com.tencent.mm",
            audit_log_path=audit_log_path,
            artifact_dir=artifact_dir,
        )
        self.profile = profile or {}
        self.serial = serial
        self.target_root = self.artifact_dir
        self.run_root = self.target_root.parent
        self.run_id = self.run_root.name
        self.current_phase = None
        self.current_account = "default"
        self.current_chat_id = None
        self.current_message_id = None

        self._ocr_engine = None
        # App launch is phase-scoped in _collect_common() to preserve preflight/enforcement order.

    def _restart_app(self, reason: str) -> None:
        """Stop and relaunch the target app to reduce cross-phase UI state leakage."""
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
            "wechat_background_after_home",
            lambda pkg=pkg: ((self.device.app_current() or {}).get("package") or "").strip() != pkg,
            timeout=1.2,
            interval=0.2,
            capture_on_timeout=False,
        )

        try:
            self.device.app_start(pkg)
            self.log_action("app_start", selector=pkg)
        except Exception as e:
            self.log_action("app_start", selector=pkg, result="fail", error=e)
            raise

        self.wait_for_screen_state(
            "wechat_app_foreground",
            lambda pkg=pkg: ((self.device.app_current() or {}).get("package") or "").strip() == pkg,
            timeout=2.5,
            interval=0.2,
            capture_on_timeout=False,
        )

    def _phase_label(self, phase_key: str) -> str:
        return self.PHASE_LABELS.get((phase_key or "").lower(), phase_key)

    def _foreground_package(self) -> str:
        try:
            cur = self.device.app_current()
            if isinstance(cur, dict):
                return (cur.get("package") or "").strip()
            return ""
        except Exception:
            return ""

    def _is_app_foreground(self) -> bool:
        pkg = (self.profile.get("package_name") or self.packageName or "").strip()
        if not pkg:
            return True
        return self._foreground_package() == pkg

    def _phase_preflight_path(self, phase_key: str):
        return self.run_root / f"preflight_{self._phase_label(phase_key).lower()}.json"

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
        phase_dir = self.target_root / self._phase_label(phase_key)
        phase_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = phase_dir

    def _enforce_network_policy(self, policy: dict) -> None:
        mode = (policy or {}).get("mode", "offline_airplane")
        enforce_dnd = (policy or {}).get("enforce_dnd", True)

        toggle_airplane_mode(True, serial=self.serial)
        self.log_action("phase_enforce", selector="airplane_mode", artifacts=["on"])
        logger.info("%s phase_enforce: airplane_mode=On", self.aura_prefix(self.current_phase or "phase"))

        if enforce_dnd:
            ret = ensure_dnd_mode(self.device, target_mode="1", serial=self.serial, timeout_sec=10.0)
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
                self.aura_prefix(self.current_phase or "phase"),
                ret.get("method"),
                ret.get("ok"),
            )

        if mode == "offline_airplane":
            set_wifi_enabled(False, serial=self.serial)
            self.log_action("phase_enforce", selector="wifi", artifacts=["off"])
            logger.info("%s phase_enforce: wifi=off", self.aura_prefix(self.current_phase or "phase"))
        elif mode == "online_wifi":
            set_wifi_enabled(True, serial=self.serial)
            self.log_action("phase_enforce", selector="wifi", artifacts=["on"])
            logger.info("%s phase_enforce: wifi=on", self.aura_prefix(self.current_phase or "phase"))
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
        self.log_action("phase_enforce_state", selector="network_state", artifacts=[json.dumps(current, ensure_ascii=False)])
        logger.info("%s phase_enforce_state: %s", self.aura_prefix(self.current_phase or "phase"), json.dumps(current, ensure_ascii=False))

    def _run_phase_preflight(self, phase_key: str, policy: dict) -> dict:
        phase_label = self._phase_label(phase_key)
        checks = []
        failures = []
        before_state = snapshot_network_state(serial=self.serial)

        try:
            self._init_ocr()
            checks.append({"check": "ocr_engine", "ok": True, "engine": getattr(self._ocr_engine, "name", "")})
        except Exception as e:
            checks.append({"check": "ocr_engine", "ok": False, "error": str(e)})
            failures.append("ocr_unavailable")

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
            return
        try:
            ret = ensure_dnd_mode(
                self.device,
                target_mode=str(target_dnd),
                serial=self.serial,
                timeout_sec=10.0,
                restore_hide_all=True,
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
        except Exception as e:
            self.log_action("restore_dnd_mode", selector="global", result="fail", error=e)

    def _capture_chat_list(self, phase_label: str) -> dict:
        capture_dir = Path(self.artifact_dir) / "Chat List"
        capture_dir.mkdir(parents=True, exist_ok=True)

        max_swipes = int(self.profile.get("chat_list_max_swipes", 20))

        logger.info("%s Chat list capture start", self.aura_prefix(phase_label))
        self.log_action("chat_list_capture_start", selector=phase_label)

        known: set[str] = set()
        candidates: list[ChatCandidate] = []
        screenshots: list[str] = []
        stagnant = 0

        for swipe_idx in range(max_swipes):
            shot = capture_dir / f"chat_list_{swipe_idx:03d}.jpg"
            ev = self.capture_visual_evidence(
                shot,
                screenshot_kind="screenshot_chat_list",
                uitree_kind=None,
                account=self.current_account,
                dump_uitree=False,
            )
            screenshot_path = ev.get("screenshot_path")
            screenshots.append(screenshot_path)

            items = self.ocr_image(screenshot_path)
            ocr_json_path = self.write_ocr_artifact(
                screenshot_path,
                items,
                kind="ocr_chat_list",
                meta={"phase": phase_label, "swipe_index": swipe_idx},
            )

            safe_area = self._compute_wechat_safe_area(items)
            found = extract_chat_candidates(
                items,
                set(self.NAV_BLACKLIST),
                swipe_index=swipe_idx,
                safe_top=safe_area["top"],
                safe_bottom=safe_area["bottom"],
            )
            new_count = 0
            for c in found:
                if c.chat_id in known:
                    continue
                known.add(c.chat_id)
                new_count += 1
                candidates.append(c)

            self.log_action(
                "chat_list_snapshot",
                selector=phase_label,
                artifacts=[
                    f"swipe={swipe_idx}",
                    f"new={new_count}",
                    f"total={len(candidates)}",
                    f"safe_top={safe_area['top']}",
                    f"safe_bottom={safe_area['bottom']}",
                    screenshot_path,
                    ocr_json_path,
                ],
            )

            if new_count == 0:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 2:
                break

            if not self._scroll_down_safe():
                logger.warning("%s Chat list scroll interrupted (recent/app switched)", self.aura_prefix(phase_label))
                break
        if candidates:
            min_swipe = min(c.swipe_index for c in candidates)
            max_swipe = max(c.swipe_index for c in candidates)
            if min_swipe > 0:
                candidates = [
                    ChatCandidate(
                        chat_id=c.chat_id,
                        chat_name=c.chat_name,
                        cx=c.cx,
                        cy=c.cy,
                        name_bbox=c.name_bbox,
                        time_text=c.time_text,
                        row_signature=c.row_signature,
                        swipe_index=max(0, c.swipe_index - min_swipe),
                    )
                    for c in candidates
                ]
                self.log_action(
                    "chat_list_swipe_index_normalized",
                    selector=phase_label,
                    artifacts=[f"min={min_swipe}", f"max={max_swipe}"],
                )

        rooms = []
        for c in candidates:
            artifacts = [f"tap={c.cx},{c.cy}", f"swipe_index={c.swipe_index}"]
            if c.time_text:
                artifacts.append(f"time={c.time_text}")
            rooms.append({"chat_id": c.chat_id, "name": c.chat_name, "type": "Unknown", "artifacts": artifacts})

        self.storage.upsert_chatrooms(self.app_id, self.current_account or "default", rooms, phase=self.current_phase or "")

        self.log_action(
            "chat_list_capture_done",
            selector=phase_label,
            artifacts=[f"count={len(rooms)}", f"screenshots={len(screenshots)}"],
        )
        logger.info(
            "%s Chat list capture done (count=%d, screenshots=%d)",
            self.aura_prefix(phase_label),
            len(rooms),
            len(screenshots),
        )
        return {"candidates": candidates, "screenshots": screenshots}

    def _collect_common(self, phase_key: str) -> dict:
        phase_label = self._phase_label(phase_key)
        self.current_phase = phase_label
        self._switch_phase_artifacts(phase_key)
        self.storage.begin_batch()
        started_at = datetime.now().astimezone().isoformat()
        t0 = time.time()
        try:
            logger.info("%s App launch", self.aura_prefix(phase_label))
            self.log_action("app_launch", selector=self.packageName)
            self.launch_app(reason=f"phase={phase_label}")
            self.wait_for_screen_state(
                "wechat_app_ready",
                lambda: self._is_app_foreground() and (not self._is_wechat_recent_overlay()),
                timeout=1.5,
                interval=0.2,
                capture_on_timeout=False,
            )
            self.log_action("app_ready", selector=self.packageName)

            chat_list = self._capture_chat_list(phase_label)
            candidates = chat_list.get("candidates") or []

            history = self.collect_chatrooms_and_messages(candidates, phase_label)
            history_status = history.get("status", "done")
            if history_status != "done":
                return {
                    "status": "failed",
                    "reason": history.get("reason", "chat_history_aborted"),
                    "phase": phase_label,
                    "duration_sec": round(time.time() - t0, 3),
                    "started_at": started_at,
                    "ended_at": datetime.now().astimezone().isoformat(),
                    "chatroom_count": len(candidates),
                    "artifact_dir": str(self.artifact_dir),
                    "db_path": str(self.storage.db_path.resolve()),
                    "chat_history": history,
                }

            self.flush_artifact_hashes()
            return {
                "status": "done",
                "phase": phase_label,
                "duration_sec": round(time.time() - t0, 3),
                "started_at": started_at,
                "ended_at": datetime.now().astimezone().isoformat(),
                "chatroom_count": len(candidates),
                "artifact_dir": str(self.artifact_dir),
                "db_path": str(self.storage.db_path.resolve()),
                "chat_history": {
                    "chat_count": len(history.get("chats") or []),
                },
            }
        finally:
            self.flush_artifact_hashes()
            self.storage.end_batch()

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
                phase_started_at = datetime.now().astimezone().isoformat()
                phase_start_ts = time.time()
                self.current_phase = phase_label

                if not phase_enabled:
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "disabled",
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": round(time.time() - phase_start_ts, 3),
                            },
                        }
                    )
                    self.log_action("phase_disabled", selector=phase_label, artifacts=["enabled=false"])
                    continue

                logger.info("%s START", self.aura_prefix(phase_label))
                self.log_action("phase_start", selector=f"{phase_label}_preflight")
                preflight = self._run_phase_preflight(phase_key, policy)

                if not preflight["ok"]:
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "preflight_failed",
                            "preflight": preflight,
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": round(time.time() - phase_start_ts, 3),
                            },
                        }
                    )
                    self.log_action(
                        "phase_fail",
                        selector=f"{phase_label}_preflight",
                        result="fail",
                        artifacts=preflight.get("failures", []),
                    )
                    if policy.get("enforce", True):
                        return {"status": "preflight_failed", "phases": phase_results}
                    continue

                # Restart app only after environment/preflight is successfully enforced.
                if policy.get("mode") == "online_wifi":
                    self._restart_app(reason=f"after_wifi_connected phase={phase_label}")
                else:
                    self._restart_app(reason=f"after_preflight phase={phase_label}")

                self.log_action("phase_end", selector=f"{phase_label}_preflight")

                if item.get("skip_collect", False) or not policy.get("collect", True):
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "skipped",
                            "preflight": preflight,
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": round(time.time() - phase_start_ts, 3),
                            },
                        }
                    )
                    continue

                self.log_action("phase_start", selector=f"{phase_label}_acquire")
                ret = self._collect_common(phase_key)
                self.log_action("phase_end", selector=f"{phase_label}_acquire", artifacts=[f"duration={ret.get('duration_sec', 0):.2f}s"])

                if ret.get("status") != "done":
                    phase_results.append(
                        {
                            "phase": phase_label,
                            "status": "failed",
                            "preflight": preflight,
                            "result": ret,
                            "timing": {
                                "started_at": phase_started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": round(time.time() - phase_start_ts, 3),
                            },
                        }
                    )
                    logger.error(
                        "%s END (failed, reason=%s, duration=%.2fs)",
                        self.aura_prefix(phase_label),
                        ret.get("reason", "collect_failed"),
                        round(time.time() - phase_start_ts, 3),
                    )
                    return {"status": "failed", "phases": phase_results}

                phase_results.append(
                    {
                        "phase": phase_label,
                        "status": "done",
                        "preflight": preflight,
                        "result": ret,
                        "timing": {
                            "started_at": phase_started_at,
                            "ended_at": datetime.now().astimezone().isoformat(),
                            "duration_sec": round(time.time() - phase_start_ts, 3),
                        },
                    }
                )
                logger.info("%s END (done, duration=%.2fs)", self.aura_prefix(phase_label), round(time.time() - phase_start_ts, 3))

                if policy.get("disable_wifi_after", False):
                    try:
                        set_wifi_enabled(False, serial=self.serial)
                        self.log_action("phase_finalize", selector=f"{phase_label}_wifi", artifacts=["off"])
                    except Exception as e:
                        self.log_action("phase_finalize", selector=f"{phase_label}_wifi", result="fail", error=e)

            return {"status": "done", "phases": phase_results}
        finally:
            self._write_collection_timing(
                {
                    "run_id": self.run_id,
                    "target": self.profile.get("app_name", "WeChat"),
                    "method": "S3",
                    "started_at": run_started_at,
                    "ended_at": datetime.now().astimezone().isoformat(),
                    "duration_sec": round(time.time() - run_start_ts, 3),
                    "device_info": self._load_device_info(),
                    "app_info": {
                        "app_name": self.profile.get("app_name", "WeChat"),
                        "package_name": self.profile.get("package_name", self.packageName),
                        "app_version": self.profile.get("app_version", ""),
                        "collection_methods": self.profile.get("collection_methods", ["S3"]),
                        "phase_plan": self.profile.get("phase_plan", []),
                    },
                    "phases": [
                        {
                            "phase": p.get("phase"),
                            "status": p.get("status"),
                            "timing": p.get("timing"),
                        }
                        for p in phase_results
                    ],
                }
            )
            self._restore_global_state(initial_state)
            self.current_phase = None
            self.current_account = "default"
            self.current_chat_id = None
            self.current_message_id = None










