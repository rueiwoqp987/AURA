import json
import logging
import hashlib
import re
import shutil
import time
from pathlib import Path
from zipfile import ZipFile

from collectors.whatsapp.mixin_base import WhatsAppCollectorDeps
from collectors.whatsapp.export_parser import parse_whatsapp_export_zip
from collectors.whatsapp.xpath import (
    BACK_TO_CHAT_LIST_DESC_MARKERS,
    BACK_TO_CHAT_LIST_TEXT_MARKERS,
    BLUETOOTH_DEVICE_ITEM_XPATH,
    BLUETOOTH_LABELS,
    BLUETOOTH_SHARE_TARGET_PARENT_XPATH,
    BLUETOOTH_SHARE_TARGET_TEXT_XPATH,
    BLUETOOTH_SHARE_TARGET_XPATH,
    CHAT_MORE_LABELS,
    CHATS_TAB_LABELS,
    EXPORT_CHAT_LABELS,
    INCLUDE_MEDIA_LABELS,
    MORE_OPTIONS_DESC,
)
from utils.evidence import sha256_file
from utils.utils import (
    collect_windows_bluetooth_received_files,
    list_windows_bluetooth_receive_files,
    prepare_windows_bluetooth_connection,
    prepare_windows_bluetooth_receiver,
    scroll_down,
    scroll_up,
    wait_for_windows_bluetooth_receive_complete,
)

logger = logging.getLogger(__name__)


