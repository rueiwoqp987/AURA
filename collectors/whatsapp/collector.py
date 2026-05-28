import json
import logging
import re
import time
from datetime import datetime

from collectors.whatsapp.chatrooms import WhatsAppChatroomsMixin
from collectors.whatsapp.messages import WhatsAppMessagesMixin
from platforms.base import BaseCollector
from utils.network_state import evaluate_network_policy, snapshot_network_state
from utils.utils import (
    bluetooth_any_device_connected,
    bluetooth_any_device_paired,
    bluetooth_target_connected,
    bluetooth_target_paired,
    detect_host_device_name,
    ensure_dnd_mode,
    get_adb_device,
    get_device_time,
    set_wifi_enabled,
    toggle_airplane_mode,
    wait_for_bluetooth_target_connected,
)

logger = logging.getLogger(__name__)


class WhatsAppCollector(
    WhatsAppMessagesMixin,
    WhatsAppChatroomsMixin,
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
            package_name="com.whatsapp",
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
        self.bluetooth_target_name = detect_host_device_name()
        self.bluetooth_initial_state = None
        self.log_action(
            "bluetooth_target_resolved",
            selector="host_device_name",
            artifacts=[self.bluetooth_target_name or "unknown"],
        )

    def _is_android_settings_screen(self) -> bool:
        try:
            current = self.device.app_current() or {}
            pkg = (current.get("package") or "").lower()
            if "settings" in pkg:
                return True
        except Exception:
            pass

        try:
            return any(
                self.device(text=label).exists
                for label in ("Settings", "Connections", "Connection", "Connected devices", "Bluetooth")
            )
        except Exception:
            return False

    def _is_android_bluetooth_settings_screen(self) -> bool:
        if not self._is_android_settings_screen():
            return False

        try:
            if self.device.xpath('//android.widget.TextView[@resource-id="com.android.settings:id/switch_text"]').exists:
                return True
            if self.device(text="Bluetooth").exists:
                return True
            if self.device(textContains="Bluetooth").exists:
                return True
        except Exception:
            pass
        return False

    def _is_android_connections_screen(self) -> bool:
        if not self._is_android_settings_screen():
            return False

        try:
            if self.device.xpath('//android.widget.TextView[@resource-id="com.android.settings:id/switch_text"]').exists:
                return False
        except Exception:
            pass

        try:
            has_section_title = any(
                self.device(text=label).exists
                for label in ("Connections", "Connection", "Connected devices")
            )
            has_connectivity_items = any(
                self.device(textContains=label).exists
                for label in ("Bluetooth", "Wi", "Airplane", "Data usage", "NFC")
            )
            return bool(has_section_title and has_connectivity_items)
        except Exception:
            return False

    def _is_android_bluetooth_device_details_screen(self, target_name: str | None = None) -> bool:
        if not self._is_android_settings_screen():
            return False

        try:
            has_unpair = self.device(text="Unpair").exists or self.device(textContains="Unpair").exists
        except Exception:
            has_unpair = False

        if not has_unpair:
            return False

        target = (target_name or "").strip()
        if not target:
            return True

        try:
            return self.device(textContains=target).exists or self.device(descriptionContains=target).exists
        except Exception:
            return True

    def _return_home(self, reason: str) -> None:
        try:
            self.device.press("home")
            self.log_action("press", selector="home", artifacts=[f"reason={reason}"])
            self._sleep(0.5)
        except Exception as e:
            self.log_action("press", selector="home", result="fail", error=e, artifacts=[f"reason={reason}"])

    def _click_bluetooth_target_row(self, target_name: str, timeout_sec: float = 4.0) -> bool:
        target = (target_name or "").strip()
        if not target:
            return False

        escaped_target = target.replace('"', '\\"')
        xpaths = (
            f'//android.widget.FrameLayout[@clickable="true"][.//android.widget.TextView[@resource-id="android:id/title" and contains(@text, "{escaped_target}")]]',
            f'//android.widget.LinearLayout[@clickable="true"][.//android.widget.TextView[@resource-id="android:id/title" and contains(@text, "{escaped_target}")]]',
            f'//android.widget.TextView[@resource-id="android:id/title" and contains(@text, "{escaped_target}")]',
        )

        for xp in xpaths:
            try:
                node = self.device.xpath(xp)
                if node.wait(timeout=0.6):
                    if self.safe_click(
                        f"bluetooth_target_row={target}",
                        lambda node=node: node.click(),
                        expected_state_name=f"bluetooth_target_connected={target}",
                        expected_predicate=lambda: bluetooth_target_connected(target, serial=self.serial),
                        timeout=timeout_sec,
                        recovery_fn=self._accept_android_bluetooth_dialog,
                        settle_sec=0.4,
                    ):
                        return True
            except Exception as e:
                self.log_action("click", selector=f"bluetooth_target_row_xpath={xp}", result="fail", error=e)

        try:
            if self.device(textContains=target).exists:
                return self.safe_click(
                    f"bluetooth_target_text={target}",
                    lambda: self.device(textContains=target).click(),
                    expected_state_name=f"bluetooth_target_connected={target}",
                    expected_predicate=lambda: bluetooth_target_connected(target, serial=self.serial),
                    timeout=timeout_sec,
                    recovery_fn=self._accept_android_bluetooth_dialog,
                    settle_sec=0.4,
                )
        except Exception as e:
            self.log_action("click", selector=f"bluetooth_target_text={target}", result="fail", error=e)
        return False

    def _collect_visible_bluetooth_device_labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        xpaths = (
            '//android.widget.FrameLayout[@clickable="true"]//android.widget.TextView[@resource-id="android:id/title"]',
            '//android.widget.LinearLayout[@clickable="true"]//android.widget.TextView[@resource-id="android:id/title"]',
        )
        seen = set()

        for xp in xpaths:
            try:
                for node in self.device.xpath(xp).all():
                    label = (getattr(node, "text", "") or "").strip().replace("\u200e", "")
                    if not label:
                        continue
                    lower = label.lower()
                    if lower == "available devices":
                        continue
                    if "make sure your bluetooth device is in pairing mode" in lower:
                        continue
                    if lower in seen:
                        continue
                    seen.add(lower)
                    labels.append(label)
            except Exception:
                continue
        return tuple(labels)

    def _post_bluetooth_connect_settle(self, target_name: str | None = None) -> bool:
        target = (target_name or self.bluetooth_target_name or "").strip()
        timeout_sec = float(self.profile.get("bluetooth_connect_settle_sec", 1.2))
        stable_needed = max(1, int(self.profile.get("bluetooth_connect_settle_polls", 2)))
        interval_sec = float(self.profile.get("bluetooth_connect_settle_interval_sec", 0.25))
        settled, _, _ = self.wait_for_consecutive_match(
            action="bluetooth_connect_settle",
            selector=target or "post_connect",
            sample_fn=lambda: (
                True if not target else bluetooth_target_connected(target, serial=self.serial),
                self._is_android_bluetooth_pairing_dialog(target or None),
            ),
            match_fn=lambda sample: bool(sample[0]) and (not bool(sample[1])),
            timeout=max(0.4, timeout_sec),
            stable_polls=stable_needed,
            interval=interval_sec,
            success_artifacts_fn=lambda _sample, _stable: [f"stable_polls={stable_needed}", f"interval={interval_sec:.2f}s"],
            timeout_artifacts_fn=lambda _sample, stable: [f"stable_count={stable}", f"target={target or 'unknown'}"],
            on_poll=lambda: self._wait_and_accept_android_bluetooth_dialog(target or None, timeout_sec=0.2),
        )
        return settled

    def _wait_for_bluetooth_device_list_stable(
        self,
        timeout_sec: float = 8.0,
        stable_polls: int = 2,
        interval_sec: float = 0.6,
        target_name: str | None = None,
        min_devices: int = 1,
    ) -> bool:
        stable_needed = max(1, int(stable_polls))
        target = (target_name or "").strip().lower()
        settled, labels, _ = self.wait_for_consecutive_same_sample(
            action="bluetooth_device_list_stable",
            selector=target_name or "any",
            sample_fn=self._collect_visible_bluetooth_device_labels,
            timeout=max(0.5, float(timeout_sec)),
            stable_polls=stable_needed,
            interval=interval_sec,
            valid_fn=lambda sample: len(sample) >= max(0, int(min_devices)) and ((not target) or any(target in label.lower() for label in sample)),
            success_result="success",
            timeout_result="fail",
            success_artifacts_fn=lambda sample, _stable: [f"stable_polls={stable_needed}", f"devices={len(sample)}"],
            timeout_artifacts_fn=lambda sample, _stable: [f"devices={len(sample or ())}", f"labels={list(sample or ())[:6]}"],
        )
        return settled

    def _wait_for_bluetooth_device_details_stable(
        self,
        target_name: str,
        timeout_sec: float = 5.0,
        stable_polls: int = 2,
        interval_sec: float = 0.4,
    ) -> bool:
        target = (target_name or "").strip()
        stable_needed = max(1, int(stable_polls))
        settled, _, _ = self.wait_for_consecutive_match(
            action="bluetooth_device_details_stable",
            selector=target,
            sample_fn=lambda: self._is_android_bluetooth_device_details_screen(target),
            match_fn=bool,
            timeout=max(0.5, float(timeout_sec)),
            stable_polls=stable_needed,
            interval=interval_sec,
            success_result="success",
            timeout_result="fail",
            success_artifacts_fn=lambda _sample, _stable: [f"stable_polls={stable_needed}", f"interval={interval_sec}s"],
            timeout_artifacts_fn=lambda _sample, stable: [f"stable_count={stable}", f"required={stable_needed}"],
        )
        return settled

    def _wait_for_bluetooth_unpair_stable(
        self,
        target_name: str,
        timeout_sec: float = 8.0,
        stable_polls: int = 2,
        interval_sec: float = 0.5,
    ) -> bool:
        target = (target_name or "").strip()
        stable_needed = max(1, int(stable_polls))
        settled, sample, _ = self.wait_for_consecutive_match(
            action="bluetooth_unpair_stable",
            selector=target,
            sample_fn=lambda: (
                self._is_android_bluetooth_device_details_screen(target),
                bluetooth_target_paired(target, serial=self.serial),
            ),
            match_fn=lambda current: (not bool(current[0])) and (not bool(current[1])),
            timeout=max(0.5, float(timeout_sec)),
            stable_polls=stable_needed,
            interval=interval_sec,
            success_result="success",
            timeout_result="fail",
            success_artifacts_fn=lambda _sample, _stable: [f"stable_polls={stable_needed}", f"interval={interval_sec}s"],
            timeout_artifacts_fn=lambda current, _stable: [
                f"details_open={bool(current[0]) if current else False}",
                f"still_paired={bool(current[1]) if current else False}",
            ],
        )
        return settled

    def _click_bluetooth_on_selector(self) -> bool:
        switch_bg_rid = "com.android.settings:id/switch_background"
        switch_widget_rid = "com.android.settings:id/switch_widget"
        switch_text_rid = "com.android.settings:id/switch_text"

        try:
            bg = self.device(resourceId=switch_bg_rid, descriptionContains="On")
            if bg.exists:
                bg.click()
                self.log_action("click", selector=f"resourceId={switch_bg_rid}[On]")
                self._sleep(0.6)
                return True
        except Exception as e:
            self.log_action("click", selector=f"resourceId={switch_bg_rid}[On]", result="fail", error=e)

        try:
            widget = self.device(resourceId=switch_widget_rid, checked=True)
            if widget.exists:
                widget.click()
                self.log_action("click", selector=f"resourceId={switch_widget_rid}[checked=true]")
                self._sleep(0.6)
                return True
        except Exception as e:
            self.log_action("click", selector=f"resourceId={switch_widget_rid}[checked=true]", result="fail", error=e)

        try:
            txt = self.device(resourceId=switch_text_rid, text="On")
            if txt.exists:
                txt.click()
                self.log_action("click", selector=f"resourceId={switch_text_rid}[text=On]")
                self._sleep(0.6)
                return True
        except Exception as e:
            self.log_action("click", selector=f"resourceId={switch_text_rid}[text=On]", result="fail", error=e)
        return False

    def _click_bluetooth_scan_button(self) -> bool:
        selectors = (
            ("text=Scan", lambda: self.device(text="Scan").click()),
            ("text=SCAN", lambda: self.device(text="SCAN").click()),
            ("desc=Scan", lambda: self.device(description="Scan").click()),
            ("desc=SCAN", lambda: self.device(description="SCAN").click()),
        )
        for action_name, click_fn in selectors:
            try:
                matched = False
                if action_name.startswith("text="):
                    matched = self.device(text=action_name.split("=", 1)[1]).exists
                elif action_name.startswith("desc="):
                    matched = self.device(description=action_name.split("=", 1)[1]).exists
                if not matched:
                    continue
                return self.safe_click(
                    f"bluetooth_scan_button={action_name}",
                    click_fn,
                    expected_state_name="android_bluetooth_settings_screen",
                    expected_predicate=self._is_android_bluetooth_settings_screen,
                    timeout=2.0,
                    settle_sec=0.4,
                )
            except Exception as e:
                self.log_action("click", selector=f"bluetooth_scan_button={action_name}", result="fail", error=e)
        return False

    def _wait_for_bluetooth_toggle_state(self, expected_state: str, timeout_sec: float = 4.0) -> bool:
        expected = (expected_state or "").strip().lower()
        return self.wait_for_screen_state(
            f"android_bluetooth_toggle_{expected}",
            lambda: self._read_bluetooth_toggle_state() == expected,
            timeout=timeout_sec,
            interval=0.3,
            capture_on_timeout=False,
        )

    def _wait_for_bluetooth_toggle_stable(
        self,
        expected_state: str,
        timeout_sec: float = 6.0,
        stable_polls: int = 3,
        interval_sec: float = 0.4,
    ) -> bool:
        expected = (expected_state or "").strip().lower()
        stable_needed = max(1, int(stable_polls))
        settled, last_state, stable_count = self.wait_for_consecutive_match(
            action="bluetooth_toggle_stable",
            selector=expected,
            sample_fn=self._read_bluetooth_toggle_state,
            match_fn=lambda sample: sample == expected,
            timeout=max(0.5, float(timeout_sec)),
            stable_polls=stable_needed,
            interval=interval_sec,
            success_result="success",
            timeout_result="fail",
            success_artifacts_fn=lambda _sample, _stable: [f"stable_polls={stable_needed}", f"interval={interval_sec}s"],
            timeout_artifacts_fn=lambda sample, stable: [f"last_state={sample}", f"stable_count={stable}", f"required={stable_needed}"],
        )
        return settled

    def _phase_label(self, phase_key) -> str:
        if phase_key is None:
            normalized = "phase"
        else:
            normalized = str(phase_key).strip() or "phase"
        return self.PHASE_LABELS.get(normalized.lower(), normalized)

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

    def _accept_android_bluetooth_dialog(self) -> bool:
        positive_resource_ids = (
            "android:id/button1",
            "com.android.settings:id/button1",
        )
        positive_texts = (
            "OK",
            "Pair",
            "Connect",
            "Allow",
            "Yes",
            "Confirm",
        )

        for rid in positive_resource_ids:
            try:
                node = self.device(resourceId=rid)
                if node.exists:
                    node.click()
                    self.log_action("click", selector=f"bt_dialog_resource={rid}")
                    self._sleep(0.5)
                    return True
            except Exception as e:
                self.log_action("click", selector=f"bt_dialog_resource={rid}", result="fail", error=e)

        for text in positive_texts:
            try:
                node = self.device(text=text)
                if node.exists:
                    node.click()
                    self.log_action("click", selector=f"bt_dialog_text={text}")
                    self._sleep(0.5)
                    return True
            except Exception as e:
                self.log_action("click", selector=f"bt_dialog_text={text}", result="fail", error=e)
        return False

    def _is_android_bluetooth_pairing_dialog(self, target_name: str | None = None) -> bool:
        target = (target_name or "").strip()
        try:
            has_positive = self.device(resourceId="android:id/button1").exists or self.device(text="OK").exists
            if not has_positive:
                return False
            if self.device(textContains="Bluetooth pairing request").exists:
                return True
            if self.device(textContains="Pair with").exists:
                return True
            if target and self.device(textContains=target).exists:
                return True
        except Exception:
            return False
        return False

    def _wait_and_accept_android_bluetooth_dialog(self, target_name: str | None = None, timeout_sec: float = 4.0) -> bool:
        deadline = time.time() + max(0.5, float(timeout_sec))
        while time.time() < deadline:
            if self._is_android_bluetooth_pairing_dialog(target_name):
                clicked = self._accept_android_bluetooth_dialog()
                self.log_action(
                    "bluetooth_pairing_dialog",
                    selector=target_name or "unknown",
                    result="success" if clicked else "fail",
                    artifacts=[f"timeout={timeout_sec}s"],
                )
                return clicked
            self._sleep(0.25)
        self.log_action(
            "bluetooth_pairing_dialog",
            selector=target_name or "unknown",
            result="timeout",
            artifacts=[f"timeout={timeout_sec}s"],
        )
        return False

    def _is_android_bluetooth_unpair_dialog(self, target_name: str | None = None) -> bool:
        target = (target_name or "").strip()
        try:
            if target and self.device(textContains=f"Unpair {target}").exists:
                return True
            if self.device(text="Unpair").exists and (
                self.device(resourceId="android:id/button1").exists
                or self.device(resourceId="android:id/button2").exists
                or self.device(resourceId="android:id/button3").exists
            ):
                return True
            if self.device(textContains="connect to this device in the future").exists:
                return True
        except Exception:
            return False
        return False

    def _confirm_android_unpair_dialog(self, target_name: str | None = None, timeout_sec: float = 4.0) -> bool:
        deadline = time.time() + max(0.5, float(timeout_sec))
        while time.time() < deadline:
            if self._is_android_bluetooth_unpair_dialog(target_name):
                selectors = (
                    ("android:id/button1", lambda: self.device(resourceId="android:id/button1").click()),
                    ("com.android.settings:id/button1", lambda: self.device(resourceId="com.android.settings:id/button1").click()),
                    ("text=Unpair", lambda: self.device(text="Unpair").click()),
                    ("textContains=Unpair", lambda: self.device(textContains="Unpair").click()),
                )
                for selector, click_fn in selectors:
                    try:
                        if selector.startswith("android:id/") or selector.startswith("com.android.settings:id/"):
                            rid = selector
                            if not self.device(resourceId=rid).exists:
                                continue
                        elif selector == "text=Unpair":
                            if not self.device(text="Unpair").exists:
                                continue
                        else:
                            if not self.device(textContains="Unpair").exists:
                                continue
                        click_fn()
                        self.log_action("click", selector=f"bluetooth_unpair_confirm={selector}")
                        self._sleep(0.5)
                        return True
                    except Exception as e:
                        self.log_action("click", selector=f"bluetooth_unpair_confirm={selector}", result="fail", error=e)
            self._sleep(0.25)
        self.log_action(
            "bluetooth_unpair_confirm",
            selector=target_name or "unknown",
            result="timeout",
            artifacts=[f"timeout={timeout_sec}s"],
        )
        return False

    def _open_android_settings_home(self) -> bool:
        try:
            self.device.app_stop("com.android.settings")
            self.log_action("app_stop", selector="com.android.settings", artifacts=["reason=fresh_settings_home"])
            self._sleep(0.4)
        except Exception as e:
            self.log_action("app_stop", selector="com.android.settings", result="fail", error=e, artifacts=["reason=fresh_settings_home"])

        opened = self.safe_click(
            "app_start=com.android.settings",
            lambda: self.device.app_start("com.android.settings"),
            expected_state_name="android_settings_screen",
            expected_predicate=self._is_android_settings_screen,
            timeout=3.5,
            settle_sec=0.8,
        )
        if opened:
            return True

        commands = ["am start -a android.settings.SETTINGS"]
        bad_tokens = ("error", "exception", "permission denial", "not found", "unable", "denied")
        for cmd in commands:
            try:
                out = (get_adb_device(self.serial).shell(cmd) or "").strip()
                self.log_action("adb_shell", selector=cmd, artifacts=[out[:400]])
                lower_out = out.lower()
                if any(token in lower_out for token in bad_tokens):
                    continue
                if self.wait_for_screen_state(
                    "android_settings_screen",
                    self._is_android_settings_screen,
                    timeout=3.0,
                    interval=0.3,
                    capture_on_timeout=False,
                ):
                    return True
            except Exception as e:
                self.log_action("adb_shell", selector=cmd, result="fail", error=e)
        return False

    def _try_bluetooth_connect_via_settings(self, target_name: str, timeout_sec: float, return_home_on_done: bool = False) -> bool:
        if not target_name:
            return False
        if not self._open_android_bluetooth_page():
            return False

        scan_wait_sec = float(self.profile.get("bluetooth_settings_scan_wait_sec", 2.5))
        scan_retries = max(1, int(self.profile.get("bluetooth_settings_scan_retries", 2)))
        pair_dialog_timeout = float(self.profile.get("bluetooth_pair_dialog_timeout_sec", 4.0))

        self._wait_for_bluetooth_device_list_stable(
            timeout_sec=min(8.0, max(3.0, timeout_sec / 2.0)),
            stable_polls=int(self.profile.get("bluetooth_device_list_stable_polls", 2)),
            interval_sec=float(self.profile.get("bluetooth_device_list_stable_interval_sec", 0.6)),
            target_name=target_name,
            min_devices=int(self.profile.get("bluetooth_device_list_min_devices", 1)),
        )

        deadline = time.time() + max(1.0, float(timeout_sec))
        attempts = 0
        while time.time() < deadline:
            self._wait_and_accept_android_bluetooth_dialog(target_name, timeout_sec=min(1.0, pair_dialog_timeout))
            if bluetooth_target_connected(target_name, serial=self.serial):
                self.log_action("bluetooth_connected", selector=f"target={target_name}")
                self._post_bluetooth_connect_settle(target_name)
                if return_home_on_done:
                    self._return_home("bluetooth_target_connected")
                return True
            try:
                target_visible = (
                    self.device(textContains=target_name).exists
                    or self.device.xpath(f'//android.widget.TextView[@resource-id="android:id/title" and contains(@text, "{target_name.replace("\"", "\\\"")}")]').exists
                )
                if target_visible:
                    remaining = max(1.0, min(4.0, deadline - time.time()))

                    def _recover_connection():
                        self._wait_and_accept_android_bluetooth_dialog(target_name, timeout_sec=pair_dialog_timeout)
                        return self.wait_for_screen_state(
                            f"bluetooth_target_connected={target_name}",
                            lambda: bluetooth_target_connected(target_name, serial=self.serial),
                            timeout=max(1.0, min(3.0, remaining)),
                            interval=0.5,
                            capture_on_timeout=False,
                        )

                    connected = self._click_bluetooth_target_row(target_name, timeout_sec=remaining)
                    self._wait_and_accept_android_bluetooth_dialog(
                        target_name,
                        timeout_sec=min(pair_dialog_timeout, max(1.5, remaining)),
                    )
                    if not connected:
                        connected = _recover_connection()
                    self._wait_and_accept_android_bluetooth_dialog(target_name, timeout_sec=1.0)
                    if connected or bluetooth_target_connected(target_name, serial=self.serial):
                        self.log_action("bluetooth_connected", selector=f"target={target_name}")
                        self._post_bluetooth_connect_settle(target_name)
                        if return_home_on_done:
                            self._return_home("bluetooth_connect_via_settings")
                        return True
                elif attempts < scan_retries:
                    clicked_scan = self._click_bluetooth_scan_button()
                    self.log_action(
                        "bluetooth_scan_triggered_settings",
                        selector=target_name,
                        result="success" if clicked_scan else "fail",
                        artifacts=[f"try={attempts + 1}"],
                    )
                    if clicked_scan:
                        self._wait_for_bluetooth_device_list_stable(
                            timeout_sec=max(2.0, scan_wait_sec + 1.0),
                            stable_polls=int(self.profile.get("bluetooth_device_list_stable_polls", 2)),
                            interval_sec=float(self.profile.get("bluetooth_device_list_stable_interval_sec", 0.6)),
                            target_name=target_name,
                            min_devices=int(self.profile.get("bluetooth_device_list_min_devices", 1)),
                        )
            except Exception as e:
                self.log_action("click", selector=f"bluetooth_settings_target={target_name}", result="fail", error=e)
            attempts += 1
            if attempts % 3 == 0:
                try:
                    self.device(scrollable=True).scroll.forward(steps=20)
                    self.log_action("swipe", selector="bluetooth_settings_scroll_forward")
                except Exception:
                    pass
                self._wait_for_bluetooth_device_list_stable(
                    timeout_sec=min(5.0, max(2.0, timeout_sec / 3.0)),
                    stable_polls=int(self.profile.get("bluetooth_device_list_stable_polls", 2)),
                    interval_sec=float(self.profile.get("bluetooth_device_list_stable_interval_sec", 0.6)),
                    target_name=target_name,
                    min_devices=int(self.profile.get("bluetooth_device_list_min_devices", 1)),
                )
            self._sleep(0.7)

        self._wait_and_accept_android_bluetooth_dialog(target_name, timeout_sec=pair_dialog_timeout)
        connected = wait_for_bluetooth_target_connected(target_name, timeout_sec=float(timeout_sec), poll_sec=1.0, serial=self.serial)
        if connected:
            self._post_bluetooth_connect_settle(target_name)
            if return_home_on_done:
                self._return_home("bluetooth_wait_connected_done")
        return connected

    def _open_android_bluetooth_page(self) -> bool:
        if self._is_android_bluetooth_settings_screen():
            self.log_screen_transition("android_bluetooth_settings_open", result="already_open")
            return True

        direct_commands = [
            "am start -a android.settings.BLUETOOTH_SETTINGS",
            "am start -a android.settings.BLUETOOTH_SETTINGS -f 0x10008000",
        ]
        bad_tokens = ("error", "exception", "permission denial", "not found", "unable", "denied")
        for cmd in direct_commands:
            try:
                out = (get_adb_device(self.serial).shell(cmd) or "").strip()
                self.log_action("adb_shell", selector=cmd, artifacts=[out[:400]])
                lower_out = out.lower()
                if any(token in lower_out for token in bad_tokens):
                    continue
                if self.wait_for_screen_state(
                    "android_bluetooth_settings_screen",
                    self._is_android_bluetooth_settings_screen,
                    timeout=3.0,
                    interval=0.3,
                    capture_on_timeout=False,
                ):
                    self.log_screen_transition("android_bluetooth_settings_open", artifacts=["route=direct_intent"])
                    return True
            except Exception as e:
                self.log_action("adb_shell", selector=cmd, result="fail", error=e)

        if not self._open_android_settings_home():
            self.log_screen_transition("android_bluetooth_settings_open", result="fail", artifacts=["settings_home_not_opened"])
            return False

        opened_connections = self._is_android_connections_screen()
        opened_via_connections = False

        for label in ("Connections", "Connection", "Connected devices"):
            try:
                if not self.device(text=label).exists:
                    continue
                if self.safe_click(
                    f"settings_text={label}",
                    lambda label=label: self.device(text=label).click(),
                    expected_state_name="android_connections_screen",
                    expected_predicate=self._is_android_connections_screen,
                    timeout=1.5,
                    settle_sec=0.4,
                ):
                    opened_connections = True
                    opened_via_connections = True
                    break
            except Exception:
                continue

        if opened_connections:
            try:
                if self.safe_click(
                    "settings_textContains=Bluetooth",
                    lambda: self.device(textContains="Bluetooth").click(),
                    expected_state_name="android_bluetooth_settings_screen",
                    expected_predicate=self._is_android_bluetooth_settings_screen,
                    timeout=1.5,
                    settle_sec=0.4,
                ):
                    self.log_screen_transition(
                        "android_bluetooth_settings_open",
                        artifacts=[f"route={'connections' if opened_via_connections else 'already_on_connections'}"],
                    )
                    return True
            except Exception:
                pass

        self.log_action(
            "settings_connections_flow_fallback",
            selector="android_bluetooth_page",
            result="fail",
            artifacts=["connections_not_confirmed_or_bluetooth_not_opened"],
        )

        try:
            if self.safe_click(
                "settings_text=Bluetooth",
                lambda: self.device(text="Bluetooth").click(),
                expected_state_name="android_bluetooth_settings_screen",
                expected_predicate=self._is_android_bluetooth_settings_screen,
                timeout=1.0,
                settle_sec=0.4,
            ):
                self.log_screen_transition("android_bluetooth_settings_open", artifacts=["route=direct_text"])
                return True
        except Exception:
            pass

        try:
            if self.safe_click(
                "settings_textContains=Bluetooth(direct_fallback)",
                lambda: self.device(textContains="Bluetooth").click(),
                expected_state_name="android_bluetooth_settings_screen",
                expected_predicate=self._is_android_bluetooth_settings_screen,
                timeout=1.0,
                settle_sec=0.4,
            ):
                self.log_screen_transition("android_bluetooth_settings_open", artifacts=["route=direct_textContains"])
                return True
        except Exception:
            pass

        self.log_screen_transition("android_bluetooth_settings_open", result="fail")
        return False

    def _click_bluetooth_off_selector(self) -> bool:
        if self.device(text="Off").exists:
            self.device(text="Off").click()
            self.log_action("click", selector="Off")
            self._sleep(0.8)
            return True
        if self.device(textContains="Off").exists:
            self.device(textContains="Off").click()
            self.log_action("click", selector="textContains=Off")
            self._sleep(0.8)
            return True
        xp = '//android.widget.TextView[@resource-id="com.android.settings:id/switch_text" and @text="Off"]'
        try:
            node = self.device.xpath(xp)
            if node.wait(timeout=0.5):
                node.click()
                self.log_action("click", selector=f"bluetooth_toggle_selector={xp}")
                self._sleep(0.8)
                return True
        except Exception as e:
            self.log_action("click", selector=f"bluetooth_toggle_selector={xp}", result="fail", error=e)
        return False

    def _read_bluetooth_toggle_state(self) -> str:
        xp = '//android.widget.TextView[@resource-id="com.android.settings:id/switch_text"]'
        try:
            nodes = self.device.xpath(xp).all()
            for n in nodes:
                txt = (getattr(n, "text", "") or "").strip().lower()
                if txt == "on":
                    return "on"
                if txt == "off":
                    return "off"
        except Exception as e:
            self.log_action("read", selector="bluetooth_switch_text", result="fail", error=e)

        try:
            if self.device(text="On").exists and (not self.device(text="Off").exists):
                return "on"
            if self.device(text="Off").exists:
                return "off"
        except Exception:
            pass
        return "unknown"

    def _is_bluetooth_enabled_via_settings(self, open_page: bool = False) -> bool:
        if open_page and (not self._open_android_bluetooth_page()):
            return False
        return self._read_bluetooth_toggle_state() == "on"

    def _ensure_android_bluetooth_enabled(self, timeout_sec: float = 15.0, return_home_on_done: bool = False) -> bool:
        if not self._open_android_bluetooth_page():
            return False

        attempts = max(1, int(timeout_sec // 2))
        for _ in range(attempts):
            state = self._read_bluetooth_toggle_state()
            if state == "on":
                if return_home_on_done:
                    self._return_home("bluetooth_already_on")
                return True

            clicked = False
            if state == "off":
                clicked = self.safe_click(
                    "bluetooth_toggle=off_to_on",
                    self._click_bluetooth_off_selector,
                    expected_state_name="android_bluetooth_toggle_on",
                    expected_predicate=lambda: self._read_bluetooth_toggle_state() == "on",
                    timeout=3.0,
                    recovery_fn=lambda: self._accept_android_bluetooth_dialog() or self._wait_for_bluetooth_toggle_state("on", timeout_sec=2.0),
                    settle_sec=0.4,
                )
            if clicked:
                self._accept_android_bluetooth_dialog()
                toggle_stable = self._wait_for_bluetooth_toggle_stable(
                    "on",
                    timeout_sec=min(6.0, max(2.5, timeout_sec / 2.0)),
                    stable_polls=int(self.profile.get("bluetooth_toggle_stable_polls", 3)),
                    interval_sec=float(self.profile.get("bluetooth_toggle_stable_interval_sec", 0.4)),
                )
                if (self._read_bluetooth_toggle_state() == "on" or self._wait_for_bluetooth_toggle_state("on", timeout_sec=2.5)) and toggle_stable:
                    if return_home_on_done:
                        self._return_home("bluetooth_enabled")
                    return True
            else:
                self._sleep(0.5)

            self._accept_android_bluetooth_dialog()
            self._sleep(0.5)

        enabled = self._read_bluetooth_toggle_state() == "on"
        if enabled and return_home_on_done:
            self._return_home("bluetooth_enable_done")
        return enabled

    def _ensure_android_bluetooth_disabled(self, timeout_sec: float = 10.0, return_home_on_done: bool = False) -> bool:
        if not self._open_android_bluetooth_page():
            return False

        attempts = max(1, int(timeout_sec // 2))
        for _ in range(attempts):
            state = self._read_bluetooth_toggle_state()
            if state == "off":
                stable = self._wait_for_bluetooth_toggle_stable(
                    "off",
                    timeout_sec=min(5.0, max(2.0, timeout_sec / 2.0)),
                    stable_polls=int(self.profile.get("bluetooth_toggle_stable_polls", 3)),
                    interval_sec=float(self.profile.get("bluetooth_toggle_stable_interval_sec", 0.4)),
                )
                if stable:
                    if return_home_on_done:
                        self._return_home("bluetooth_already_off")
                    return True

            clicked = False
            if state == "on":
                clicked = self.safe_click(
                    "bluetooth_toggle=on_to_off",
                    self._click_bluetooth_on_selector,
                    expected_state_name="android_bluetooth_toggle_off",
                    expected_predicate=lambda: self._read_bluetooth_toggle_state() == "off",
                    timeout=3.0,
                    recovery_fn=lambda: self._accept_android_bluetooth_dialog() or self._wait_for_bluetooth_toggle_state("off", timeout_sec=2.0),
                    settle_sec=0.4,
                )
            if clicked:
                self._accept_android_bluetooth_dialog()
                stable = self._wait_for_bluetooth_toggle_stable(
                    "off",
                    timeout_sec=min(5.0, max(2.0, timeout_sec / 2.0)),
                    stable_polls=int(self.profile.get("bluetooth_toggle_stable_polls", 3)),
                    interval_sec=float(self.profile.get("bluetooth_toggle_stable_interval_sec", 0.4)),
                )
                if stable:
                    if return_home_on_done:
                        self._return_home("bluetooth_disabled")
                    return True
            else:
                self._sleep(0.5)

        disabled = self._read_bluetooth_toggle_state() == "off"
        if disabled and return_home_on_done:
            self._return_home("bluetooth_disable_done")
        return disabled

    def _open_bluetooth_device_settings(self, target_name: str) -> bool:
        target = (target_name or "").strip()
        if not target:
            return False

        escaped_target = target.replace('"', '\\"')
        xpaths = (
            f'//android.widget.ImageView[contains(@content-desc, "{escaped_target}") and contains(@content-desc, "Device settings")]',
            f'//android.widget.ImageView[contains(@content-desc, "{escaped_target}") and contains(@content-desc, "settings")]',
        )
        for xp in xpaths:
            try:
                node = self.device.xpath(xp)
                if node.wait(timeout=0.8):
                    if self.safe_click(
                        f"bluetooth_device_settings_xpath={xp}",
                        lambda node=node: node.click(),
                        expected_state_name=f"android_bluetooth_device_details={target}",
                        expected_predicate=lambda target=target: self._is_android_bluetooth_device_details_screen(target),
                        timeout=2.5,
                        settle_sec=0.5,
                    ):
                        return True
            except Exception as e:
                self.log_action("click", selector=f"bluetooth_device_settings_xpath={xp}", result="fail", error=e)

        try:
            pattern = rf".*{re.escape(target)}.*Device settings.*"
            node = self.device(className="android.widget.ImageView", descriptionMatches=pattern)
            if node.exists:
                if self.safe_click(
                    f"bluetooth_device_settings_descmatch={pattern}",
                    lambda: node.click(),
                    expected_state_name=f"android_bluetooth_device_details={target}",
                    expected_predicate=lambda target=target: self._is_android_bluetooth_device_details_screen(target),
                    timeout=2.5,
                    settle_sec=0.5,
                ):
                    return True
        except Exception as e:
            self.log_action("click", selector="bluetooth_device_settings_descmatch", result="fail", error=e)
        return False

    def _unpair_android_target(self, target_name: str, return_home_on_done: bool = True) -> dict:
        target = (target_name or "").strip()
        if not target:
            return {"ok": False, "reason": "empty_target_name", "detail": ""}

        if not self._open_android_bluetooth_page():
            return {"ok": False, "reason": "open_bluetooth_page_failed", "detail": ""}

        settings_opened = self._open_bluetooth_device_settings(target)
        self.log_screen_transition(
            "android_bluetooth_device_details_open",
            result="success" if settings_opened else "fail",
            artifacts=[target],
        )
        if not settings_opened:
            if return_home_on_done:
                self._return_home("bluetooth_device_settings_not_found")
            return {"ok": False, "reason": "device_settings_not_found", "detail": target}
        self._wait_for_bluetooth_device_details_stable(
            target,
            timeout_sec=float(self.profile.get("bluetooth_unpair_details_stable_timeout_sec", 5.0)),
            stable_polls=int(self.profile.get("bluetooth_unpair_details_stable_polls", 2)),
            interval_sec=float(self.profile.get("bluetooth_unpair_details_stable_interval_sec", 0.4)),
        )

        clicked = False
        try:
            if self.device(text="Unpair").exists:
                self.device(text="Unpair").click()
                self.log_action("click", selector="bluetooth_unpair_request=text=Unpair")
                self._sleep(0.5)
                clicked = True
            elif self.device(textContains="Unpair").exists:
                self.device(textContains="Unpair").click()
                self.log_action("click", selector="bluetooth_unpair_request=textContains=Unpair")
                self._sleep(0.5)
                clicked = True
        except Exception as e:
            self.log_action("click", selector="Unpair", result="fail", error=e)
        if clicked:
            confirmed = self._confirm_android_unpair_dialog(target, timeout_sec=4.0)
            if not confirmed:
                self._accept_android_bluetooth_dialog()
        unpair_stable = False
        if clicked:
            unpair_stable = self._wait_for_bluetooth_unpair_stable(
                target,
                timeout_sec=float(self.profile.get("bluetooth_unpair_stable_timeout_sec", 8.0)),
                stable_polls=int(self.profile.get("bluetooth_unpair_stable_polls", 2)),
                interval_sec=float(self.profile.get("bluetooth_unpair_stable_interval_sec", 0.5)),
            )
        if return_home_on_done:
            self._return_home("bluetooth_unpair_done")
        return {
            "ok": clicked and unpair_stable,
            "reason": "unpair_stable" if (clicked and unpair_stable) else ("unpair_clicked_unstable" if clicked else "unpair_text_not_found"),
            "detail": target,
        }

    def _return_to_bluetooth_settings_after_unpair(self, target_name: str) -> bool:
        target = (target_name or "").strip() or None
        try:
            if self._is_android_bluetooth_device_details_screen(target):
                self.device.press("back")
                self.log_action("press", selector="back", artifacts=["reason=bluetooth_unpair_to_settings"])
                if self.wait_for_screen_state(
                    "android_bluetooth_settings_screen",
                    self._is_android_bluetooth_settings_screen,
                    timeout=3.0,
                    interval=0.3,
                    capture_on_timeout=False,
                ):
                    return True
        except Exception as e:
            self.log_action("press", selector="back", result="fail", error=e, artifacts=["reason=bluetooth_unpair_to_settings"])

        if self._is_android_bluetooth_settings_screen():
            return True
        return self._open_android_bluetooth_page()

    def _teardown_bluetooth_pairing(self) -> dict:
        if not bool(self.profile.get("bluetooth_unpair_after_collection", True)):
            return {"ok": True, "reason": "skipped", "detail": "disabled"}

        previous_phase = self.current_phase
        self.current_phase = "cleanup"
        try:
            target = (self.bluetooth_target_name or "").strip()
            android_ret = self._unpair_android_target(target, return_home_on_done=False)
            self.log_action(
                "bluetooth_unpair_android",
                selector=target,
                result="success" if android_ret.get("ok") else "fail",
                artifacts=[android_ret.get("reason", ""), android_ret.get("detail", "")],
            )

            host_ret = {"ok": True, "reason": "skipped_windows_unpair_disabled", "detail": ""}
            restore_ret = {"ok": True, "reason": "skipped_restore", "detail": str(self.bluetooth_initial_state or "unknown")}
            restore_initial = bool(self.profile.get("bluetooth_restore_initial_state_after_collection", True))
            if restore_initial and (self.bluetooth_initial_state == "off"):
                self._return_to_bluetooth_settings_after_unpair(target)
                restored = self._ensure_android_bluetooth_disabled(timeout_sec=10.0, return_home_on_done=False)
                restore_ret = {
                    "ok": bool(restored),
                    "reason": "restored_off" if restored else "restore_off_failed",
                    "detail": "target=off",
                }
                self.log_action(
                    "bluetooth_restore_android",
                    selector=target or "global",
                    result="success" if restored else "fail",
                    artifacts=[restore_ret.get("reason", ""), restore_ret.get("detail", "")],
                )

            self._return_home("bluetooth_teardown_done")
            return {
                "ok": bool(android_ret.get("ok")) and bool(restore_ret.get("ok")),
                "android": android_ret,
                "windows": host_ret,
                "restore": restore_ret,
            }
        finally:
            self.current_phase = previous_phase

    def _run_bluetooth_preconnect(self, phase_label: str) -> dict:
        plan_enabled = bool(self.profile.get("bluetooth_preconnect_enabled", True))
        host_manual_mode = bool(self.profile.get("bluetooth_host_manual_mode", True))
        target_name = (self.bluetooth_target_name or "").strip()
        require_target = bool(self.profile.get("bluetooth_require_target", True))
        require_connected = bool(self.profile.get("bluetooth_preconnect_require_connected", True))
        allow_any_connected_fallback = bool(
            self.profile.get("bluetooth_allow_any_connected_fallback", host_manual_mode)
        )
        allow_paired_fallback = bool(
            self.profile.get("bluetooth_allow_paired_fallback", True)
        )
        allow_any_paired_fallback = bool(
            self.profile.get("bluetooth_allow_any_paired_fallback", host_manual_mode)
        )
        open_settings = bool(self.profile.get("bluetooth_preconnect_open_settings", True))
        connect_timeout = float(self.profile.get("bluetooth_preconnect_timeout_sec", 12))
        settings_timeout = float(self.profile.get("bluetooth_preconnect_settings_timeout_sec", 12))

        payload = {
            "phase": phase_label,
            "enabled": plan_enabled,
            "target": target_name,
            "required_target": require_target,
            "required_connected": require_connected,
            "host_manual_mode": host_manual_mode,
            "allow_paired_fallback": allow_paired_fallback,
            "allow_any_paired_fallback": allow_any_paired_fallback,
            "steps": [],
            "failures": [],
            "ok": True,
        }
        if not plan_enabled:
            payload["steps"].append({"step": "bluetooth_preconnect_disabled", "ok": True})
            return payload

        def device_preconnect_job():
            steps = []
            failures = []

            bt_on = self._is_bluetooth_enabled_via_settings(open_page=open_settings)
            bt_initial_state = "on" if bt_on else self._read_bluetooth_toggle_state()
            if self.bluetooth_initial_state in (None, "", "unknown"):
                self.bluetooth_initial_state = bt_initial_state
            steps.append({"step": "bluetooth_initial_state", "ok": bt_on, "state": bt_initial_state})
            if (not bt_on) and open_settings:
                bt_on = self._ensure_android_bluetooth_enabled(timeout_sec=settings_timeout, return_home_on_done=False)
                steps.append({"step": "bluetooth_enable_ui", "ok": bt_on})
            if not bt_on:
                failures.append("bluetooth_not_enabled")

            if require_target and not target_name:
                steps.append({"step": "target_name_present", "ok": False})
                failures.append("bluetooth_target_missing")
                return {"steps": steps, "failures": failures, "connected": False}

            steps.append({"step": "target_name_present", "ok": True})

            connected = False
            paired = False
            connected_required_ok = False
            if target_name:
                connected = bluetooth_target_connected(target_name, serial=self.serial)
                steps.append({"step": "target_connected_initial", "ok": connected})
                if not connected and open_settings:
                    connected = self._try_bluetooth_connect_via_settings(
                        target_name,
                        timeout_sec=settings_timeout,
                        return_home_on_done=False,
                    )
                    steps.append({"step": "connect_via_settings", "ok": connected})
                if not connected:
                    connected = wait_for_bluetooth_target_connected(
                        target_name,
                        timeout_sec=float(connect_timeout),
                        poll_sec=1.0,
                        serial=self.serial,
                    )
                    steps.append({"step": "wait_connected", "ok": connected})
                if not connected and allow_any_connected_fallback:
                    connected = bluetooth_any_device_connected(serial=self.serial)
                    steps.append({"step": "any_connected_fallback", "ok": connected})
                if not connected and allow_paired_fallback:
                    paired = bluetooth_target_paired(target_name, serial=self.serial)
                    steps.append({"step": "target_paired_fallback", "ok": paired})
                if not connected and (not paired) and allow_any_paired_fallback:
                    paired = bluetooth_any_device_paired(serial=self.serial)
                    steps.append({"step": "any_paired_fallback", "ok": paired})

            connected_required_ok = bool(connected) if require_connected else bool(connected or paired)
            if require_connected and target_name and not connected_required_ok:
                failures.append("bluetooth_target_not_connected")
            if require_connected and target_name:
                steps.append({"step": "target_connected_required", "ok": connected_required_ok})
            if (not require_connected) and target_name:
                steps.append({"step": "target_connected_or_paired_required", "ok": bool(connected or paired)})

            return {
                "steps": steps,
                "failures": failures,
                "connected": bool(connected),
                "paired": bool(paired),
                "connected_required_ok": connected_required_ok,
            }

        self.log_action(
            "bluetooth_preconnect_start",
            selector=phase_label,
            artifacts=[f"target={target_name or 'none'}"],
        )
        logger.info("%s Bluetooth preconnect start (target=%s)", self.aura_prefix(phase_label),
            target_name or "none",
        )

        device_ret = device_preconnect_job()

        payload["steps"].extend(device_ret.get("steps", []))
        payload["failures"].extend(device_ret.get("failures", []))
        payload["device_connect_result"] = {
            "connected": bool(device_ret.get("connected")),
            "paired": bool(device_ret.get("paired")),
            "connected_required_ok": bool(device_ret.get("connected_required_ok")),
        }

        payload["ok"] = len(payload["failures"]) == 0
        self.log_action(
            "bluetooth_preconnect_end",
            selector=phase_label,
            result="success" if payload["ok"] else "fail",
            artifacts=[
                f"failures={len(payload.get('failures', []))}",
                f"connected={bool((payload.get('device_connect_result') or {}).get('connected'))}",
                f"paired={bool((payload.get('device_connect_result') or {}).get('paired'))}",
            ],
        )
        logger.info("%s Bluetooth preconnect end (ok=%s, failures=%d)", self.aura_prefix(phase_label),
            payload["ok"],
            len(payload.get("failures", [])),
        )
        return payload

    def _enforce_network_policy(self, policy: dict) -> None:
        mode = (policy or {}).get("mode", "offline_airplane")
        enforce_dnd = (policy or {}).get("enforce_dnd", True)
        phase_label = self.current_phase or "phase"

        toggle_airplane_mode(True, serial=self.serial)
        self.log_action("phase_enforce", selector="airplane_mode", artifacts=["on"])
        logger.info("%s phase_enforce: airplane_mode=On", self.aura_prefix(phase_label))

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
                self.aura_prefix(phase_label),
                ret.get("method"),
                ret.get("ok"),
            )

        if mode == "offline_airplane":
            set_wifi_enabled(False, serial=self.serial)
            self.log_action("phase_enforce", selector="wifi", artifacts=["off"])
            logger.info("%s phase_enforce: wifi=off", self.aura_prefix(phase_label))
        elif mode == "online_wifi":
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

        bt_preconnect = self._run_bluetooth_preconnect(phase_label)
        checks.append(
            {
                "check": "bluetooth_preconnect",
                "ok": bool(bt_preconnect.get("ok")),
                "failures": bt_preconnect.get("failures", []),
            }
        )
        failures.extend(bt_preconnect.get("failures", []))

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
            "bluetooth_preconnect": bt_preconnect,
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

    def _collect_common(self, phase_key: str) -> dict:
        phase_label = self._phase_label(phase_key)
        self.current_phase = phase_label
        self._switch_phase_artifacts(phase_key)
        self.storage.begin_batch()
        started_at = datetime.now().astimezone().isoformat()
        started_ts = time.time()
        try:
            logger.info("%s App launch", self.aura_prefix(phase_label))
            self.log_action("app_launch", selector=self.packageName)
            self.launch_app(reason=f"phase={phase_label}")
            self._sleep(1.5)
            self.log_action("app_ready", selector=self.packageName)
            data = self.collect_chatrooms()
            self.flush_artifact_hashes()
            return {
                "status": "done",
                "phase": phase_label,
                "duration_sec": round(time.time() - started_ts, 3),
                "started_at": started_at,
                "ended_at": datetime.now().astimezone().isoformat(),
                "chatroom_count": len(data["chatrooms"]),
                "chat_list_screenshot_count": len(data["chat_list_screenshots"]),
                "export_count": data["export_count"],
                "artifact_dir": str(self.artifact_dir),
                "db_path": str(self.storage.db_path.resolve()),
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
        run_status = "running"
        unpair_result = None

        try:
            for item in phase_plan:
                if not isinstance(item, dict):
                    self.log_action("phase_plan_invalid", selector=str(item), result="fail")
                    phase_results.append(
                        {
                            "phase": "phase",
                            "status": "invalid_phase_plan_item",
                            "timing": {
                                "started_at": datetime.now().astimezone().isoformat(),
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": 0.0,
                            },
                        }
                    )
                    continue
                phase_key = item.get("name") or "phase"
                policy = item.get("policy") or {}
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
                        run_status = "preflight_failed"
                        return {"status": "preflight_failed", "phases": phase_results}
                    continue
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

            run_status = "done"
            if bool(self.profile.get("bluetooth_unpair_after_collection", True)):
                unpair_result = self._teardown_bluetooth_pairing()
            return {
                "status": "done",
                "phases": phase_results,
                "bluetooth_unpair": unpair_result,
            }
        finally:
            self._write_collection_timing(
                {
                    "run_id": self.run_id,
                    "target": self.profile.get("app_name", "WhatsApp"),
                    "method": "S2",
                    "started_at": run_started_at,
                    "ended_at": datetime.now().astimezone().isoformat(),
                    "duration_sec": round(time.time() - run_start_ts, 3),
                    "device_info": self._load_device_info(),
                    "app_info": {
                        "app_name": self.profile.get("app_name", "WhatsApp"),
                        "package_name": self.profile.get("package_name", self.packageName),
                        "app_version": self.profile.get("app_version", ""),
                        "collection_methods": self.profile.get("collection_methods", ["S2"]),
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
                    "run_status": run_status,
                    "bluetooth_unpair": unpair_result,
                }
            )
            self._restore_global_state(initial_state)
            self.current_phase = None
            self.current_account = None
            self.current_chat_id = None
            self.current_message_id = None


WhatsApp = WhatsAppCollector