class WhatsAppChatroomsMixin(WhatsAppCollectorDeps):
    _BT_TARGET_SKIP_KEYWORDS = (
        "pair",
        "scan",
        "rename",
        "received",
        "available",
        "paired",
        "bluetooth",
    )

    def _wait_visual_stable(self, selector: str, *, predicate=None, timeout_sec: float = 1.4, stable_polls: int = 2, interval_sec: float = 0.18) -> bool:
        return self.wait_for_visual_stable(
            selector,
            predicate=predicate,
            timeout=timeout_sec,
            stable_polls=stable_polls,
            interval=interval_sec,
        )

    def _is_whatsapp_chat_list_screen(self) -> bool:
        try:
            has_chat_marker = any(self.device(text=label).exists for label in BACK_TO_CHAT_LIST_TEXT_MARKERS)
            has_search_marker = any(self.device(description=label).exists for label in BACK_TO_CHAT_LIST_DESC_MARKERS)
            has_chat_rows = self.device.xpath('//*[@resource-id="com.whatsapp:id/conversations_row_contact_name"]').exists
            return bool((has_chat_marker or has_search_marker) and (has_chat_rows or has_search_marker))
        except Exception:
            return False

    def _is_whatsapp_chatroom_screen(self) -> bool:
        try:
            has_more_options = any(self.device(description=label).exists for label in MORE_OPTIONS_DESC)
            has_chat_header = (
                self.device(resourceId="com.whatsapp:id/conversation_contact_name").exists
                or self.device(resourceId="com.whatsapp:id/conversation_contact_status").exists
            )
            has_entry = (
                self.device(resourceId="com.whatsapp:id/entry").exists
                or self.device(descriptionContains="Message").exists
                or self.device(textContains="Message").exists
            )
            has_read_only_chat = self.device(resourceId="com.whatsapp:id/read_only_chat_info").exists
            has_message_list = (
                self.device(resourceId="com.whatsapp:id/conversation_row_date_divider").exists
                or self.device(resourceId="com.whatsapp:id/name_in_group_tv").exists
                or self.device(resourceId="android:id/list").exists
            )
            return bool(has_more_options and has_chat_header and (has_entry or has_read_only_chat or has_message_list))
        except Exception:
            return False

    def _is_whatsapp_more_menu_screen(self) -> bool:
        try:
            return any(self.device(text=label).exists for label in CHAT_MORE_LABELS + EXPORT_CHAT_LABELS)
        except Exception:
            return False

    def _is_whatsapp_export_dialog_screen(self) -> bool:
        try:
            if any(self.device(text=label).exists for label in INCLUDE_MEDIA_LABELS):
                return True
            return bool(self.device(resourceId="android:id/button1").exists)
        except Exception:
            return False

    def _is_whatsapp_export_or_share_screen(self) -> bool:
        return self._is_whatsapp_export_dialog_screen() or self._is_android_share_sheet_screen()

    def _is_android_share_sheet_screen(self) -> bool:
        try:
            if self.device(textContains="Send chat via").exists:
                return True
            if self.device(resourceId="android:id/resolver_list").exists:
                return True
            current = self.device.app_current() or {}
            pkg = (current.get("package") or "").strip().lower()
            if "intentresolver" in pkg:
                return True
            if self.device(resourceId="com.android.intentresolver:id/sem_chooser_frame_root").exists:
                return True
            if self.device(resourceId="com.android.intentresolver:id/sem_chooser_recycler_ranked_app").exists:
                return True
            if self.device(resourceId="com.android.intentresolver:id/sem_chooser_recycler_direct_share").exists:
                return True
            if self.device(resourceId="android:id/profile_tabhost").exists:
                return True
            if any(self.device(text=label).exists for label in BLUETOOTH_LABELS):
                return True
            return (
                self.device.xpath(BLUETOOTH_SHARE_TARGET_XPATH).exists
                or self.device.xpath(BLUETOOTH_SHARE_TARGET_PARENT_XPATH).exists
                or self.device.xpath(BLUETOOTH_SHARE_TARGET_TEXT_XPATH).exists
            )
        except Exception:
            return False

    def _has_bluetooth_share_target(self) -> bool:
        try:
            if any(self.device(text=label).exists for label in BLUETOOTH_LABELS):
                return True
            return bool(
                self.device.xpath(BLUETOOTH_SHARE_TARGET_PARENT_XPATH).exists
                or self.device.xpath(BLUETOOTH_SHARE_TARGET_XPATH).exists
                or self.device.xpath(BLUETOOTH_SHARE_TARGET_TEXT_XPATH).exists
            )
        except Exception:
            return False

    def _swipe_share_sheet_targets(self, direction: str = "left", duration_sec: float = 0.12) -> bool:
        try:
            w, h = self.device.window_size()
            y = int(h * 0.86)
            if direction == "left":
                start_x = int(w * 0.82)
                end_x = int(w * 0.18)
            else:
                start_x = int(w * 0.18)
                end_x = int(w * 0.82)
            self.device.swipe(start_x, y, end_x, y, duration_sec)
            self.log_action(
                "swipe",
                selector=f"share_sheet_targets_{direction}",
                artifacts=[f"start={start_x},{y}", f"end={end_x},{y}", f"duration={duration_sec}s"],
            )
            self._wait_visual_stable(
                f"share_sheet_targets_{direction}",
                predicate=self._is_android_share_sheet_screen,
                timeout_sec=1.2,
                stable_polls=2,
                interval_sec=0.15,
            )
            return True
        except Exception as e:
            self.log_action("swipe", selector=f"share_sheet_targets_{direction}", result="fail", error=e)
            return False

    def _expand_share_sheet(self) -> bool:
        bluetooth_profile = getattr(self, "bluetooth_ui_profile", {}) or {}
        enabled = bool(
            bluetooth_profile.get(
                "share_sheet_expand_after_horizontal_scan",
                bluetooth_profile.get(
                    "share_sheet_expand_before_scan",
                    self.profile.get(
                        "bluetooth_share_sheet_expand_after_horizontal_scan",
                        self.profile.get("bluetooth_share_sheet_expand_before_scan", False),
                    ),
                ),
            )
        )
        if not enabled or self._has_bluetooth_share_target():
            return False

        try:
            w, h = self.device.window_size()
            start_x = int(w * 0.5)
            start_y = int(h * 0.88)
            end_y = int(h * 0.50)
            self.device.swipe(start_x, start_y, start_x, end_y, 0.18)
            self.log_action(
                "swipe",
                selector="share_sheet_expand",
                artifacts=[f"start={start_x},{start_y}", f"end={start_x},{end_y}", "duration=0.18s"],
            )
            self._wait_visual_stable(
                "share_sheet_expand",
                predicate=self._is_android_share_sheet_screen,
                timeout_sec=1.5,
                stable_polls=2,
                interval_sec=0.15,
            )
            return True
        except Exception as e:
            self.log_action("swipe", selector="share_sheet_expand", result="fail", error=e)
            return False

    def _wait_bluetooth_share_handoff(self, timeout_sec: float = 2.5) -> bool:
        return self._wait_visual_stable(
            "bluetooth_share_handoff",
            predicate=lambda: self._is_bluetooth_picker_closed() and (not self._is_android_share_sheet_screen()),
            timeout_sec=timeout_sec,
            stable_polls=2,
            interval_sec=0.18,
        )

    def _click_bluetooth_share_target_once(self) -> bool:
        if self.device.xpath(BLUETOOTH_SHARE_TARGET_PARENT_XPATH).exists:
            return self.safe_click(
                "share_target=Bluetooth(parent)",
                lambda: self.device.xpath(BLUETOOTH_SHARE_TARGET_PARENT_XPATH).click(),
                expected_state_name="android_bluetooth_picker_screen",
                expected_predicate=self._is_bluetooth_picker_screen,
                timeout=4.0,
                settle_sec=0.2,
            )
        if self.device.xpath(BLUETOOTH_SHARE_TARGET_XPATH).exists:
            return self.safe_click(
                "share_target=Bluetooth",
                lambda: self.device.xpath(BLUETOOTH_SHARE_TARGET_XPATH).click(),
                expected_state_name="android_bluetooth_picker_screen",
                expected_predicate=self._is_bluetooth_picker_screen,
                timeout=4.0,
                settle_sec=0.2,
            )
        if self.device.xpath(BLUETOOTH_SHARE_TARGET_TEXT_XPATH).exists:
            return self.safe_click(
                "share_target=Bluetooth(text)",
                lambda: self.device.xpath(BLUETOOTH_SHARE_TARGET_TEXT_XPATH).click(),
                expected_state_name="android_bluetooth_picker_screen",
                expected_predicate=self._is_bluetooth_picker_screen,
                timeout=4.0,
                settle_sec=0.2,
            )
        return self._click_first_text(
            BLUETOOTH_LABELS,
            delay_sec=0.2,
            expected_state_name="android_bluetooth_picker_screen",
            expected_predicate=self._is_bluetooth_picker_screen,
            timeout=4.0,
        )

    def _click_bluetooth_share_target(self) -> bool:
        if self._click_bluetooth_share_target_once():
            return True

        max_swipes = max(1, int(self.profile.get("bluetooth_share_sheet_swipes", 4)))
        directions = ("left", "right")

        def scan_horizontal(stage: str) -> bool:
            for direction in directions:
                for idx in range(max_swipes):
                    if not self._swipe_share_sheet_targets(direction):
                        break
                    self.log_action(
                        "share_sheet_bluetooth_scan",
                        selector="Bluetooth",
                        artifacts=[f"swipe_try={idx + 1}", f"direction={direction}", f"stage={stage}"],
                    )
                    if self._has_bluetooth_share_target() and self._click_bluetooth_share_target_once():
                        return True
            return False

        if scan_horizontal("collapsed"):
            return True

        if self._expand_share_sheet():
            if self._click_bluetooth_share_target_once():
                return True
            if scan_horizontal("expanded"):
                return True

        return False

    def _is_bluetooth_picker_screen(self) -> bool:
        try:
            return bool(self.device.xpath(BLUETOOTH_DEVICE_ITEM_XPATH).exists or self.device(resourceId="android:id/button1").exists)
        except Exception:
            return False

    def _is_bluetooth_picker_ready(self) -> bool:
        if not self._is_bluetooth_picker_screen():
            return False
        try:
            return bool(
                self.device.xpath(BLUETOOTH_DEVICE_ITEM_XPATH).exists
                or self.device(text="Scan").exists
                or self.device(text="SCAN").exists
                or self.device(text="Refresh").exists
                or self.device(description="Scan").exists
                or self.device(description="SCAN").exists
                or self.device(description="Refresh").exists
                or self.device(resourceId="android:id/button1").exists
            )
        except Exception:
            return False

    def _is_bluetooth_picker_closed(self) -> bool:
        return not self._is_bluetooth_picker_screen()

    def _current_chat_list_signature(self) -> tuple[str, ...]:
        try:
            return tuple(self._extract_chat_names())
        except Exception:
            return tuple()

    def _click_first_text(self, labels: tuple[str, ...], delay_sec: float = 0.8, expected_state_name=None, expected_predicate=None, timeout: float | None = None) -> bool:
        for label in labels:
            try:
                if self.device(text=label).exists:
                    return self.safe_click(
                        f"text={label}",
                        lambda label=label: self.device(text=label).click(),
                        expected_state_name=expected_state_name,
                        expected_predicate=expected_predicate,
                        timeout=timeout,
                        settle_sec=delay_sec,
                    )
            except Exception as e:
                self.log_action("click", selector=f"text={label}", result="fail", error=e)
        return False

    def _click_first_desc(self, labels: tuple[str, ...], delay_sec: float = 0.8, expected_state_name=None, expected_predicate=None, timeout: float | None = None) -> bool:
        for label in labels:
            try:
                if self.device(description=label).exists:
                    return self.safe_click(
                        f"desc={label}",
                        lambda label=label: self.device(description=label).click(),
                        expected_state_name=expected_state_name,
                        expected_predicate=expected_predicate,
                        timeout=timeout,
                        settle_sec=delay_sec,
                    )
            except Exception as e:
                self.log_action("click", selector=f"desc={label}", result="fail", error=e)
        return False

    def _tap_chats_tab(self) -> bool:
        return self._click_first_text(
            CHATS_TAB_LABELS,
            delay_sec=0.2,
            expected_state_name="whatsapp_chat_list_screen",
            expected_predicate=self._is_whatsapp_chat_list_screen,
            timeout=2.5,
        )

    def _open_chat_by_name(self, chat_name: str) -> bool:
        selectors = [
            {"resourceId": "com.whatsapp:id/conversations_row_contact_name", "text": chat_name},
            {"text": chat_name},
        ]
        for selector in selectors:
            try:
                node = self.device(**selector)
                if node.exists:
                    return self.safe_click(
                        f"open_chat={chat_name}",
                        lambda node=node: node.click(),
                        expected_state_name="whatsapp_chatroom_screen",
                        expected_predicate=self._is_whatsapp_chatroom_screen,
                        timeout=3.0,
                        settle_sec=0.2,
                    )
            except Exception as e:
                self.log_action("open_chat", selector=str(selector), result="fail", error=e)
        return False

    def _back_to_chat_list(self, *, reason: str, max_back: int = 4, timeout: float = 2.0) -> bool:
        return self.press_back_to_state(
            reason=reason,
            state_name="whatsapp_chat_list_screen",
            predicate=self._is_whatsapp_chat_list_screen,
            max_back=max_back,
            timeout=timeout,
            interval=0.2,
        )

    def _scroll_to_top(self) -> None:
        try:
            self.device(scrollable=True).scroll.toBeginning(max_swipes=10, steps=6)
            self.log_action("scroll_to_top", selector="uiautomator")
            self.wait_for_screen_state(
                "whatsapp_chat_list_screen",
                self._is_whatsapp_chat_list_screen,
                timeout=2.0,
                interval=0.2,
                capture_on_timeout=False,
            )
            return
        except Exception as e:
            self.log_action("scroll_to_top", selector="uiautomator", result="fail", error=e)

        for _ in range(8):
            scroll_up(self.device)
            self.log_action("swipe", selector="scroll_up")
            self._sleep(0.4)

    def _discover_chatrooms(self, list_dir) -> tuple[list[dict], list[str]]:
        max_swipes = int(self.profile.get("chat_list_max_swipes", 20))
        screenshot_interval = max(1, int(self.profile.get("chat_list_snapshot_interval_pages", 2)))

        self.log_action(
            "chat_list_discovery_start",
            selector=self.current_phase,
            artifacts=[f"max_swipes={max_swipes}", f"snapshot_interval={screenshot_interval}"],
        )
        logger.info(
            "%s Chat list discovery start (max_swipes=%d, snapshot_interval=%d)",
            self.aura_prefix(self.current_phase or "phase"),
            max_swipes,
            screenshot_interval,
        )
        if not self._tap_chats_tab():
            self.log_action("chat_list_discovery_start", selector=self.current_phase, result="chat_list_state_unconfirmed")
        self._scroll_to_top()

        discovered: dict[str, dict] = {}
        list_shots: list[str] = []
        previous_signature = ()
        stagnant_pages = 0
        pages_since_capture = 0

        for _ in range(max_swipes):
            names = self._extract_chat_names()
            signature = tuple(names)
            if signature == previous_signature:
                stagnant_pages += 1
            else:
                stagnant_pages = 0
            previous_signature = signature

            new_count = 0
            for name in names:
                chat_id = self._build_chat_id(name)
                if chat_id in discovered:
                    continue
                discovered[chat_id] = {
                    "chat_id": chat_id,
                    "name": name,
                    "type": "Unknown",
                    "artifacts": [],
                }
                new_count += 1

            should_capture = (
                len(list_shots) == 0
                or new_count > 0
                or pages_since_capture >= screenshot_interval
            )
            if should_capture:
                shot_path = list_dir / f"chat_list_discovery_{len(list_shots)}.jpg"
                ev = self.capture_visual_evidence(
                    shot_path,
                    screenshot_kind="screenshot_chat_list",
                    uitree_kind="uitree_chat_list",
                )
                list_shots.append(ev["screenshot_path"])
                artifacts = [ev["screenshot_path"], f"new={new_count}", f"total={len(discovered)}"]
                if ev.get("uitree_path"):
                    artifacts.append(ev["uitree_path"])
                self.log_action("chat_list_discovery_snapshot", selector=self.current_phase, artifacts=artifacts)
                pages_since_capture = 0
            else:
                pages_since_capture += 1

            if stagnant_pages >= 1 and new_count == 0:
                break

            before_scroll_signature = self._current_chat_list_signature()
            scroll_down(self.device)
            self.log_action("swipe", selector="scroll_down")
            self.wait_for_list_changed(
                "whatsapp_chat_list_scroll_down",
                before_scroll_signature,
                self._current_chat_list_signature,
                timeout=1.5,
                interval=0.2,
            )

        chatrooms = list(discovered.values())
        self.log_action(
            "chat_list_discovery_done",
            selector=self.current_phase,
            artifacts=[f"count={len(chatrooms)}", f"screenshots={len(list_shots)}"],
        )
        logger.info(
            "%s Chat list discovery done (chatrooms=%d, screenshots=%d)",
            self.aura_prefix(self.current_phase or "phase"),
            len(chatrooms),
            len(list_shots),
        )
        return chatrooms, list_shots

    def _select_bluetooth_target(self, target_name: str, allow_fallback: bool) -> tuple[bool, str]:
        picker_wait_sec = int(self.profile.get("bluetooth_picker_wait_sec", 12))
        scan_wait_sec = float(self.profile.get("bluetooth_scan_wait_sec", 2.5))
        max_scan_retries = max(1, int(self.profile.get("bluetooth_scan_retries", 2)))

        def _collect_visible_labels() -> list[str]:
            labels = []
            for node in self.device.xpath(BLUETOOTH_DEVICE_ITEM_XPATH).all():
                label = (node.text or "").strip()
                if label:
                    labels.append(label)
            return labels

        def _wait_picker_items(timeout_sec: int) -> bool:
            deadline = time.time() + max(1, timeout_sec)
            while time.time() < deadline:
                if self.wait_for_screen_state(
                    "android_bluetooth_picker_ready",
                    self._is_bluetooth_picker_ready,
                    timeout=min(1.5, max(0.5, deadline - time.time())),
                    interval=0.3,
                    capture_on_timeout=False,
                ):
                    return True
                if self.device(resourceId="android:id/button1").exists:
                    self.safe_click(
                        "resourceId=android:id/button1",
                        lambda: self.device(resourceId="android:id/button1").click(),
                        expected_state_name="android_bluetooth_picker_screen",
                        expected_predicate=self._is_bluetooth_picker_screen,
                        timeout=2.0,
                        settle_sec=0.3,
                    )
                self._sleep(0.5)
            return self._is_bluetooth_picker_ready()

        def _click_scan_button() -> bool:
            scan_labels = ("Scan", "SCAN", "Refresh")
            for label in scan_labels:
                try:
                    if self.device(text=label).exists:
                        return self.safe_click(
                            f"bluetooth_scan={label}",
                            lambda label=label: self.device(text=label).click(),
                            expected_state_name="android_bluetooth_picker_screen",
                            expected_predicate=self._is_bluetooth_picker_screen,
                            timeout=2.0,
                            settle_sec=0.3,
                        )
                except Exception as e:
                    self.log_action("click", selector=f"bluetooth_scan={label}", result="fail", error=e)

            for label in scan_labels:
                try:
                    if self.device(description=label).exists:
                        return self.safe_click(
                            f"bluetooth_scan_desc={label}",
                            lambda label=label: self.device(description=label).click(),
                            expected_state_name="android_bluetooth_picker_screen",
                            expected_predicate=self._is_bluetooth_picker_screen,
                            timeout=2.0,
                            settle_sec=0.3,
                        )
                except Exception as e:
                    self.log_action("click", selector=f"bluetooth_scan_desc={label}", result="fail", error=e)
            return False

        picker_ready = _wait_picker_items(picker_wait_sec)
        self.log_action(
            "bluetooth_picker_ready",
            selector="picker",
            result="success" if picker_ready else "fail",
            artifacts=[f"timeout={picker_wait_sec}s"],
        )

        visible_labels = _collect_visible_labels()
        if visible_labels:
            self.log_action("bluetooth_targets_visible", selector="picker", artifacts=visible_labels)
            logger.info("%s Bluetooth targets visible (count=%d)", self.aura_prefix(self.current_phase or "phase"), len(visible_labels))

        if target_name:
            try:
                if self.device(textContains=target_name).exists:
                    if self.safe_click(
                        f"bluetooth_device={target_name}",
                        lambda: self.device(textContains=target_name).click(),
                        expected_state_name="android_bluetooth_picker_closed",
                        expected_predicate=self._is_bluetooth_picker_closed,
                        timeout=4.0,
                        settle_sec=0.4,
                    ):
                        logger.info("%s Bluetooth target selected (matched=%s)", self.aura_prefix(self.current_phase or "phase"), target_name)
                        return True, f"matched:{target_name}"
            except Exception as e:
                self.log_action("click", selector=f"bluetooth_device={target_name}", result="fail", error=e)

            for scan_try in range(max_scan_retries):
                clicked_scan = _click_scan_button()
                if clicked_scan:
                    self.log_action("bluetooth_scan_triggered", selector="picker", artifacts=[f"try={scan_try+1}"])
                    _wait_picker_items(max(2, int(scan_wait_sec) + 1))
                    visible_labels = _collect_visible_labels()
                    if visible_labels:
                        self.log_action("bluetooth_targets_visible", selector="picker_after_scan", artifacts=visible_labels)
                    try:
                        if self.device(textContains=target_name).exists:
                            if self.safe_click(
                                f"bluetooth_device={target_name}",
                                lambda: self.device(textContains=target_name).click(),
                                expected_state_name="android_bluetooth_picker_closed",
                                expected_predicate=self._is_bluetooth_picker_closed,
                                timeout=4.0,
                                settle_sec=0.4,
                            ):
                                logger.info("%s Bluetooth target selected after scan (matched=%s)", self.aura_prefix(self.current_phase or "phase"), target_name)
                                return True, f"matched_after_scan:{target_name}"
                    except Exception as e:
                        self.log_action("click", selector=f"bluetooth_device={target_name}", result="fail", error=e)
                else:
                    self.log_action("bluetooth_scan_unavailable", selector="picker", artifacts=[f"try={scan_try+1}"])
                    break

        if not allow_fallback:
            return False, "target_not_found"

        try:
            for node in self.device.xpath(BLUETOOTH_DEVICE_ITEM_XPATH).all():
                label = (node.text or "").strip()
                lower_label = label.lower()
                if not label:
                    continue
                if any(k in lower_label for k in self._BT_TARGET_SKIP_KEYWORDS):
                    continue
                if self.safe_click(
                    f"bluetooth_device_fallback={label}",
                    lambda node=node: node.click(),
                    expected_state_name="android_bluetooth_picker_closed",
                    expected_predicate=self._is_bluetooth_picker_closed,
                    timeout=4.0,
                    settle_sec=0.4,
                ):
                    logger.info("%s Bluetooth target selected (fallback=%s)", self.aura_prefix(self.current_phase or "phase"), label)
                    return True, f"fallback:{label}"
        except Exception as e:
            self.log_action("click", selector="bluetooth_device_fallback", result="fail", error=e)

        logger.info("%s Bluetooth target selection failed", self.aura_prefix(self.current_phase or "phase"))
        return False, "fallback_target_not_found"

    def _chat_export_dir(self, export_dir, chat_name: str, chat_id: str):
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", (chat_name or "").strip()).strip("._-")
        if not safe_name:
            safe_name = "chat"
        folder = export_dir / f"{safe_name}_{chat_id[:8]}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _whatsapp_export_attempt_id(self, chat_id: str, entry: dict, status: str) -> str:
        raw = "|".join(
            [
                self.run_id,
                self.current_phase or "",
                chat_id or "",
                str(entry.get("record_id") or ""),
                str(entry.get("message_id") or ""),
                str(entry.get("observation_id") or ""),
                str(entry.get("file_name") or entry.get("zip_member") or ""),
                status,
            ]
        )
        return "wa_export_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]

    def _whatsapp_export_operation_id(self, chat_id: str, archive_path, action: str = "process") -> str:
        p = Path(archive_path)
        try:
            stat = p.stat()
            size = str(int(stat.st_size))
            mtime = str(int(stat.st_mtime_ns))
        except Exception:
            size = ""
            mtime = ""
        raw = "|".join(
            [
                self.run_id,
                self.current_phase or "",
                chat_id or "",
                action,
                p.name,
                size,
                mtime,
            ]
        )
        return "op_whatsapp_export_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]

    def _file_size_sha256(self, path) -> tuple[int | None, str | None]:
        try:
            p = Path(path)
            return int(p.stat().st_size), sha256_file(p)
        except Exception:
            return None, None

    def _export_current_chat(self, chat_name: str, chat_id: str, export_dir) -> dict:
        self.current_chat_id = chat_id
        artifacts: list[str] = []
        status = "fail"
        bluetooth_clicked = False
        host_manual_mode = bool(self.profile.get("bluetooth_host_manual_mode", True))
        bluetooth_target = (self.bluetooth_target_name or "").strip()
        require_target = bool(self.profile.get("bluetooth_require_target", True))
        allow_fallback_target = bool(self.profile.get("bluetooth_allow_fallback_target", True))
        wait_transfer_complete = bool(self.profile.get("bluetooth_wait_transfer_complete", True))
        require_host_confirmation = bool(self.profile.get("bluetooth_require_host_confirmation", False))
        host_confirm_timeout = float(self.profile.get("bluetooth_host_confirm_timeout_sec", 45))
        host_confirm_poll = float(self.profile.get("bluetooth_host_confirm_poll_sec", 1.0))
        host_prepare_before_export = bool(self.profile.get("bluetooth_prepare_host_before_export", True))
        host_prepare_timeout = float(self.profile.get("bluetooth_prepare_host_timeout_sec", 10))
        host_prepare_enforce = bool(self.profile.get("bluetooth_prepare_host_enforce", False))
        host_prepare_open_settings = bool(self.profile.get("bluetooth_prepare_host_open_settings", True))
        host_prepare_ensure_service = bool(self.profile.get("bluetooth_prepare_host_ensure_service", True))
        host_prepare_pairing_wait = float(self.profile.get("bluetooth_prepare_host_pairing_wait_sec", 15.0))
        host_prepare_pairing_enforce = bool(self.profile.get("bluetooth_prepare_host_pairing_enforce", False))
        host_receive_timeout = float(self.profile.get("bluetooth_receive_collect_timeout_sec", max(20.0, host_confirm_timeout)))
        host_receive_poll = float(self.profile.get("bluetooth_receive_collect_poll_sec", 1.0))
        host_receive_scan_depth = int(self.profile.get("bluetooth_receive_scan_depth", 2))
        host_receive_strict_exchange = bool(self.profile.get("bluetooth_receive_strict_exchange_dir", True))
        host_receive_allowed_ext = self.profile.get("bluetooth_receive_allowed_extensions", [".zip", ".txt"])
        host_receive_min_size = int(self.profile.get("bluetooth_receive_min_size_bytes", 1))
        require_received_file = bool(self.profile.get("bluetooth_require_received_file", wait_transfer_complete))
        receiver_precheck_enforce = bool(self.profile.get("bluetooth_receiver_precheck_enforce", True))
        receiver_retry_enabled = bool(self.profile.get("bluetooth_receiver_retry_on_missing", True))
        receiver_retry_timeout = float(self.profile.get("bluetooth_receiver_retry_timeout_sec", 8.0))
        chat_export_dir = self._chat_export_dir(export_dir, chat_name, chat_id)
        receive_baseline = list_windows_bluetooth_receive_files(
            max_depth=host_receive_scan_depth,
            strict_exchange_only=host_receive_strict_exchange,
        )
        receive_start_ts = time.time()
        export_start_ts = time.time()
        logger.info("%s Export chat start: chat=%s chat_id=%s", self.aura_prefix(self.current_phase or "phase"), chat_name, chat_id)
        self.log_action("export_chat_start", selector=chat_name, artifacts=[chat_id])
        expected_file_names: list[str] = []
        expected_file_contains: list[str] = []
        received_files: list[str] = []
        parsed_count = 0

        try:
            if host_manual_mode:
                self.log_action(
                    "bluetooth_host_manual_required",
                    selector=chat_name,
                    artifacts=["accept_windows_notification_manually"],
                )

            host_prep = None
            if host_prepare_before_export:
                host_prep = prepare_windows_bluetooth_connection(
                    target_name=bluetooth_target,
                    ensure_service=host_prepare_ensure_service,
                    open_settings=host_prepare_open_settings,
                    pairing_wait_sec=host_prepare_pairing_wait,
                    receiver_timeout_sec=host_prepare_timeout,
                    enforce_pairing_prompt=host_prepare_pairing_enforce,
                    manual_pairing_approval=host_manual_mode,
                )
                artifacts.append(f"host_prepare_ok={host_prep.get('ok')}")
                logger.info("%s Host bluetooth prepare (ok=%s, failures=%d)", self.aura_prefix(self.current_phase or "phase"), bool(host_prep.get("ok")), len(host_prep.get("failures", []) or []))
                self.log_action("export_chat_stage", selector=chat_name, artifacts=["host_prepare"])
                self.log_action(
                    "bluetooth_host_prepare",
                    selector=chat_name,
                    result="success" if host_prep.get("ok") else "fail",
                    artifacts=[
                        json.dumps(host_prep.get("failures", []), ensure_ascii=False),
                        host_prep.get("receiver", {}).get("reason", ""),
                    ],
                )
                if host_prepare_enforce and not host_prep.get("ok"):
                    self.log_action(
                        "export_chat",
                        selector=chat_name,
                        result="fail",
                        artifacts=artifacts + ["host_prepare_failed"],
                    )
                    return {"status": status, "artifacts": artifacts}

            host_prep_receiver = (host_prep or {}).get("receiver") or {}
            if host_prep_receiver and host_prep_receiver.get("ok"):
                receiver_precheck = {
                    "ok": True,
                    "reason": f"host_prepare_{host_prep_receiver.get('reason', 'receiver_ready')}",
                    "detail": host_prep_receiver.get("detail", ""),
                }
            else:
                receiver_precheck = prepare_windows_bluetooth_receiver(
                    timeout_sec=host_prepare_timeout,
                    prefer_fresh_start=host_manual_mode,
                )
            logger.info("%s Host receiver precheck (ok=%s, reason=%s)", self.aura_prefix(self.current_phase or "phase"), bool(receiver_precheck.get("ok")), receiver_precheck.get("reason", ""))
            self.log_action("export_chat_stage", selector=chat_name, artifacts=["receiver_precheck"])
            artifacts.append(f"receiver_precheck={receiver_precheck.get('reason', '')}")
            self.log_action(
                "bluetooth_receiver_precheck",
                selector=chat_name,
                result="success" if receiver_precheck.get("ok") else "fail",
                artifacts=[receiver_precheck.get("reason", ""), receiver_precheck.get("detail", "")],
            )
            if receiver_precheck_enforce and (not receiver_precheck.get("ok")):
                self.log_action(
                    "export_chat",
                    selector=chat_name,
                    result="fail",
                    artifacts=artifacts + ["bluetooth_receiver_precheck_failed"],
                )
                return {"status": status, "artifacts": artifacts}

            if not self._click_first_desc(
                MORE_OPTIONS_DESC,
                delay_sec=0.2,
                expected_state_name="whatsapp_more_menu_screen",
                expected_predicate=self._is_whatsapp_more_menu_screen,
                timeout=2.0,
            ):
                self.log_action("export_chat", selector=chat_name, result="fail", artifacts=["more_options_not_found"])
                return {"status": status, "artifacts": artifacts}
            self.log_action("export_chat_stage", selector=chat_name, artifacts=["more_options_opened"])

            more_menu_ready = self._click_first_text(
                CHAT_MORE_LABELS,
                delay_sec=0.2,
                expected_state_name="whatsapp_chat_more_menu_screen",
                expected_predicate=lambda: any(self.device(text=label).exists for label in EXPORT_CHAT_LABELS),
                timeout=2.0,
            )
            if more_menu_ready:
                self.log_action("export_chat_stage", selector=chat_name, artifacts=["chat_more_menu_opened"])
            elif not any(self.device(text=label).exists for label in EXPORT_CHAT_LABELS):
                self.log_action("export_chat", selector=chat_name, result="fail", artifacts=["chat_more_menu_not_found"])
                return {"status": status, "artifacts": artifacts}

            if not self._click_first_text(
                EXPORT_CHAT_LABELS,
                delay_sec=0.2,
                expected_state_name="whatsapp_export_or_share_screen",
                expected_predicate=self._is_whatsapp_export_or_share_screen,
                timeout=5.0,
            ):
                self.log_action("export_chat", selector=chat_name, result="fail", artifacts=["export_menu_not_found"])
                return {"status": status, "artifacts": artifacts}
            self.log_action("export_chat_stage", selector=chat_name, artifacts=["export_menu_opened"])

            include_ok = self._is_android_share_sheet_screen()
            if include_ok:
                self.log_action("export_chat_stage", selector=chat_name, artifacts=["share_sheet_direct"])
            else:
                include_ok = self._click_first_text(
                    INCLUDE_MEDIA_LABELS,
                    delay_sec=0.2,
                    expected_state_name="android_share_sheet_screen",
                    expected_predicate=self._is_android_share_sheet_screen,
                    timeout=5.0,
                )
                if not include_ok and self.device(resourceId="android:id/button1").exists:
                    include_ok = self.safe_click(
                        "resourceId=android:id/button1",
                        lambda: self.device(resourceId="android:id/button1").click(),
                        expected_state_name="android_share_sheet_screen",
                        expected_predicate=self._is_android_share_sheet_screen,
                        timeout=5.0,
                        settle_sec=0.2,
                    )
            if not include_ok:
                self.log_action("export_chat", selector=chat_name, result="fail", artifacts=["include_media_not_found"])
                return {"status": status, "artifacts": artifacts}
            self.log_action("export_chat_stage", selector=chat_name, artifacts=["share_sheet_ready"])

            shot_path = chat_export_dir / f"{chat_id}_share.jpg"
            ev = self.capture_visual_evidence(
                shot_path,
                screenshot_kind="screenshot_export_share",
                uitree_kind="uitree_export_share",
                chat_id=chat_id,
                source_action="capture_export_share_sheet",
                source_screen="android_share_sheet",
            )
            artifacts.append(ev["screenshot_path"])
            if ev.get("uitree_path"):
                artifacts.append(ev["uitree_path"])

            bluetooth_clicked = self._click_bluetooth_share_target()

            if bluetooth_clicked:
                logger.info("%s Export share target clicked: Bluetooth (chat=%s)", self.aura_prefix(self.current_phase or "phase"), chat_name)
                self.log_action("export_chat_stage", selector=chat_name, artifacts=["bluetooth_share_clicked"])

            if not bluetooth_clicked:
                self.log_action("export_chat", selector=chat_name, result="fail", artifacts=["bluetooth_option_not_clicked"])
                return {"status": status, "artifacts": artifacts}

            target_selected = False
            target_detail = "target_skipped"
            if require_target or bluetooth_target:
                target_selected, target_detail = self._select_bluetooth_target(
                    bluetooth_target,
                    allow_fallback=allow_fallback_target,
                )
                artifacts.append(f"bluetooth_target={target_detail}")
                if not target_selected:
                    self.log_action(
                        "export_chat",
                        selector=chat_name,
                        result="fail",
                        artifacts=artifacts + ["bluetooth_target_not_selected"],
                    )
                    return {"status": status, "artifacts": artifacts}
                self._wait_bluetooth_share_handoff(timeout_sec=2.5)

                logger.info("%s Bluetooth target selected (detail=%s)", self.aura_prefix(self.current_phase or "phase"), target_detail)
                self.log_action("export_chat_stage", selector=chat_name, artifacts=["bluetooth_target_selected"])

            if require_host_confirmation and target_selected:
                host_confirm = wait_for_windows_bluetooth_receive_complete(
                    timeout_sec=host_confirm_timeout,
                    poll_sec=host_confirm_poll,
                    save_dir=str(chat_export_dir),
                )
                artifacts.append(f"host_confirm={host_confirm.get('reason')}")
                artifacts.extend(host_confirm.get("screenshots", []) or [])
                self.log_action(
                    "bluetooth_host_confirm",
                    selector=chat_name,
                    result="success" if host_confirm.get("ok") else "fail",
                    artifacts=[host_confirm.get("reason", ""), host_confirm.get("detail", "")],
                )
                if not host_confirm.get("ok"):
                    self.log_action(
                        "export_chat",
                        selector=chat_name,
                        result="fail",
                        artifacts=artifacts + ["bluetooth_host_confirmation_failed"],
                    )
                    return {"status": status, "artifacts": artifacts}
            elif require_host_confirmation and not target_selected:
                artifacts.append("host_confirm=skipped_no_target")
            else:
                artifacts.append("host_confirm=disabled")
                if wait_transfer_complete:
                    host_finish = wait_for_windows_bluetooth_receive_complete(
                        timeout_sec=host_confirm_timeout,
                        poll_sec=host_confirm_poll,
                        save_dir=str(chat_export_dir),
                    )
                    artifacts.append(f"host_finish={host_finish.get('reason')}")
                    artifacts.extend(host_finish.get("screenshots", []) or [])
                    expected_file_names = host_finish.get("file_names", []) or []
                    expected_file_contains = [re.sub(r"[\u2026.]+$", "", n).strip() for n in expected_file_names if (n or "").strip()]
                    self.log_action(
                        "bluetooth_host_finish",
                        selector=chat_name,
                        result="success" if host_finish.get("ok") else "fail",
                        artifacts=[host_finish.get("reason", ""), host_finish.get("detail", "")],
                    )
                    if (not host_finish.get("ok")) and host_finish.get("reason") == "receiver_window_closed":
                        self.log_action(
                            "bluetooth_receiver_closed",
                            selector=chat_name,
                            result="fail",
                            artifacts=[host_finish.get("detail", "")],
                        )

            receive_ret = collect_windows_bluetooth_received_files(
                dest_dir=chat_export_dir,
                baseline=receive_baseline,
                since_ts=receive_start_ts,
                timeout_sec=host_receive_timeout,
                poll_sec=host_receive_poll,
                max_depth=host_receive_scan_depth,
                strict_exchange_only=host_receive_strict_exchange,
                allowed_extensions=host_receive_allowed_ext,
                min_size_bytes=host_receive_min_size,
            )
            received_files = receive_ret.get("files", []) or []
            artifacts.append(f"received_files={len(received_files)}")
            artifacts.append(f"received_move_reason={receive_ret.get('reason', '')}")
            if received_files:
                artifacts.extend(received_files)
            logger.info("%s Bluetooth receive collect done (chat=%s, ok=%s, files=%d, reason=%s)", self.aura_prefix(self.current_phase or "phase"), chat_name, bool(receive_ret.get("ok")), len(received_files), receive_ret.get("reason", ""))
            self.log_action("export_chat_stage", selector=chat_name, artifacts=["receive_collect"])
            self.log_action(
                "bluetooth_receive_collect",
                selector=chat_name,
                result="success" if receive_ret.get("ok") else "fail",
                artifacts=[receive_ret.get("reason", ""), str(len(received_files))],
            )

            if (not received_files) and receiver_retry_enabled:
                retry_baseline = list_windows_bluetooth_receive_files(
                    max_depth=host_receive_scan_depth,
                    strict_exchange_only=host_receive_strict_exchange,
                )
                retry_started_ts = time.time()
                receiver_retry = prepare_windows_bluetooth_receiver(
                    timeout_sec=receiver_retry_timeout,
                    prefer_fresh_start=host_manual_mode,
                )
                self.log_action(
                    "bluetooth_receiver_retry_start",
                    selector=chat_name,
                    result="success" if receiver_retry.get("ok") else "fail",
                    artifacts=[receiver_retry.get("reason", ""), receiver_retry.get("detail", "")],
                )

                if receiver_retry.get("ok"):
                    retried_target, retried_detail = self._select_bluetooth_target(
                        bluetooth_target,
                        allow_fallback=allow_fallback_target,
                    )
                    artifacts.append(f"bluetooth_retry_target={retried_detail}")
                    if retried_target:
                        self._wait_bluetooth_share_handoff(timeout_sec=2.5)
                        if wait_transfer_complete:
                            retry_finish = wait_for_windows_bluetooth_receive_complete(
                                timeout_sec=host_confirm_timeout,
                                poll_sec=host_confirm_poll,
                                save_dir=str(chat_export_dir),
                            )
                            artifacts.extend(retry_finish.get("screenshots", []) or [])
                            expected_file_names = retry_finish.get("file_names", []) or expected_file_names
                            expected_file_contains = [re.sub(r"[\u2026.]+$", "", n).strip() for n in expected_file_names if (n or "").strip()]
                            self.log_action(
                                "bluetooth_host_finish_retry",
                                selector=chat_name,
                                result="success" if retry_finish.get("ok") else "fail",
                                artifacts=[retry_finish.get("reason", ""), retry_finish.get("detail", "")],
                            )
                            if (not retry_finish.get("ok")) and retry_finish.get("reason") == "receiver_window_closed":
                                self.log_action(
                                    "bluetooth_receiver_closed",
                                    selector=chat_name,
                                    result="fail",
                                    artifacts=["during_retry", retry_finish.get("detail", "")],
                                )

                        retry_ret = collect_windows_bluetooth_received_files(
                            dest_dir=chat_export_dir,
                            baseline=retry_baseline,
                            since_ts=retry_started_ts,
                            timeout_sec=host_receive_timeout,
                            poll_sec=host_receive_poll,
                            max_depth=host_receive_scan_depth,
                            strict_exchange_only=host_receive_strict_exchange,
                            allowed_extensions=host_receive_allowed_ext,
                            min_size_bytes=host_receive_min_size,
                        )
                        retry_files = retry_ret.get("files", []) or []
                        self.log_action(
                            "bluetooth_receive_collect_retry",
                            selector=chat_name,
                            result="success" if retry_ret.get("ok") else "fail",
                            artifacts=[retry_ret.get("reason", ""), str(len(retry_files))],
                        )
                        if retry_files:
                            received_files = retry_files
                            receive_ret = retry_ret
                            artifacts.append(f"received_retry_files={len(retry_files)}")
                            artifacts.extend(retry_files)

            transfer_by_dest = {}
            for transfer in receive_ret.get("transfers", []) or []:
                try:
                    dest_key = str(Path(transfer.get("dest_path") or "").resolve()).lower()
                    transfer_by_dest[dest_key] = transfer
                except Exception:
                    continue


            for received_path in received_files:
                try:
                    p = Path(received_path)
                    if not (p.exists() and p.is_file()):
                        continue

                    operation_id = self._whatsapp_export_operation_id(chat_id, p)
                    archive_size, archive_sha256 = self._file_size_sha256(p)
                    transfer = transfer_by_dest.get(str(p.resolve()).lower())
                    copy_artifacts = [
                        "stage=post_acquisition",
                        "component=whatsapp_s2",
                        f"operation_id={operation_id}",
                        f"phase={self.current_phase or ''}",
                        f"chat_id={chat_id}",
                        f"dest_path={str(p.resolve())}",
                        f"dest_size={archive_size if archive_size is not None else ''}",
                        f"dest_sha256={archive_sha256 or ''}",
                    ]
                    copy_result = "success"
                    if transfer:
                        copy_artifacts.extend(
                            [
                                f"source_path={transfer.get('source_path') or ''}",
                                f"source_size={transfer.get('source_size', '')}",
                                f"source_sha256={transfer.get('source_sha256') or ''}",
                                f"transfer_mode={transfer.get('transfer_mode') or ''}",
                                f"verified={bool(transfer.get('verified'))}",
                            ]
                        )
                        copy_result = "success" if transfer.get("verified") else "warn"
                    else:
                        copy_artifacts.append("source_path=unknown")
                        copy_artifacts.append("warning=transfer_metadata_missing")
                        copy_result = "warn"
                    self.log_action(
                        "copy_received_export",
                        selector=chat_name,
                        result=copy_result,
                        artifacts=copy_artifacts,
                    )

                    kind = "whatsapp_export_zip" if p.suffix.lower() == ".zip" else "whatsapp_export_file"
                    archive_artifact = self.register_artifact(
                        p,
                        kind=kind,
                        chat_id=chat_id,
                        source_action="bluetooth_receive_collect",
                        source_screen="host_bluetooth_receiver",
                    )
                    if archive_sha256:
                        self.storage.update_file_artifact_hash(self.run_id, str(p.resolve()), archive_sha256, archive_size)

                    if p.suffix.lower() != ".zip":
                        continue

                    self.log_action(
                        "register_export_archive_artifact",
                        selector=chat_name,
                        artifacts=[
                            "stage=post_acquisition",
                            "component=whatsapp_s2",
                            f"operation_id={operation_id}",
                            f"artifact_id={(archive_artifact or {}).get('artifact_id') or ''}",
                            f"archive_path={str(p.resolve())}",
                            f"archive_size={archive_size if archive_size is not None else ''}",
                            f"archive_sha256={archive_sha256 or ''}",
                        ],
                    )

                    parse_start_seq = self.log_action(
                        "whatsapp_export_parse_start",
                        selector=chat_name,
                        artifacts=[str(p), f"operation_id={operation_id}"],
                    )
                    logger.info("%s Export zip parse start (chat=%s, path=%s)", self.aura_prefix(self.current_phase or "phase"), chat_name, str(p))
                    parsed = parse_whatsapp_export_zip(chat_id, p)
                    msgs = parsed.messages or []
                    attachment_entries = parsed.attachments or []
                    missing_attachment_entries = parsed.missing_attachments or []
                    attachment_mentions_total = len(attachment_entries) + len(missing_attachment_entries)
                    text_artifact = None
                    text_extract_path = None
                    text_size = None
                    text_sha256 = None
                    extracted_entry_count = 0
                    total_extracted_bytes = 0
                    extract_warning_count = 0
                    extract_root = chat_export_dir / f"{p.stem}_extracted"

                    if parsed.source_txt_name:
                        try:
                            extract_root.mkdir(parents=True, exist_ok=True)
                            text_member = parsed.source_txt_name
                            text_name = Path(text_member.replace("\\", "/")).name or "chat.txt"
                            text_extract_path = extract_root / text_name
                            with ZipFile(str(p), "r") as zf:
                                text_bytes = zf.read(text_member)
                            text_extract_path.write_bytes(text_bytes)
                            extracted_entry_count += 1
                            total_extracted_bytes += len(text_bytes)
                            text_artifact = self.register_artifact(
                                text_extract_path,
                                kind="whatsapp_export_text",
                                chat_id=chat_id,
                                source_action="extract_export_archive",
                                source_screen="whatsapp_export_zip",
                                archive_path=str(p.resolve()),
                                archive_member=text_member,
                            )
                            text_size, text_sha256 = self._file_size_sha256(text_extract_path)
                            if text_sha256:
                                self.storage.update_file_artifact_hash(self.run_id, str(text_extract_path.resolve()), text_sha256, text_size)
                        except Exception as e:
                            extract_warning_count += 1
                            self.log_action(
                                "extract_export_archive",
                                selector=chat_name,
                                result="warn",
                                error=e,
                                artifacts=[
                                    "stage=post_acquisition",
                                    "component=whatsapp_s2",
                                    f"operation_id={operation_id}",
                                    f"archive_path={str(p.resolve())}",
                                    f"text_member={parsed.source_txt_name}",
                                    "warning=text_extract_failed",
                                ],
                            )
                    logger.info("%s Export zip parse done (chat=%s, messages=%d)", self.aura_prefix(self.current_phase or "phase"), chat_name, len(msgs))
                    self.log_action(
                        "whatsapp_export_parse_end",
                        selector=chat_name,
                        artifacts=[
                            str(p),
                            f"operation_id={operation_id}",
                            f"messages={len(msgs)}",
                            f"attachments={len(attachment_entries)}",
                            f"missing_attachments={len(missing_attachment_entries)}",
                            f"txt={parsed.source_txt_name or ''}",
                        ],
                    )
                    self.log_action(
                        "parse_chat_text",
                        selector=chat_name,
                        artifacts=[
                            "stage=post_acquisition",
                            "component=whatsapp_s2",
                            f"operation_id={operation_id}",
                            f"phase={self.current_phase or ''}",
                            f"chat_id={chat_id}",
                            f"input_path={str(text_extract_path.resolve()) if text_extract_path else 'zip:' + (parsed.source_txt_name or '')}",
                            f"input_sha256={text_sha256 or ''}",
                            "parser=whatsapp_export_parser",
                            "parser_version=2026-05-29",
                            "encoding=utf-8",
                            f"messages_parsed={len(msgs)}",
                            f"attachment_mentions_detected={attachment_mentions_total}",
                            "parse_warnings=0",
                        ],
                    )

                    attachment_success_count = 0
                    if attachment_entries:
                        attachment_dir = chat_export_dir / f"{p.stem}_attachments"
                        attachment_dir.mkdir(parents=True, exist_ok=True)
                        with ZipFile(str(p), "r") as zf:
                            for idx, entry in enumerate(attachment_entries):
                                member = (entry.get("zip_member") or "").strip()
                                if not member:
                                    continue
                                relative_member = Path(member.replace("\\", "/"))
                                out_path = attachment_dir / relative_member
                                out_path.parent.mkdir(parents=True, exist_ok=True)
                                if not out_path.exists():
                                    with zf.open(member, "r") as src, open(out_path, "wb") as dst:
                                        shutil.copyfileobj(src, dst)
                                out_size, out_sha256 = self._file_size_sha256(out_path)
                                if out_size is not None:
                                    extracted_entry_count += 1
                                    total_extracted_bytes += out_size
                                artifact_meta = self.register_artifact(
                                    out_path,
                                    kind="attachment_export_media",
                                    chat_id=chat_id,
                                    message_id=entry.get("message_id"),
                                    record_id=entry.get("record_id"),
                                    observation_id=entry.get("observation_id"),
                                    message_type=entry.get("message_type"),
                                    file_name=out_path.name,
                                    display_filename=entry.get("file_name") or out_path.name,
                                    collected_path=str(out_path.resolve()),
                                    source_action="parse_whatsapp_export_zip",
                                    source_screen="whatsapp_export_zip",
                                    created_ts=time.time() + (idx / 1000.0),
                                )
                                if out_sha256:
                                    self.storage.update_file_artifact_hash(self.run_id, str(out_path.resolve()), out_sha256, out_size)
                                    if artifact_meta is not None:
                                        artifact_meta["sha256"] = out_sha256
                                artifact_register_seq = getattr(self, "audit_seq", None)
                                attempt_ended_ts = time.time()
                                self.storage.upsert_attachment_attempt(
                                    {
                                        "run_id": self.run_id,
                                        "app": self.app_id,
                                        "phase": self.current_phase or "",
                                        "account": "default",
                                        "chat_id": chat_id,
                                        "record_id": entry.get("record_id"),
                                        "message_id": entry.get("message_id"),
                                        "observation_id": entry.get("observation_id"),
                                        "method": "S2",
                                        "primitive": "whatsapp_export_zip_parse",
                                        "route_id": "whatsapp_export_chat",
                                        "target_type": "attachment",
                                        "target_label": entry.get("file_name") or out_path.name,
                                        "message_type": entry.get("message_type"),
                                        "display_filename": entry.get("file_name") or out_path.name,
                                        "status": "success",
                                        "failure_reason": None,
                                        "ui_reached": 1,
                                        "artifact_materialized": 1,
                                        "started_audit_seq": parse_start_seq,
                                        "ended_audit_seq": artifact_register_seq,
                                        "started_ts": export_start_ts,
                                        "ended_ts": attempt_ended_ts,
                                        "duration_sec": round(attempt_ended_ts - export_start_ts, 3),
                                        "download_detection_method": "whatsapp_export_zip_member",
                                        "audit_operation_id": operation_id,
                                        "artifact_ids": [artifact_meta.get("artifact_id")] if artifact_meta else [],
                                        "artifact_paths": [artifact_meta.get("artifact_path")] if artifact_meta else [str(out_path.resolve())],
                                        "sha256_list": [artifact_meta.get("sha256")] if artifact_meta and artifact_meta.get("sha256") else [],
                                        "device_paths": [member],
                                        "attempt_id": self._whatsapp_export_attempt_id(chat_id, entry, "success"),
                                    }
                                )
                                attachment_success_count += 1
                    self.log_action(
                        "extract_export_archive",
                        selector=chat_name,
                        result="warn" if extract_warning_count else "success",
                        artifacts=[
                            "stage=post_acquisition",
                            "component=whatsapp_s2",
                            f"operation_id={operation_id}",
                            f"phase={self.current_phase or ''}",
                            f"chat_id={chat_id}",
                            f"archive_path={str(p.resolve())}",
                            f"archive_sha256={archive_sha256 or ''}",
                            f"extract_dir={str(chat_export_dir.resolve())}",
                            f"entry_count={extracted_entry_count}",
                            f"total_extracted_bytes={total_extracted_bytes}",
                            f"warnings={extract_warning_count}",
                        ],
                    )
                    attachment_missing_count = 0
                    if missing_attachment_entries:
                        for entry in missing_attachment_entries:
                            warning_seq = self.log_action(
                                "attachment_validation_warning",
                                selector=chat_name,
                                result="warn",
                                artifacts=[
                                    "stage=post_acquisition",
                                    "component=whatsapp_s2",
                                    f"operation_id={operation_id}",
                                    f"phase={self.current_phase or ''}",
                                    f"chat_id={chat_id}",
                                    f"message_id={entry.get('message_id') or ''}",
                                    f"attachment_mention={entry.get('file_name') or ''}",
                                    "match_status=missing",
                                    f"failure_reason={entry.get('failure_reason') or 'zip_member_missing'}",
                                ],
                            )
                            attempt_ended_ts = time.time()
                            self.storage.upsert_attachment_attempt(
                                {
                                    "run_id": self.run_id,
                                    "app": self.app_id,
                                    "phase": self.current_phase or "",
                                    "account": "default",
                                    "chat_id": chat_id,
                                    "record_id": entry.get("record_id"),
                                    "message_id": entry.get("message_id"),
                                    "observation_id": entry.get("observation_id"),
                                    "method": "S2",
                                    "primitive": "whatsapp_export_zip_parse",
                                    "route_id": "whatsapp_export_chat",
                                    "target_type": "attachment",
                                    "target_label": entry.get("file_name"),
                                    "message_type": entry.get("message_type"),
                                    "display_filename": entry.get("file_name"),
                                    "status": "missing",
                                    "failure_reason": entry.get("failure_reason") or "zip_member_missing",
                                    "ui_reached": 1,
                                    "artifact_materialized": 0,
                                    "started_audit_seq": parse_start_seq,
                                    "ended_audit_seq": warning_seq,
                                    "started_ts": export_start_ts,
                                    "ended_ts": attempt_ended_ts,
                                    "duration_sec": round(attempt_ended_ts - export_start_ts, 3),
                                    "download_detection_method": "whatsapp_export_zip_member",
                                    "audit_operation_id": operation_id,
                                    "artifact_ids": [],
                                    "artifact_paths": [],
                                    "sha256_list": [],
                                    "device_paths": [],
                                    "attempt_id": self._whatsapp_export_attempt_id(chat_id, entry, "missing"),
                                }
                            )
                            attachment_missing_count += 1
                        self.log_action(
                            "whatsapp_export_attachment_missing",
                            selector=chat_name,
                            result="missing",
                            artifacts=[
                                f"count={len(missing_attachment_entries)}",
                                "files=" + " | ".join(str(x.get("file_name") or "") for x in missing_attachment_entries[:10]),
                            ],
                        )
                    if attachment_mentions_total:
                        self.log_action(
                            "validate_attachment_mentions",
                            selector=chat_name,
                            result="warn" if attachment_missing_count else "success",
                            artifacts=[
                                "stage=post_acquisition",
                                "component=whatsapp_s2",
                                f"operation_id={operation_id}",
                                f"phase={self.current_phase or ''}",
                                f"chat_id={chat_id}",
                                f"text_artifact_id={(text_artifact or {}).get('artifact_id') or ''}",
                                f"export_root={str(chat_export_dir.resolve())}",
                                f"mentions_total={attachment_mentions_total}",
                                f"found={attachment_success_count}",
                                f"missing={attachment_missing_count}",
                                "ambiguous=0",
                                f"db_rows_updated={attachment_success_count + attachment_missing_count}",
                            ],
                        )
                    if not msgs:
                        self.log_action(
                            "commit_whatsapp_export_records",
                            selector=chat_name,
                            result="warn",
                            artifacts=[
                                "stage=post_acquisition",
                                "component=whatsapp_s2",
                                f"operation_id={operation_id}",
                                f"phase={self.current_phase or ''}",
                                f"chat_id={chat_id}",
                                "db_tables=messages,artifacts,attachment_attempts",
                                "messages_inserted=0",
                                f"file_artifacts_inserted={extracted_entry_count + 1}",
                                f"attachment_links_inserted={attachment_success_count + attachment_missing_count}",
                                "warning=no_messages_parsed",
                            ],
                        )
                        self.log_action(
                            "whatsapp_export_analysis_summary",
                            selector=chat_name,
                            result="warn",
                            artifacts=[
                                "stage=post_acquisition",
                                "component=whatsapp_s2",
                                f"operation_id={operation_id}",
                                "messages=0",
                                f"attachments_found={attachment_success_count}",
                                f"attachments_missing={attachment_missing_count}",
                                "warning=no_messages_parsed",
                            ],
                        )
                        continue


                    logger.info("%s Export zip ingest start (chat=%s, messages=%d)", self.aura_prefix(self.current_phase or "phase"), chat_name, len(msgs))
                    self.storage.upsert_messages(
                        self.app_id,
                        "default",
                        chat_id,
                        msgs,
                        phase=self.current_phase or "",
                        chat_name=chat_name,
                        chat_type="Unknown",
                    )
                    parsed_count += len(msgs)
                    self.log_action(
                        "commit_whatsapp_export_records",
                        selector=chat_name,
                        artifacts=[
                            "stage=post_acquisition",
                            "component=whatsapp_s2",
                            f"operation_id={operation_id}",
                            f"phase={self.current_phase or ''}",
                            f"chat_id={chat_id}",
                            "db_tables=messages,artifacts,attachment_attempts",
                            f"messages_inserted={len(msgs)}",
                            f"file_artifacts_inserted={extracted_entry_count + 1}",
                            f"attachment_links_inserted={attachment_success_count + attachment_missing_count}",
                        ],
                    )
                    self.log_action(
                        "whatsapp_export_analysis_summary",
                        selector=chat_name,
                        result="warn" if attachment_missing_count else "success",
                        artifacts=[
                            "stage=post_acquisition",
                            "component=whatsapp_s2",
                            f"operation_id={operation_id}",
                            f"messages={len(msgs)}",
                            f"attachments_found={attachment_success_count}",
                            f"attachments_missing={attachment_missing_count}",
                            f"archive_sha256={archive_sha256 or ''}",
                        ],
                    )
                    logger.info("%s Export zip ingest done (chat=%s, ingested_messages=%d)", self.aura_prefix(self.current_phase or "phase"), chat_name, len(msgs))
                except Exception as e:
                    logger.warning("%s Export parse/ingest failed: %s", self.aura_prefix(self.current_phase or 'phase'), e)
                    self.log_action(
                        "whatsapp_export_parse",
                        selector=chat_name,
                        result="fail",
                        error=e,
                        artifacts=[str(received_path)],
                    )

            if parsed_count > 0:
                artifacts.append(f"ingested_messages={parsed_count}")
                self.log_action(
                    "whatsapp_export_ingest",
                    selector=chat_name,
                    artifacts=[f"messages={parsed_count}"],
                )
            if require_received_file and (not received_files):
                self.log_action(
                    "export_chat",
                    selector=chat_name,
                    result="fail",
                    artifacts=artifacts + ["bluetooth_received_file_missing"],
                )
                return {"status": status, "artifacts": artifacts}
            status = "done"
            self.log_action(
                "export_chat",
                selector=chat_name,
                artifacts=artifacts + [f"bluetooth_clicked={bluetooth_clicked}"],
            )
            return {"status": status, "artifacts": artifacts}
        finally:
            self.current_chat_id = None
            duration = time.time() - export_start_ts
            ok = status == "done"
            logger.info(
                "%s Export chat end: chat=%s chat_id=%s (status=%s, received_files=%d, ingested_messages=%d, duration=%.2fs)",
                self.aura_prefix(self.current_phase or "phase"),
                chat_name,
                chat_id,
                status,
                len(received_files or []),
                int(parsed_count or 0),
                duration,
            )
            self.log_action(
                "export_chat_end",
                selector=chat_name,
                result="success" if ok else "fail",
                artifacts=[
                    chat_id,
                    f"status={status}",
                    f"received_files={len(received_files or [])}",
                    f"ingested_messages={int(parsed_count or 0)}",
                    f"duration={duration:.2f}s",
                ],
            )
            self._back_to_chat_list(reason=f"export_chat_end:{chat_name}")

    def _collect_exports(self, chatrooms: list[dict], export_dir) -> int:
        if not chatrooms:
            return 0

        max_swipes = int(self.profile.get("chat_list_max_swipes", 20))
        export_passes = max(1, int(self.profile.get("chat_export_max_passes", 2)))

        by_id = {room["chat_id"]: room for room in chatrooms}
        pending = set(by_id.keys())
        export_done = 0

        for pass_idx in range(export_passes):
            if not pending:
                break

            if not self._tap_chats_tab():
                self.log_action("chat_export_pass_start", selector=self.current_phase, result="chat_list_state_unconfirmed")
            self._scroll_to_top()
            previous_signature = ()
            stagnant_pages = 0

            self.log_action(
                "chat_export_pass_start",
                selector=self.current_phase,
                artifacts=[f"pass={pass_idx+1}", f"pending={len(pending)}"],
            )

            for _ in range(max_swipes):
                names = self._extract_chat_names()
                signature = tuple(names)
                if signature == previous_signature:
                    stagnant_pages += 1
                else:
                    stagnant_pages = 0
                previous_signature = signature

                for name in names:
                    chat_id = self._build_chat_id(name)
                    if chat_id not in pending:
                        continue
                    if not self._open_chat_by_name(name):
                        continue

                    ret = self._export_current_chat(name, chat_id, export_dir)
                    by_id[chat_id]["artifacts"] = ret.get("artifacts", [])
                    if ret.get("status") == "done":
                        export_done += 1
                        pending.remove(chat_id)

                if not pending:
                    break
                if stagnant_pages >= 1:
                    break

                before_scroll_signature = self._current_chat_list_signature()
                scroll_down(self.device)
                self.log_action("swipe", selector="scroll_down")
                self.wait_for_list_changed(
                    "whatsapp_chat_export_scroll_down",
                    before_scroll_signature,
                    self._current_chat_list_signature,
                    timeout=1.5,
                    interval=0.2,
                )

            self.log_action(
                "chat_export_pass_end",
                selector=self.current_phase,
                artifacts=[f"pass={pass_idx+1}", f"pending={len(pending)}", f"done={export_done}"],
            )

        if pending:
            missing = [by_id[cid]["name"] for cid in pending if cid in by_id]
            self.log_action(
                "chat_export_incomplete",
                selector=self.current_phase,
                result="fail",
                artifacts=[f"missing_count={len(missing)}"],
            )
            for name in missing[:50]:
                self.log_action("chat_export_missing", selector=name, result="fail")
        return export_done

    def collect_chatrooms(self) -> dict:
        phase_label = self.current_phase or "phase"
        list_dir = self.artifact_dir / "Chat History" / "Chat List"
        export_dir = self.artifact_dir / "Chat History" / "Export"
        list_dir.mkdir(parents=True, exist_ok=True)
        export_dir.mkdir(parents=True, exist_ok=True)

        self.log_action("chatrooms_collect_start", selector=phase_label)
        logger.info("%s Chatroom collection start", self.aura_prefix(phase_label))

        chatrooms, list_shots = self._discover_chatrooms(list_dir)
        self.storage.upsert_chatrooms(self.app_id, "default", chatrooms, phase=self.current_phase or "")
        export_done = self._collect_exports(chatrooms, export_dir)
        self.storage.upsert_chatrooms(self.app_id, "default", chatrooms, phase=self.current_phase or "")

        self.log_action(
            "chatrooms_collect_end",
            selector=phase_label,
            artifacts=[f"chatrooms={len(chatrooms)}", f"exports={export_done}"],
        )
        logger.info(
            "%s Chatroom collection end (chatrooms=%d, exports=%d)",
            self.aura_prefix(phase_label),
            len(chatrooms),
            export_done,
        )
        return {
            "chatrooms": chatrooms,
            "chat_list_screenshots": list_shots,
            "export_count": export_done,
        }
