import time
import logging
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from utils.utils import scroll_down, scroll_up
from collectors.telegram.ui_selectors import (
    BANNER_CLOSE_XPATH,
    CHATRROM_END_ANCHOR,
    CHATROOM_COMPOSER_BOX,
    CHATROOM_COMPOSER_DESC,
    CHATROOM_ELEM,
    CHATROOM_GROUP_MEMBER_INFO,
    CHATROOM_HEADER_ACTION_DESCS,
    CHATROOM_HEADER_BACK_DESC,
    CHATROOM_PROFILE_SKIP_STRING,
    CHATROOM_PROFILE_TYPE,
    CHATROOM_TYPE_PAGE,
    CHATS,
    CONTACTS,
    GO_TO_BOTTOM_DESC,
    PROFILE,
    PROFILE_MOBILE_PREFIX,
    PROFILE_USERNAME_PREFIX,
    SETTINGS,
    CHAT_LIST_SEARCH_TEXT,
    CHAT_LIST_TOP_TEXTS,
)
from collectors.telegram.mixin_base import TelegramCollectorDeps

logger = logging.getLogger(__name__)

class TelegramChatroomsMixin(TelegramCollectorDeps):
    def _is_invalid_chatroom_target(self, chat_type, chat_name):
        normalized_type = (chat_type or "").strip().lower()
        normalized_name = (chat_name or "").strip().lower()




        if normalized_name == "new message":
            return True

        return False

    def _find_chatroom_top_bar_bounds_from_root(self, root) -> list[int] | None:
        candidates = []

        for node in root.iter():
            if self._xml_class(node) != "android.widget.FrameLayout":
                continue

            b = self._xml_bounds(node)
            if not b:
                continue

            child_descs = {self._xml_desc(x) for x in list(node) if self._xml_desc(x)}

            has_direct_back = CHATROOM_HEADER_BACK_DESC in child_descs
            has_direct_title_container = any(
                self._xml_class(x) == "android.widget.FrameLayout" and any(
                    self._xml_class(g) == "android.widget.TextView" and self._xml_text(g)
                    for g in x.iter()
                )
                for x in list(node)
            )
            has_direct_action_group = any(
                self._xml_class(x) == "android.widget.LinearLayout" and any(
                    self._xml_desc(g) in CHATROOM_HEADER_ACTION_DESCS
                    for g in x.iter()
                )
                for x in list(node)
            )

            if has_direct_back and has_direct_title_container and has_direct_action_group:
                candidates.append(b)

        if candidates:
            return min(candidates, key=lambda x: (x[3], x[1], x[2] - x[0]))
        return None

    def _find_chat_list_top_bar_bounds_from_root(self, root) -> list[int] | None:
        candidates = []
        header_signal_bottom = None
        screen_width = 0
        try:
            screen_width = int(root.attrib.get("width") or 0)
        except Exception:
            screen_width = 0

        for node in root.iter():
            b = self._xml_bounds(node)
            if not b:
                continue
            left, top, right, bottom = b
            width = right - left
            height = bottom - top
            if top > 320 or bottom > 420:
                continue

            node_class = self._xml_class(node)
            node_text = self._xml_text(node)
            node_desc = self._xml_desc(node)
            is_header_signal = (
                (node_class == "android.widget.TextView" and node_text in CHAT_LIST_TOP_TEXTS)
                or (node_class == "android.widget.ImageButton" and node_desc in ("Search", "More options"))
            )
            if is_header_signal:
                header_signal_bottom = max(header_signal_bottom or 0, bottom)

            if node_class not in ("android.widget.FrameLayout", "android.widget.LinearLayout", "android.view.View"):
                continue
            if screen_width and width < int(screen_width * 0.9):
                continue
            if height <= 0 or height > 180:
                continue
            if top > 260:
                continue

            has_header_text = any(
                self._xml_class(x) == "android.widget.TextView" and self._xml_text(x) in CHAT_LIST_TOP_TEXTS
                for x in node.iter()
            )
            has_right_image_button = any(
                self._xml_class(x) == "android.widget.ImageButton"
                and self._xml_desc(x) in ("Search", "More options")
                for x in node.iter()
            )

            if has_header_text or has_right_image_button:
                candidates.append(b)
                continue

            if header_signal_bottom is not None and top <= header_signal_bottom + 120:
                candidates.append(b)

        if candidates:
            top = min(item[1] for item in candidates)
            bottom = max(item[3] for item in candidates)
            right = max(item[2] for item in candidates)
            return [0, top, right, bottom]
        return None

    def _get_chatroom_actionable_bounds(self) -> tuple[int | None, int | None]:
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
        except Exception:
            composer_bounds = None
            safe_bottom = None
            try:
                composer_bounds = getattr(self, "input_bound", None) or self.device.xpath(CHATROOM_COMPOSER_BOX).info.get("bounds")
            except Exception:
                composer_bounds = None
            if isinstance(composer_bounds, dict):
                safe_bottom = composer_bounds.get("top")
            return 0, safe_bottom

        top_bar_bounds = self._find_chatroom_top_bar_bounds_from_root(root)
        web_tabs_candidates = []

        for node in root.iter():
            b = self._xml_bounds(node)
            if not b:
                continue

            node_class = self._xml_class(node)
            content_desc = self._xml_desc(node)
            if node_class == "android.widget.FrameLayout" and content_desc == CHATROOM_COMPOSER_DESC:
                web_tabs_candidates.append((b[1], b))

        safe_top = top_bar_bounds[3] if top_bar_bounds else None
        safe_bottom = min(web_tabs_candidates, key=lambda item: item[0])[1][1] if web_tabs_candidates else None

        if safe_bottom is None:
            try:
                composer_bounds = getattr(self, "input_bound", None) or self.device.xpath(CHATROOM_COMPOSER_BOX).info.get("bounds")
                if isinstance(composer_bounds, dict):
                    safe_bottom = composer_bounds.get("top")
            except Exception:
                pass

        resolved_safe_top = safe_top if safe_top is not None else 0
        return resolved_safe_top, safe_bottom

    def _is_telegram_chatroom_screen(self) -> bool:
        try:
            has_back = self.device(description=CHATROOM_HEADER_BACK_DESC).exists
            has_recycler = self.device.xpath('//androidx.recyclerview.widget.RecyclerView').exists
            has_action = any(self.device(description=desc).exists for desc in CHATROOM_HEADER_ACTION_DESCS)
            has_composer = self.device.xpath(CHATROOM_COMPOSER_BOX).exists
            if has_back and has_recycler and (has_action or has_composer):
                return True
        except Exception:
            pass

        try:
            root = ET.fromstring(self.device.dump_hierarchy())
        except Exception:
            return False

        has_recycler = False
        has_header_back = False
        has_header_action = False
        has_composer = False

        for node in root.iter():
            node_class = self._xml_class(node)
            node_desc = self._xml_desc(node)
            node_bounds = self._xml_bounds(node)

            if node_class == "androidx.recyclerview.widget.RecyclerView":
                has_recycler = True

            if node_desc == CHATROOM_COMPOSER_DESC:
                has_composer = True

            if not node_bounds or node_bounds[1] > 360:
                continue

            if node_desc == CHATROOM_HEADER_BACK_DESC:
                has_header_back = True
            if node_desc in CHATROOM_HEADER_ACTION_DESCS:
                has_header_action = True

        return has_recycler and has_header_back and (has_header_action or has_composer)

    def _is_telegram_chatroom_profile_screen(self) -> bool:
        try:
            if self.device(textStartsWith=PROFILE_USERNAME_PREFIX).exists:
                return True
            if self.device(textStartsWith=PROFILE_MOBILE_PREFIX).exists:
                return True
            if any(self.device(text=text).exists for text in ("Message", "Mute", "Share")):
                return True
        except Exception:
            pass

        try:
            root = ET.fromstring(self.device.dump_hierarchy())
        except Exception:
            return False

        has_recycler = any(self._xml_class(n) == "androidx.recyclerview.widget.RecyclerView" for n in root.iter())
        has_profile_text = False
        for node in root.iter():
            text = self._xml_text(node)
            if not text:
                continue
            if text.startswith(PROFILE_USERNAME_PREFIX) or text.startswith(PROFILE_MOBILE_PREFIX):
                has_profile_text = True
                break
            if text in CHATROOM_PROFILE_SKIP_STRING or self._is_any_chatroom_type_text(text):
                has_profile_text = True
                break

        return has_recycler and has_profile_text and not self._is_telegram_chatroom_screen()

    def _press_back_to_chat_list(self, *, reason: str, max_back: int = 2, timeout: float = 2.0) -> bool:
        return self.press_back_to_state(
            reason=reason,
            state_name="telegram_chat_list_screen",
            predicate=self._is_telegram_chat_list_screen,
            max_back=max_back,
            timeout=timeout,
            on_success=lambda: self._wait_for_chatroom_list_settled(reason=reason),
        )

    def _chatroom_row_bounds(self, chatroom) -> list[int] | None:
        try:
            bounds = (chatroom.info or {}).get("bounds") or {}
        except Exception:
            bounds = {}

        left = bounds.get("left")
        top = bounds.get("top")
        right = bounds.get("right")
        bottom = bounds.get("bottom")

        if not isinstance(left, int):
            return None
        if not isinstance(top, int):
            return None
        if not isinstance(right, int):
            return None
        if not isinstance(bottom, int):
            return None
        return [left, top, right, bottom]

    def _chatroom_list_bounds(self) -> tuple[int | None, int | None]:
        root = ET.fromstring(self.device.dump_hierarchy())

        search_bounds = None
        top_bar_bounds = self._find_chat_list_top_bar_bounds_from_root(root)

        for n in root.iter():
            if search_bounds is None:
                for x in n.iter():
                    xb = self._xml_bounds(x)
                    if not xb:
                        continue
                    if self._xml_class(x) == "android.widget.EditText" and (
                        self._xml_text(x) == CHAT_LIST_SEARCH_TEXT or self._xml_desc(x) == CHAT_LIST_SEARCH_TEXT
                    ):
                        search_bounds = xb
                        break

        if search_bounds:
            top_bound = search_bounds[3]
        elif top_bar_bounds:
            top_bound = top_bar_bounds[3]
        else:
            top_bound = None

        bottom_bounds = []
        for selector in (CHATS, CONTACTS, SETTINGS, PROFILE):
            try:
                node = self.device.xpath(selector)
                if node.exists:
                    bounds = node.info.get("bounds") or {}
                    top = bounds.get("top")
                    if isinstance(top, int):
                        bottom_bounds.append(top)
            except Exception:
                continue

        bottom_bound = min(bottom_bounds) if bottom_bounds else None
        return (
            top_bound,
            bottom_bound,
        )

    def _is_clickable_chatroom_row(
        self,
        chatroom,
        list_bounds: tuple[int | None, int | None] | None = None,
    ):
        bounds = self._chatroom_row_bounds(chatroom)
        if not bounds:
            return True
        return self._visible_chatroom_row_bounds(bounds, list_bounds=list_bounds) is not None

    def _visible_chatroom_row_bounds(
        self,
        bounds,
        *,
        list_bounds: tuple[int | None, int | None] | None = None,
    ) -> list[int] | None:
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            return None

        if list_bounds is None:
            list_bounds = self.chatroom_list_bounds
        top_exclude_bottom, nav_top = list_bounds or (None, None)

        try:
            left, top, right, bottom = [int(v) for v in bounds]
        except (TypeError, ValueError):
            return None

        visible_top = top
        visible_bottom = bottom
        if isinstance(top_exclude_bottom, int):
            visible_top = max(visible_top, top_exclude_bottom + 1)
        if isinstance(nav_top, int):
            visible_bottom = min(visible_bottom, nav_top - 1)

        visible_height = visible_bottom - visible_top
        row_height = max(1, bottom - top)
        min_visible_height = max(44, min(96, int(row_height * 0.45)))

        if visible_height < min_visible_height:
            return None

        return [left, visible_top, right, visible_bottom]

    def _snapshot_chatroom_rows(self, *, log_skips: bool = True):
        rows = []
        seen_bounds = set()
        self.chatroom_list_bounds = self._chatroom_list_bounds()
        list_bounds = self.chatroom_list_bounds
        top_exclude_bottom, nav_top = list_bounds
        window_width, _ = self.device.window_size()
        min_row_width = int(window_width * 0.6)

        for chatroom in self.device.xpath(CHATROOM_ELEM).all():
            bounds = self._chatroom_row_bounds(chatroom)
            if not bounds:
                continue

            left, top, right, bottom = bounds
            if (right - left) < min_row_width:
                continue

            visible_bounds = self._visible_chatroom_row_bounds(bounds, list_bounds=list_bounds)
            if not visible_bounds:
                if isinstance(top_exclude_bottom, int) and bounds[1] < top_exclude_bottom:
                    reason = "top_partial_overlap" if bounds[3] > top_exclude_bottom else "top_header_overlap"
                else:
                    reason = "bottom_nav_overlap"
                if log_skips:
                    self.log_action("chatroom_skip", selector=reason, artifacts=[f"bounds={bounds}"])
                continue

            bounds_key = tuple(bounds)
            if bounds_key in seen_bounds:
                continue
            seen_bounds.add(bounds_key)

            try:
                text = (chatroom.text or "").strip()
            except Exception:
                text = ""

            rows.append({
                "bounds": bounds,
                "visible_bounds": visible_bounds,
                "text": text,
            })

        rows.sort(key=lambda item: ((item.get("visible_bounds") or item["bounds"])[1], item["bounds"][0]))
        return rows

    def _scroll_chatroom_list_down(self, *, distance_ratio: float = 0.4):
        top_bound, bottom_bound = self.chatroom_list_bounds or (None, None)
        scroll_down(
            self.device,
            distance_ratio=distance_ratio,
            top_bound=top_bound,
            bottom_bound=bottom_bound,
        )

    def _click_chatroom_row(
        self,
        bounds,
        *,
        list_bounds: tuple[int | None, int | None] | None = None,
        prefer_visible_center: bool = False,
        prefer_visible_lower_third: bool = False,
        prefer_upper_third: bool = False,
    ):
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            return False

        try:
            left, top, right, bottom = [int(v) for v in bounds]
        except (TypeError, ValueError):
            return False

        x = (left + right) // 2

        if prefer_visible_center or prefer_visible_lower_third:
            if list_bounds is None:
                list_bounds = self.chatroom_list_bounds
            top_exclude_bottom, nav_top = list_bounds
            visible_top = top
            visible_bottom = bottom

            if isinstance(top_exclude_bottom, int):
                visible_top = max(visible_top, top_exclude_bottom + 1)
            if isinstance(nav_top, int):
                visible_bottom = min(visible_bottom, nav_top - 1)

            if visible_bottom <= visible_top:
                return False

            visible_height = max(visible_bottom - visible_top, 1)
            if prefer_visible_lower_third:
                y = visible_top + int(visible_height * 0.65)
            else:
                y = (visible_top + visible_bottom) // 2
        elif prefer_upper_third:
            height = max(bottom - top, 1)
            y = top + int(height * 0.35)
        else:
            y = (top + bottom) // 2

        self.device.click(x, y)
        return True

    def go_chat_list_top(self):
        top_text = next((text for text in CHAT_LIST_TOP_TEXTS if self.device(text=text).exists), None)
        if top_text:
            self.device(text=top_text).click()
            self.log_action("click", selector=f'text="{top_text}"')
        else:
            w, h = self.device.window_size()
            self.device.click(w // 2, int(h * 0.04))
            self.log_action("click", selector="chat_list_top_fallback")

        self.wait_for_screen_state(
            "telegram_chat_list_screen",
            self._is_telegram_chat_list_screen,
            timeout=1.5,
            capture_on_timeout=False,
        )

    def _dismiss_chat_banner_if_exists(self):
        try:
            close_btn = self.device.xpath(BANNER_CLOSE_XPATH)

            if close_btn.exists:
                close_btn.click()
                self.log_action("click", selector="dismiss_chat_banner")
                self.wait_for_screen_state(
                    "telegram_chat_list_screen",
                    self._is_telegram_chat_list_screen,
                    timeout=1.5,
                    capture_on_timeout=False,
                )
                return True

        except Exception as e:
            logger.debug("%s dismiss_chat_banner failed: %s", self.aura_prefix(), e)

        return False

    def _is_blank(self, value):
        return value is None or str(value).strip() == ""

    def _is_unknown_mobile(self, value):
        if value is None:
            return True
        normalized = str(value).strip().lower()
        return normalized == "" or normalized == "unknown"

    def _is_ambiguous_deleted_account(self, chatroom_name, chatroom_user_id, chatroom_mobile, chatroom_type=None):
        name = (chatroom_name or "").strip().lower()
        if name != "deleted account":
            return False
        if not self._is_blank(chatroom_user_id):
            return False
        if not self._is_unknown_mobile(chatroom_mobile):
            return False


        if chatroom_type is not None and str(chatroom_type).strip():
            return str(chatroom_type).strip().lower() == "direct"

        return True

    def _make_storage_chatroom_id(self, logical_chatroom_id, ambiguous_deleted):
        if not ambiguous_deleted:
            return logical_chatroom_id




        self._ambiguous_deleted_account_counter += 1
        return f"{logical_chatroom_id}_ambiguous_deleted_{self._ambiguous_deleted_account_counter:03d}"

    def _is_chatroom_type_match(self, keyword, text):
        normalized_keyword = (keyword or "").lower().strip()
        normalized_text = (text or "").lower().strip()

        if normalized_keyword == "bot":
            return normalized_text == "bot"

        return normalized_keyword in normalized_text

    def _is_any_chatroom_type_text(self, text):
        return any(self._is_chatroom_type_match(keyword, text) for keyword in CHATROOM_PROFILE_TYPE)

    def _click_chatroom_row_or_raise(self, row):
        clicked = self._click_chatroom_row(
            row["bounds"],
            list_bounds=self.chatroom_list_bounds,
            prefer_visible_lower_third=True,
        )
        if not clicked:
            raise RuntimeError("invalid_chatroom_row_bounds")

    def collect_chatrooms(self, account):
        opened = self.safe_click(
            "telegram_chats_tab",
            lambda: self.device.xpath(CHATS).click(),
            expected_state_name="telegram_chat_list_screen",
            expected_predicate=self._is_telegram_chat_list_screen,
            timeout=3.0,
        )
        if not opened:
            logger.warning("%s Chat list open failed: account=%s", self.aura_prefix(), account)
            self.log_action("collect_chatrooms", selector=CHATS, result="fail_open_chats")
            return


        self._dismiss_chat_banner_if_exists()

        chatroom_targets = self.get_chatroom_list(account=account)

        target_ids = {target["logical_chatroom_id"] for target in chatroom_targets}
        required_target_ids = {
            target["logical_chatroom_id"]
            for target in chatroom_targets
            if not target.get("ambiguous_deleted_account")
        }
        ambiguous_target_count = sum(1 for target in chatroom_targets if target.get("ambiguous_deleted_account"))
        ambiguous_storage_queue = {}
        for target in chatroom_targets:
            if not target.get("ambiguous_deleted_account"):
                continue
            logical_chatroom_id = target["logical_chatroom_id"]
            ambiguous_storage_queue.setdefault(logical_chatroom_id, []).append(target["chat_id"])

        self.targets[account] = target_ids

        completed = self.completed_targets.setdefault(account, set())
        ambiguous_completed = 0


        direction = 'Down'
        pass_idx = 1
        pass_start = time.time()
        loop_count = 0
        no_progress_loops = 0
        max_collect_loops = max(1, int((self.profile or {}).get("telegram_collect_chatrooms_loop_max", 120)))
        max_collect_passes = max(1, int((self.profile or {}).get("telegram_collect_chatrooms_pass_max", 6)))
        max_no_progress_loops = max(1, int((self.profile or {}).get("telegram_collect_chatrooms_no_progress_max", 18)))
        target_name_by_id = {
            target["logical_chatroom_id"]: target.get("name") or target["logical_chatroom_id"]
            for target in chatroom_targets
        }

        while True:
            loop_count += 1
            if completed.issuperset(required_target_ids) and ambiguous_completed >= ambiguous_target_count:
                break
            if loop_count > max_collect_loops:
                missing = sorted(target_name_by_id.get(tid, tid) for tid in (required_target_ids - completed))
                self.log_action(
                    "collect_chatrooms",
                    selector=f"account={account}",
                    result="stop_max_loops",
                    artifacts=[
                        f"loop_count={loop_count}",
                        f"max_collect_loops={max_collect_loops}",
                        f"pass_idx={pass_idx}",
                        f"missing={json.dumps(missing, ensure_ascii=False)}",
                    ],
                )
                break
            if pass_idx > max_collect_passes:
                missing = sorted(target_name_by_id.get(tid, tid) for tid in (required_target_ids - completed))
                self.log_action(
                    "collect_chatrooms",
                    selector=f"account={account}",
                    result="stop_max_passes",
                    artifacts=[
                        f"pass_idx={pass_idx}",
                        f"max_collect_passes={max_collect_passes}",
                        f"loop_count={loop_count}",
                        f"missing={json.dumps(missing, ensure_ascii=False)}",
                    ],
                )
                break
            if not self._is_telegram_chat_list_screen():
                opened = self.safe_click(
                    "telegram_chats_tab_recover",
                    lambda: self.device.xpath(CHATS).click(),
                    expected_state_name="telegram_chat_list_screen",
                    expected_predicate=self._is_telegram_chat_list_screen,
                    timeout=2.5,
                    settle_sec=0.1,
                )
                if not opened:
                    self.log_action("collect_chatrooms", selector="pre_collect_chat_list", result="fail")
                    break
            chatroom_rows = self._prepare_chatroom_list_snapshot(reason="pre_collect_snapshot")
            if chatroom_rows is None:
                continue

            progress_before = (len(completed), ambiguous_completed)

            if any(p and self.device(text=p).exists for p in CHATRROM_END_ANCHOR):
                direction = 'Up'

            if chatroom_rows:
                for row in chatroom_rows:
                    opened = self.safe_click(
                        "chatroom_list_item_collect",
                        lambda row=row: self._click_chatroom_row_or_raise(row),
                        expected_state_name="telegram_chatroom_screen",
                        expected_predicate=self._is_telegram_chatroom_screen,
                        timeout=3.0,
                        settle_sec=0.1,
                    )
                    if not opened:
                        self.log_action("chatroom_skip", selector="open_failed", artifacts=[f"bounds={row['bounds']}", f"text={row['text']}"])
                        continue

                    self.log_action("click", selector="CHATROOM_ITEM", artifacts=[f"bounds={row['bounds']}", f"text={row['text']}"])

                    chat_type, chat_name, chat_user_id, logical_chatroom_id, chat_mobile = self.check_chatroom_type()
                    ambiguous_deleted = self._is_ambiguous_deleted_account(
                        chat_name,
                        chat_user_id,
                        chat_mobile,
                        chat_type,
                    )
                    identity_status = "ambiguous" if ambiguous_deleted else "identified"
                    dedup_applied = not ambiguous_deleted
                    if ambiguous_deleted:
                        storage_chatroom_id = None
                        queued_ids = ambiguous_storage_queue.get(logical_chatroom_id) or []
                        if queued_ids:
                            storage_chatroom_id = queued_ids.pop(0)
                        if not storage_chatroom_id:
                            storage_chatroom_id = self._make_storage_chatroom_id(logical_chatroom_id, True)
                        self.log_action(
                            "chatroom_identity",
                            selector="profile",
                            result="ambiguous_deleted_account_no_dedup",
                            artifacts=[
                                f"logical_chatroom_id={logical_chatroom_id}",
                                f"storage_chatroom_id={storage_chatroom_id}",
                                f"chatroom_name={chat_name}",
                                f"chatroom_type={chat_type}",
                                f"chatroom_user_id={chat_user_id}",
                                f"chatroom_mobile={chat_mobile}",
                                "identity_status=ambiguous",
                                "dedup_applied=False",
                            ],
                        )
                    else:
                        storage_chatroom_id = logical_chatroom_id

                    if logical_chatroom_id not in target_ids:
                        self.log_action(
                            "chatroom_skip",
                            selector=f"out_of_scope:{chat_type}:{chat_name}",
                            artifacts=[logical_chatroom_id, storage_chatroom_id],
                        )
                        self._press_back_to_chat_list(reason="out_of_scope_chatroom")
                        continue
                    self.log_action(
                        "chatroom_enter",
                        selector=f"{chat_type}:{chat_name}",
                        artifacts=[
                            f"logical_chatroom_id={logical_chatroom_id}",
                            f"storage_chatroom_id={storage_chatroom_id}",
                            f"identity_status={identity_status}",
                            f"dedup_applied={dedup_applied}",
                        ],
                    )

                    processed = False
                    try:
                        if logical_chatroom_id in completed and not ambiguous_deleted:
                            self.log_action(
                                "chatroom_skip",
                                selector=f"{chat_type}:{chat_name}",
                                artifacts=[
                                    f"logical_chatroom_id={logical_chatroom_id}",
                                    "dedup_applied=True",
                                ],
                            )
                        else:
                            if logical_chatroom_id in completed and ambiguous_deleted:
                                self.log_action(
                                    "chatroom_skip",
                                    selector="chatroom_id",
                                    result="duplicate_but_collected_ambiguous_deleted_account",
                                    artifacts=[
                                        f"logical_chatroom_id={logical_chatroom_id}",
                                        f"storage_chatroom_id={storage_chatroom_id}",
                                        f"chatroom_name={chat_name}",
                                        f"chatroom_type={chat_type}",
                                        f"chatroom_user_id={chat_user_id}",
                                        f"chatroom_mobile={chat_mobile}",
                                        "identity_status=ambiguous",
                                        "dedup_applied=False",
                                    ],
                                )
                            self.current_chat_id = storage_chatroom_id
                            if chat_type in ('Direct', 'Bot', 'Telegram'):
                                self.process_chatroom_dm(
                                    account,
                                    chat_type,
                                    chat_name,
                                    storage_chatroom_id,
                                    chat_user_id=chat_user_id,
                                    logical_chatroom_id=logical_chatroom_id,
                                    chat_mobile=chat_mobile,
                                    ambiguous_deleted_account=ambiguous_deleted,
                                    identity_status=identity_status,
                                    dedup_applied=dedup_applied,
                                )
                            elif chat_type == 'Group':
                                self.process_chatroom_group(
                                    account,
                                    chat_type,
                                    chat_name,
                                    storage_chatroom_id,
                                    chat_user_id=chat_user_id,
                                    logical_chatroom_id=logical_chatroom_id,
                                    chat_mobile=chat_mobile,
                                    ambiguous_deleted_account=ambiguous_deleted,
                                    identity_status=identity_status,
                                    dedup_applied=dedup_applied,
                                )
                            elif chat_type == 'Channel':
                                self.process_chatroom_channel(
                                    account,
                                    chat_type,
                                    chat_name,
                                    storage_chatroom_id,
                                    chat_user_id=chat_user_id,
                                    logical_chatroom_id=logical_chatroom_id,
                                    chat_mobile=chat_mobile,
                                    ambiguous_deleted_account=ambiguous_deleted,
                                    identity_status=identity_status,
                                    dedup_applied=dedup_applied,
                                )
                            processed = True
                    finally:
                        self.current_chat_id = None
                        returned = self._press_back_to_chat_list(reason="chatroom_done")
                        self.log_action(
                            "chatroom_done",
                            selector=f"{chat_type}:{chat_name}",
                            result="success" if returned else "return_failed",
                            artifacts=[
                                f"logical_chatroom_id={logical_chatroom_id}",
                                f"storage_chatroom_id={storage_chatroom_id}",
                                f"identity_status={identity_status}",
                                f"dedup_applied={dedup_applied}",
                            ],
                        )

                    if processed:
                        if ambiguous_deleted:
                            ambiguous_completed += 1
                        else:
                            completed.add(logical_chatroom_id)

            progress_after = (len(completed), ambiguous_completed)
            if progress_after == progress_before:
                no_progress_loops += 1
            else:
                no_progress_loops = 0

            if no_progress_loops >= max_no_progress_loops:
                missing = sorted(target_name_by_id.get(tid, tid) for tid in (required_target_ids - completed))
                self.log_action(
                    "collect_chatrooms",
                    selector=f"account={account}",
                    result="stop_no_progress",
                    artifacts=[
                        f"no_progress_loops={no_progress_loops}",
                        f"max_no_progress_loops={max_no_progress_loops}",
                        f"pass_idx={pass_idx}",
                        f"loop_count={loop_count}",
                        f"missing={json.dumps(missing, ensure_ascii=False)}",
                    ],
                )
                break
            try:
                if direction == 'Down':
                    self._scroll_chatroom_list_down()
                    self.log_action("swipe", selector="scroll_down")
                else:
                    duration = time.time() - pass_start
                    logger.info("%s Chatroom pass %d (Down) (duration=%.2fs)", self.aura_prefix(), pass_idx, duration)
                    self.log_action("chatroom_pass", selector="Down", artifacts=[f"duration={duration:.2f}s"])
                    pass_idx += 1
                    pass_start = time.time()
                    direction = 'Down'
                    self.go_chat_list_top()
            except Exception as e:
                logger.warning("%s Chatroom swipe failed: %s", self.aura_prefix(), e)
                self.log_action("collect_chatrooms", selector="swipe", result="fail", error=e)
                break

        self.log_action(
            "collect_chatrooms",
            selector=f"account={account}",
            result="done",
            artifacts=[f"completed={len(completed)}", f"ambiguous_completed={ambiguous_completed}"],
        )

    def gen_chatroom_id(self, name, type, user_id, mobile):
        sha256_hash = hashlib.sha256()
        name = "" if name is None else str(name)
        type = "" if type is None else str(type)
        user_id = "" if user_id is None else str(user_id)
        mobile = "" if mobile is None else str(mobile)
        if type == 'Direct':
            sha256_hash.update((name + type + user_id + mobile).encode())
        elif type == 'Group':
            members = self.device.xpath(CHATROOM_GROUP_MEMBER_INFO).all()[1:]
            hash_data = ''
            for member in members:
                base = member.get_xpath()
                mem = self.device.xpath(base + '/android.widget.TextView').all()
                if len(mem) > 0:
                    mem_name = mem[0].text
                    hash_data += mem_name
            hash_data = "".join(sorted(hash_data))
            if hash_data:
                sha256_hash.update(hash_data.encode())
            else:
                sha256_hash.update((name + type).encode())
        elif type == 'Bot':
            sha256_hash.update((name + type + user_id).encode())
        elif type == 'Channel':
            sha256_hash.update((name + type).encode())
        elif type == 'Telegram':
            sha256_hash.update((name + type + user_id + mobile).encode())
        else:
            sha256_hash.update((name + type + user_id + mobile).encode())

        return sha256_hash.hexdigest()

    def parse_chat_profile_info(self):
        name = ''
        type_text = ''
        username = ''
        mobile = ''

        root = ET.fromstring(self.device.dump_hierarchy())
        telegram_package = getattr(self, "packageName", "org.telegram.messenger")

        def is_telegram_textview(node) -> bool:
            return (
                self._xml_class(node) == "android.widget.TextView"
                and bool(self._xml_text(node))
                and self._xml_package(node) == telegram_package
            )

        textviews = [
            self._xml_text(n) for n in root.iter()
            if is_telegram_textview(n)
        ]

        recycler_descendant_ids = set()
        for rv in root.iter():
            if self._xml_class(rv) == "androidx.recyclerview.widget.RecyclerView":
                recycler_descendant_ids.update(id(x) for x in rv.iter())

        header_textviews = [
            self._xml_text(n) for n in root.iter()
            if (
                is_telegram_textview(n)
                and id(n) not in recycler_descendant_ids
            )
        ]

        for n in root.iter():
            text = self._xml_text(n)
            if text.startswith(PROFILE_USERNAME_PREFIX):
                username = text.split(":", 1)[1].strip()
                break

        if not username:
            for text in textviews:
                if text.startswith("@"):
                    username = text
                    break

        for n in root.iter():
            text = self._xml_text(n)
            if text.startswith(PROFILE_MOBILE_PREFIX):
                mobile = text.split(":", 1)[1].strip() or None
                break

        if not mobile:
            for text in textviews:
                if re.match(r"^\+\d{1,3}[\s\d\-()]+$", text):
                    mobile = text
                    break

        for keyword in CHATROOM_PROFILE_TYPE:
            candidates = [
                text for text in textviews
                if self._is_chatroom_type_match(keyword, text)
            ]

            if candidates:
                type_text = min(candidates, key=len)
                break

        name_candidates = []
        for text in header_textviews:
            raw = text.strip()

            if raw.endswith(", Verified"):
                raw = raw.replace(", Verified", "").strip()

            if not raw:
                continue
            if raw in CHATROOM_PROFILE_SKIP_STRING:
                continue
            if raw.startswith("@"):
                continue
            if raw.startswith(PROFILE_USERNAME_PREFIX) or raw.startswith(PROFILE_MOBILE_PREFIX):
                continue
            if username and raw == username:
                continue
            if mobile and raw == mobile:
                continue
            if re.match(r"^\+\d{1,3}[\s\d\-()]+$", raw):
                continue
            if type_text and raw == type_text:
                continue
            if self._is_any_chatroom_type_text(raw):
                continue
            if raw.startswith("By launching this mini app"):
                continue

            name_candidates.append(raw)

        if name_candidates:
            name = name_candidates[-1]

        return name, (type_text or "").lower(), username, mobile

    def check_chatroom_type(self):
        opened = self.safe_click(
            "chatroom_profile_open",
            lambda: self.device.xpath(CHATROOM_TYPE_PAGE).click(),
            expected_state_name="telegram_chatroom_profile_screen",
            expected_predicate=self._is_telegram_chatroom_profile_screen,
            timeout=2.5,
        )
        if not opened:
            self.log_screen_transition("chatroom_profile_open", result="unconfirmed")
        else:
            self.log_screen_transition("chatroom_profile_open")

        chatroom_name, chatroom_type, chatroom_user_id, chatroom_mobile = self.parse_chat_profile_info()

        if (
            'subscriber' in chatroom_type
            or 'private channel' in chatroom_type
            or 'public channel' in chatroom_type
        ):
            chatroom_type = 'Channel'
        elif (
            'member' in chatroom_type
            or 'private group' in chatroom_type
            or 'public group' in chatroom_type
        ):
            chatroom_type = 'Group'
        elif 'bot' in chatroom_type:
            chatroom_type = 'Bot'
        elif 'monthly user' in chatroom_type:
            chatroom_type = 'Bot'
        elif 'service notifications' in chatroom_type:
            chatroom_type = 'Telegram'
        else:
            chatroom_type = 'Direct'

        chatroom_id = self.gen_chatroom_id(chatroom_name, chatroom_type, chatroom_user_id, chatroom_mobile)
        self.log_action(
            "inspect_chatroom_profile",
            selector=f"{chatroom_type}:{chatroom_name}",
            artifacts=[
                f"user_id={chatroom_user_id}",
                f"mobile={chatroom_mobile}",
                f"chat_id={chatroom_id}",
            ],
        )
        try:
            self.device.press('back')
            self.log_action("press", selector="back", artifacts=["reason=chatroom_profile_close"])
        except Exception as e:
            self.log_action("press", selector="back", result="fail", error=e, artifacts=["reason=chatroom_profile_close"])
        returned = self.wait_for_screen_state(
            "telegram_chatroom_screen",
            self._is_telegram_chatroom_screen,
            timeout=2.5,
            capture_on_timeout=True,
        )
        self.log_screen_transition("chatroom_profile_close")
        if not returned:
            self.log_screen_transition("chatroom_profile_close", result="unconfirmed")
        return (chatroom_type, chatroom_name, chatroom_user_id, chatroom_id, chatroom_mobile)

    def chatroom_go_to_bottom(self):
        try:
            settle_sec = float((getattr(self, "profile", None) or {}).get("telegram_go_to_bottom_settle_sec", 0.35))
        except Exception:
            settle_sec = 0.35

        def _post_settle():
            if settle_sec > 0:
                self._sleep(settle_sec)
                self.log_action(
                    "chatroom_go_to_bottom_settle",
                    selector="post_bottom_align",
                    artifacts=[f"sleep={settle_sec}s"],
                )

        btn = self.device(description=GO_TO_BOTTOM_DESC)
        if btn.exists():
            btn.click()
            self.log_action("click", selector=GO_TO_BOTTOM_DESC)
            _post_settle()
            return
        max_swipes = 12
        distance_ratio = 0.6
        try:
            safe_top, safe_bottom = self._get_chatroom_actionable_bounds()
            performed = 0
            for _ in range(max_swipes):
                before = self._current_chat_history_signature()
                scroll_down(
                    self.device,
                    distance_ratio=distance_ratio,
                    top_bound=safe_top,
                    bottom_bound=safe_bottom,
                )
                performed += 1
                changed = self.wait_for_list_changed(
                    "chat_history_scroll_down_to_bottom",
                    before,
                    self._current_chat_history_signature,
                    timeout=1.0,
                    interval=0.2,
                )
                if not changed:
                    break
            self.log_action(
                "swipe",
                selector="scroll_down_to_bottom",
                artifacts=[
                    f"max_swipes={max_swipes}",
                    f"performed={performed}",
                    f"distance_ratio={distance_ratio}",
                    f"safe_top={safe_top}",
                    f"safe_bottom={safe_bottom}",
                ],
            )
            _post_settle()
        except Exception as e:
            self.log_action(
                "swipe",
                selector="scroll_down_to_bottom",
                result="fail",
                error=e,
                artifacts=[f"max_swipes={max_swipes}", f"distance_ratio={distance_ratio}"],
            )

    def process_chatroom_dm(self, account, chat_type, chat_name, chat_id, chat_user_id=None, logical_chatroom_id=None, chat_mobile=None, ambiguous_deleted_account=False, identity_status=None, dedup_applied=True):
        return self._process_chatroom_generic(account, chat_type, chat_name, chat_id, parser=self.process_message_dm, allow_sender_context=False, chat_user_id=chat_user_id, logical_chatroom_id=logical_chatroom_id, chat_mobile=chat_mobile, ambiguous_deleted_account=ambiguous_deleted_account, identity_status=identity_status, dedup_applied=dedup_applied)

    def process_chatroom_group(self, account, chat_type, chat_name, chat_id, chat_user_id=None, logical_chatroom_id=None, chat_mobile=None, ambiguous_deleted_account=False, identity_status=None, dedup_applied=True):
        return self._process_chatroom_generic(account, chat_type, chat_name, chat_id, parser=self.process_message_group, allow_sender_context=True, chat_user_id=chat_user_id, logical_chatroom_id=logical_chatroom_id, chat_mobile=chat_mobile, ambiguous_deleted_account=ambiguous_deleted_account, identity_status=identity_status, dedup_applied=dedup_applied)

    def process_chatroom_channel(self, account, chat_type, chat_name, chat_id, chat_user_id=None, logical_chatroom_id=None, chat_mobile=None, ambiguous_deleted_account=False, identity_status=None, dedup_applied=True):
        return self._process_chatroom_generic(account, chat_type, chat_name, chat_id, parser=self.process_message_group, allow_sender_context=True, chat_user_id=chat_user_id, logical_chatroom_id=logical_chatroom_id, chat_mobile=chat_mobile, ambiguous_deleted_account=ambiguous_deleted_account, identity_status=identity_status, dedup_applied=dedup_applied)

    def _chat_message_node_raw_text(self, node):
        try:
            text = (node.text or "").strip()
        except Exception:
            text = ""

        if text:
            return text, "text"

        try:
            info = node.info or {}
        except Exception:
            info = {}

        for key in ("contentDescription", "content-desc", "description"):
            value = (info.get(key) or "").strip()
            if value:
                return value, key

        return "", "empty"

    def _snapshot_chat_messages_page(self, msgs, safe_top, safe_bottom):
        page_rows = []
        skipped_rows = []
        try:
            attachment_bottom_slack = int(
                (getattr(self, "profile", None) or {}).get(
                    "telegram_attachment_bottom_slack_px",
                    64,
                )
            )
        except Exception:
            attachment_bottom_slack = 64

        for msg in reversed(msgs):
            try:
                msg_bounds = msg.info.get("bounds")
            except Exception:
                msg_bounds = None
            if not msg_bounds:
                skipped_rows.append({"reason": "missing_bounds", "bounds": None})
                continue

            raw_text, raw_source = self._chat_message_node_raw_text(msg)
            if not (raw_text or "").strip():
                skipped_rows.append({"reason": "empty_text", "bounds": msg_bounds, "raw_source": raw_source})
                continue

            readability_reason = self._bounds_readability_reason(msg_bounds, safe_top, safe_bottom)
            if readability_reason:
                skipped_rows.append({
                    "reason": readability_reason,
                    "bounds": msg_bounds,
                    "raw_text": raw_text,
                    "raw_source": raw_source,
                })
                continue

            msg_type_hint = self._peek_message_type(raw_text)
            attachment_safe_bottom = safe_bottom
            if isinstance(safe_bottom, int) and msg_type_hint in ("Photo", "Video", "File"):
                attachment_safe_bottom = safe_bottom + max(0, attachment_bottom_slack)
            attachment_actionable = self._bounds_visibility_reason(msg_bounds, safe_top, attachment_safe_bottom) is None

            page_rows.append({
                "raw_text": raw_text,
                "bounds": msg_bounds,
                "msg_type_hint": msg_type_hint,
                "attachment_candidate": msg_type_hint in ("Photo", "Video", "File"),
                "attachment_actionable": attachment_actionable,
                "attachment_safe_bottom": attachment_safe_bottom,
                "raw_source": raw_source,
            })
        return page_rows, skipped_rows

    def _chat_history_page_signature(self, page_rows, limit=8):
        signature = []
        for page_row in (page_rows or [])[:limit]:
            raw_text = re.sub(r"\s+", " ", (page_row.get("raw_text") or "").strip())
            bounds = page_row.get("bounds") or {}
            top = int(bounds.get("top") or 0) // 32
            bottom = int(bounds.get("bottom") or 0) // 32
            signature.append((
                page_row.get("msg_type_hint") or "",
                page_row.get("raw_source") or "",
                raw_text,
                top,
                bottom,
            ))
        return tuple(signature)

    def _current_chat_history_signature(self, limit=8):
        try:
            safe_top, safe_bottom = self._get_chatroom_actionable_bounds()
            msgs = self.device.xpath(CHATROOM_ELEM).all()
            page_rows, _ = self._snapshot_chat_messages_page(msgs, safe_top, safe_bottom)
            return self._chat_history_page_signature(page_rows, limit=limit)
        except Exception:
            return tuple()

    def _chatroom_list_signature(self, limit=12):
        signature = []
        try:
            for row in self._snapshot_chatroom_rows(log_skips=False)[:limit]:
                bounds = row.get("visible_bounds") or row.get("bounds") or []
                signature.append((row.get("text") or "", tuple(bounds)))
        except Exception:
            return tuple()
        return tuple(signature)

    def _wait_for_chatroom_list_settled(self, *, reason: str, timeout: float = 1.8, interval: float = 0.2, stable_polls: int = 2) -> bool:
        attempts = {"count": 0}

        settled, signature, stable_count = self.wait_for_consecutive_same_sample(
            action="list_state",
            selector="chatroom_list_settled",
            sample_fn=self._chatroom_list_signature,
            timeout=max(0.0, timeout),
            stable_polls=stable_polls,
            interval=interval,
            valid_fn=lambda sample: self._is_telegram_chat_list_screen() and bool(sample),
            success_result="stable",
            timeout_result="timeout",
            success_artifacts_fn=lambda sample, stable: [
                f"reason={reason}",
                f"attempts={attempts['count']}",
                f"stable_polls={stable}",
                f"visible_rows={len(sample)}",
            ],
            timeout_artifacts_fn=lambda _sample, stable: [
                f"reason={reason}",
                f"attempts={attempts['count']}",
                f"stable_polls={stable}",
            ],
            on_poll=lambda: attempts.__setitem__("count", attempts["count"] + 1),
        )
        return settled

    def _prepare_chatroom_list_snapshot(
        self,
        *,
        reason: str,
        settle_timeout: float = 0.9,
        retry_sleep: float = 0.2,
        stable_polls: int = 1,
        min_fallback_rows: int = 2,
    ):
        settled = self._wait_for_chatroom_list_settled(
            reason=reason,
            timeout=settle_timeout,
            stable_polls=stable_polls,
        )
        chatroom_rows = self._snapshot_chatroom_rows()

        if settled:
            return chatroom_rows

        if len(chatroom_rows) >= min_fallback_rows:
            self.log_action(
                "list_state",
                selector=reason,
                result="fallback_visible_rows",
                artifacts=[f"visible_rows={len(chatroom_rows)}"],
            )
            return chatroom_rows

        self.log_action(
            "list_state",
            selector=reason,
            result="unsettled_skip",
            artifacts=[f"visible_rows={len(chatroom_rows)}"],
        )
        self._sleep(retry_sleep)
        return None

    def _process_chatroom_generic(self, account, chat_type, chat_name, chat_id, parser, allow_sender_context=True, chat_user_id=None, logical_chatroom_id=None, chat_mobile=None, ambiguous_deleted_account=False, identity_status=None, dedup_applied=True):
        screenshots = []
        chat_history_root = self.artifact_dir / account / "Chat History"
        chat_history_root.mkdir(parents=True, exist_ok=True)
        chat_dir = chat_history_root / (chat_type + '_' + chat_name + '_' + chat_id)
        chat_dir.mkdir(parents=True, exist_ok=True)
        download_dir = chat_dir / 'files'
        download_dir.mkdir(parents=True, exist_ok=True)

        self.chatroom_go_to_bottom()

        start_ts = time.time()
        current_date = None
        current_sender = None
        pending_messages = []
        seen_messages = set()
        downloaded_ids = set()
        downloaded_attachment_keys = set()
        parsed_messages = []
        message_screenshot_map = {}
        message_uitree_map = {}
        last_capture_path = None
        last_uitree_path = None
        pages_since_capture = 0
        capture_interval_pages = int((self.profile or {}).get("chat_snapshot_interval_pages", 2))
        capture_interval_pages = max(capture_interval_pages, 1)
        page_index = 0
        top_stable_rounds = 0
        top_stable_round_limit = int((self.profile or {}).get("chat_history_top_stable_rounds", 2))
        top_stable_round_limit = max(top_stable_round_limit, 1)
        try:
            chat_history_scroll_distance_ratio = float((self.profile or {}).get("chat_history_scroll_distance_ratio", 0.35))
        except (TypeError, ValueError):
            chat_history_scroll_distance_ratio = 0.35
        chat_history_scroll_distance_ratio = min(max(chat_history_scroll_distance_ratio, 0.2), 0.6)
        chat_entry = {
            "chat_id": chat_id,
            "logical_chatroom_id": logical_chatroom_id or chat_id,
            "type": chat_type,
            "name": chat_name,
            "user_id": chat_user_id,
            "mobile": chat_mobile,
            "ambiguous_deleted_account": ambiguous_deleted_account,
            "identity_status": identity_status or ("ambiguous" if ambiguous_deleted_account else "identified"),
            "dedup_applied": dedup_applied,
            "artifacts": screenshots,
        }
        chat_message_id_salt = f"chat_id={chat_id}|logical_chatroom_id={logical_chatroom_id or chat_id}"

        try:
            self.storage.upsert_chatrooms(self.app_id, account or "", [chat_entry], phase=self.current_phase or "")
        except Exception as e:
            logger.warning("%s Chatroom pre-upsert failed: %s", self.aura_prefix(), e)

        def flush_pending(date_ctx):
            nonlocal pending_messages
            if not pending_messages:
                return
            updated = []
            for m in pending_messages:
                ts = m.get("timestamp")
                if date_ctx and ts and "-" not in ts[:5]:
                    m["timestamp"] = f"{date_ctx.isoformat()} {ts}"
                if m.get("downloaded"):
                    if m.get("type") in ("Photo", "Video", "File") and m.get("bounds"):
                        downloaded_ids.add(m["message_id"])
                updated.append(m)
            pending_messages = []
            for m in updated:
                mid = m.get("record_id") or m.get("message_id")
                if mid and mid not in seen_messages:
                    seen_messages.add(mid)
                    parsed_messages.append(m)

        while True:
            page_index += 1
            if not self._is_telegram_chatroom_screen():
                self.log_action(
                    "chatroom_collection_interrupted",
                    selector="pre_page_not_chatroom",
                    result="fail_soft",
                    artifacts=[
                        f"chat_type={chat_type}",
                        f"chat_name={chat_name}",
                        f"chat_id={chat_id}",
                        f"page={page_index}",
                    ],
                )
                break

            msgs = self.device.xpath(CHATROOM_ELEM).all()
            page_record_ids = set()
            safe_top, safe_bottom = self._get_chatroom_actionable_bounds()
            page_rows, skipped_rows = self._snapshot_chat_messages_page(msgs, safe_top, safe_bottom)
            current_page_signature = self._chat_history_page_signature(page_rows)

            pending_ids = {pm.get("record_id") or pm.get("message_id") for pm in pending_messages if pm.get("record_id") or pm.get("message_id")}
            self.log_action(
                "chat_history_page_state",
                selector=f"{chat_type}:{chat_name}",
                artifacts=[
                    f"page={page_index}",
                    f"safe_top={safe_top}",
                    f"safe_bottom={safe_bottom}",
                    f"visible_rows={len(page_rows)}",
                    f"skipped_rows={len(skipped_rows)}",
                    f"attachment_candidates={sum(1 for row in page_rows if row.get('attachment_candidate'))}",
                ],
            )
            for skipped in skipped_rows[:8]:
                raw_preview = (skipped.get("raw_text") or "").strip().replace("\n", " ")[:120]
                self.log_action(
                    "chat_message_row_skipped",
                    selector=skipped.get("reason"),
                    artifacts=[
                        f"page={page_index}",
                        f"bounds={skipped.get('bounds')}",
                        f"safe_top={safe_top}",
                        f"safe_bottom={safe_bottom}",
                        f"raw_source={skipped.get('raw_source', '')}",
                        f"raw_preview={raw_preview}",
                    ],
                )

            stop_chatroom_due_to_unexpected_screen = False
            for page_row_idx, page_row in enumerate(page_rows, start=1):
                self.current_message_id = None
                msg_bounds = page_row["bounds"]
                raw_text = page_row["raw_text"]
                msg_type_hint = page_row["msg_type_hint"]
                observation_id = self._build_page_observation_id(raw_text, msg_type_hint, msg_bounds, page_index, page_row_idx)
                attachment_action_key = None
                if page_row.get("attachment_candidate"):
                    attachment_action_key = self._build_attachment_action_observation_key(raw_text, msg_type_hint, msg_bounds)
                if page_row.get("attachment_candidate"):
                    raw_preview = (raw_text or "").strip().replace("\n", " ")[:160]
                    self.log_action(
                        "chat_attachment_candidate_row",
                        selector=str(msg_type_hint),
                        artifacts=[
                            f"page={page_index}",
                            f"page_row_index={page_row_idx}",
                            f"observation_id={observation_id}",
                            f"attachment_action_key={attachment_action_key}",
                            f"attachment_actionable={page_row.get('attachment_actionable')}",
                            f"bounds={msg_bounds}",
                            f"raw_source={page_row.get('raw_source', '')}",
                            f"raw_preview={raw_preview}",
                        ],
                    )
                prev_date = current_date
                parse_bounds = msg_bounds
                if page_row.get("attachment_candidate") and not page_row.get("attachment_actionable"):
                    self.log_action(
                        "attachment_download_suppressed",
                        selector="not_actionable_bounds",
                        result="skipped_partial_row",
                        artifacts=[
                            f"page={page_index}",
                            f"page_row_index={page_row_idx}",
                            f"observation_id={observation_id}",
                            f"attachment_action_key={attachment_action_key or ''}",
                            f"bounds={msg_bounds}",
                            f"safe_top={safe_top}",
                            f"safe_bottom={safe_bottom}",
                        ],
                    )
                    self.log_action(
                        "attachment_observation_pending",
                        selector=str(msg_type_hint),
                        result="pending_actionable_row",
                        artifacts=[
                            f"page={page_index}",
                            f"page_row_index={page_row_idx}",
                            f"observation_id={observation_id}",
                            f"attachment_action_key={attachment_action_key or ''}",
                            f"bounds={msg_bounds}",
                            "reason=partial_attachment_row_not_recorded",
                        ],
                    )
                    continue
                message_id_salt = chat_message_id_salt
                if allow_sender_context:
                    parsed, current_date, current_sender = parser(
                        raw_text,
                        bounds=parse_bounds,
                        current_date=current_date,
                        current_sender=current_sender,
                        seen_ids=seen_messages,
                        pending_ids=pending_ids,
                        downloaded_ids=downloaded_ids,
                        download_dir=download_dir,
                        message_id_salt=message_id_salt,
                        observation_id=observation_id,
                        page_index=page_index,
                        page_row_index=page_row_idx,
                        downloaded_attachment_keys=downloaded_attachment_keys,
                        attachment_action_key=attachment_action_key,
                    )
                else:
                    parsed, current_date, _ = parser(
                        raw_text,
                        bounds=parse_bounds,
                        current_date=current_date,
                        sender_ctx=chat_name,
                        seen_ids=seen_messages,
                        pending_ids=pending_ids,
                        downloaded_ids=downloaded_ids,
                        download_dir=download_dir,
                        message_id_salt=message_id_salt,
                        observation_id=observation_id,
                        page_index=page_index,
                        page_row_index=page_row_idx,
                        downloaded_attachment_keys=downloaded_attachment_keys,
                        attachment_action_key=attachment_action_key,
                    )

                if page_row.get("attachment_candidate") and not self._is_telegram_chatroom_screen():
                    loading_state = {"visible": False}
                    try:
                        loading_state = getattr(self, "_telegram_attachment_loading_dialog_state", lambda: {"visible": False})()
                    except Exception:
                        loading_state = {"visible": False}

                    if loading_state.get("visible"):
                        self.log_action(
                            "attachment_loading_dialog",
                            selector="post_attachment_check",
                            result="visible",
                            artifacts=[
                                f"chat_type={chat_type}",
                                f"chat_name={chat_name}",
                                f"chat_id={chat_id}",
                                f"page={page_index}",
                                f"page_row_index={page_row_idx}",
                                f"msg_type_hint={msg_type_hint}",
                                f"observation_id={observation_id}",
                                f"percent={loading_state.get('percent') if loading_state.get('percent') is not None else ''}",
                                "download_result_determined_by=device_snapshot_diff",
                            ],
                        )
                        self.wait_for_screen_state(
                            "telegram_chatroom_after_attachment_loading_dialog",
                            self._is_telegram_chatroom_screen,
                            timeout=2.0,
                            interval=0.2,
                            capture_on_timeout=False,
                        )

                    if not self._is_telegram_chatroom_screen():
                        try:
                            loading_state = getattr(self, "_telegram_attachment_loading_dialog_state", lambda: {"visible": False})()
                        except Exception:
                            loading_state = {"visible": False}
                        if loading_state.get("visible"):
                            try:
                                self.device.press("back")
                                self.log_action(
                                    "press",
                                    selector="back",
                                    artifacts=[
                                        "reason=attachment_loading_dialog_postcheck_close",
                                        f"percent={loading_state.get('percent') if loading_state.get('percent') is not None else ''}",
                                    ],
                                )
                            except Exception as e:
                                self.log_action("press", selector="back", result="fail", error=e, artifacts=["reason=attachment_loading_dialog_postcheck_close"])
                            self.wait_for_screen_state(
                                "telegram_chatroom_after_attachment_loading_dialog_close",
                                self._is_telegram_chatroom_screen,
                                timeout=2.0,
                                interval=0.2,
                                capture_on_timeout=True,
                            )

                if page_row.get("attachment_candidate") and not self._is_telegram_chatroom_screen():
                    self.log_action(
                        "chatroom_collection_interrupted",
                        selector="post_attachment_unexpected_screen",
                        result="fail_soft",
                        artifacts=[
                            f"chat_type={chat_type}",
                            f"chat_name={chat_name}",
                            f"chat_id={chat_id}",
                            f"page={page_index}",
                            f"page_row_index={page_row_idx}",
                            f"msg_type_hint={msg_type_hint}",
                            f"observation_id={observation_id}",
                        ],
                    )
                    stop_chatroom_due_to_unexpected_screen = True
                    break

                if current_date != prev_date and pending_messages:
                    flush_pending(current_date)
                    if allow_sender_context:
                        current_sender = None

                if not parsed:
                    continue

                msg_id = parsed.get("message_id")
                record_id = parsed.get("record_id") or msg_id
                if record_id:
                    page_record_ids.add(record_id)
                if msg_id:
                    self.current_message_id = msg_id

                if current_date:
                    if not record_id or record_id in seen_messages:
                        continue
                    if parsed.get("downloaded") and parsed.get("type") in ("Photo", "Video", "File") and msg_id:
                        downloaded_ids.add(msg_id)
                    seen_messages.add(record_id)
                    parsed_messages.append(parsed)
                else:
                    if record_id and record_id in seen_messages:
                        continue
                    if record_id and any((pm.get("record_id") or pm.get("message_id")) == record_id for pm in pending_messages):
                        continue
                    pending_messages.append(parsed)
                self.current_message_id = None

            if stop_chatroom_due_to_unexpected_screen:
                break

            should_capture = (
                len(screenshots) == 0
                or pages_since_capture >= capture_interval_pages
                or len(page_record_ids) > 0
                or top_stable_rounds > 0
            )

            if should_capture:
                screenshot_name = chat_dir / f"chatlog_{len(screenshots)}.jpg"
                ev = self.capture_visual_evidence(
                    screenshot_name,
                    screenshot_kind="screenshot_chat",
                    uitree_kind="uitree_chat",
                    account=account,
                    chat_id=chat_id,
                )
                screenshots.append(ev["screenshot_path"])
                shot_path = None
                tree_path = None
                if ev.get("screenshot_artifact"):
                    shot_path = ev["screenshot_artifact"].get("artifact_path")
                    last_capture_path = shot_path or last_capture_path
                if ev.get("uitree_artifact"):
                    tree_path = ev["uitree_artifact"].get("artifact_path")
                    last_uitree_path = tree_path or last_uitree_path
                if shot_path and page_record_ids:
                    for mid in page_record_ids:
                        if not mid:
                            continue
                        bucket = message_screenshot_map.setdefault(mid, [])
                        if shot_path not in bucket:
                            bucket.append(shot_path)
                if tree_path and page_record_ids:
                    for mid in page_record_ids:
                        if not mid:
                            continue
                        bucket = message_uitree_map.setdefault(mid, [])
                        if tree_path not in bucket:
                            bucket.append(tree_path)
                artifacts = [ev["screenshot_path"]]
                if ev.get("uitree_path"):
                    artifacts.append(ev["uitree_path"])
                self.log_action("chat_history_snapshot", selector=f"{chat_type}:{chat_name}", artifacts=artifacts)
                pages_since_capture = 0
            else:
                if last_capture_path and page_record_ids:
                    for mid in page_record_ids:
                        if not mid:
                            continue
                        bucket = message_screenshot_map.setdefault(mid, [])
                        if last_capture_path not in bucket:
                            bucket.append(last_capture_path)
                if last_uitree_path and page_record_ids:
                    for mid in page_record_ids:
                        if not mid:
                            continue
                        bucket = message_uitree_map.setdefault(mid, [])
                        if last_uitree_path not in bucket:
                            bucket.append(last_uitree_path)
                pages_since_capture += 1
                self.log_action(
                    "chat_history_snapshot_skip",
                    selector=f"{chat_type}:{chat_name}",
                    artifacts=[f"page={page_index}", f"visible_msgs={len(page_record_ids)}", f"since_last={pages_since_capture}"],
                )

            try:
                scroll_up(
                    self.device,
                    distance_ratio=chat_history_scroll_distance_ratio,
                    top_bound=safe_top,
                    bottom_bound=safe_bottom,
                )
                self.log_action(
                    "swipe",
                    selector="scroll_backward",
                    artifacts=[
                        f"distance_ratio={chat_history_scroll_distance_ratio}",
                        f"safe_top={safe_top}",
                        f"safe_bottom={safe_bottom}",
                    ],
                )
                changed = self.wait_for_list_changed(
                    "chat_history_scroll_backward",
                    current_page_signature,
                    self._current_chat_history_signature,
                    timeout=1.5,
                    interval=0.2,
                )
                if not changed:
                    top_stable_rounds += 1
                    self.log_action(
                        "chat_history_top_check",
                        selector=f"{chat_type}:{chat_name}",
                        result="stable",
                        artifacts=[
                            f"page={page_index}",
                            f"stable_rounds={top_stable_rounds}",
                            f"required={top_stable_round_limit}",
                            f"visible_rows={len(page_rows)}",
                            f"attachment_candidates={sum(1 for row in page_rows if row.get('attachment_candidate'))}",
                        ],
                    )
                    if top_stable_rounds >= top_stable_round_limit:
                        break
                else:
                    top_stable_rounds = 0
            except Exception as e:
                self.log_action("swipe", selector="scroll_backward", result="fail", error=e)
                break

        chat_entry = {
            "chat_id": chat_id,
            "logical_chatroom_id": logical_chatroom_id or chat_id,
            "type": chat_type,
            "name": chat_name,
            "user_id": chat_user_id,
            "mobile": chat_mobile,
            "ambiguous_deleted_account": ambiguous_deleted_account,
            "identity_status": identity_status or ("ambiguous" if ambiguous_deleted_account else "identified"),
            "dedup_applied": dedup_applied,
            "artifacts": screenshots,
        }

        if pending_messages:
            flush_pending(current_date)
        for m in parsed_messages:
            mid = m.get("record_id") or m.get("message_id")
            if not mid:
                continue
            m["screenshot_paths"] = message_screenshot_map.get(mid, [])
            m["uitree_paths"] = message_uitree_map.get(mid, [])
        self.chatrooms.setdefault(account, {})[chat_id] = chat_entry
        try:
            self.storage.upsert_chatrooms(self.app_id, account or "", [chat_entry], phase=self.current_phase or "")
        except Exception as e:
            logger.warning("%s Chatroom upsert failed: %s", self.aura_prefix(), e)
        if parsed_messages:
            self.storage.upsert_messages(
                self.app_id,
                account or "",
                chat_id,
                parsed_messages,
                phase=self.current_phase or "",
                chat_name=chat_name,
                chat_type=chat_type,
                logical_chatroom_id=logical_chatroom_id or chat_id,
                ambiguous_deleted_account=ambiguous_deleted_account,
                identity_status=identity_status or ("ambiguous" if ambiguous_deleted_account else "identified"),
                dedup_applied=dedup_applied,
            )
        duration = time.time() - start_ts
        logger.info("%s Chat history collected: account=%s chat_id=%s chat=%s (%s) (pages=%d, duration=%.2fs)", self.aura_prefix(), account, chat_id, chat_name, chat_type, len(screenshots), duration)

    def get_chatroom_list(self, account=None):
        chatrooms = []
        chatroom_keys = set()
        chatroom_payloads = []
        PATIENCE = 2
        pass_idx = 1
        pass_start = time.time()
        total_start = pass_start
        loop_count = 0
        no_growth_loops = 0
        max_list_loops = max(1, int((self.profile or {}).get("telegram_get_chatroom_list_loop_max", 80)))
        max_no_growth_loops = max(1, int((self.profile or {}).get("telegram_get_chatroom_list_no_growth_max", 12)))

        while True:
            loop_count += 1
            if loop_count > max_list_loops:
                self.log_action(
                    "get_chatroom_list",
                    selector=account or "default",
                    result="stop_max_loops",
                    artifacts=[
                        f"loop_count={loop_count}",
                        f"max_list_loops={max_list_loops}",
                        f"pass_idx={pass_idx}",
                        f"count={len(chatrooms)}",
                    ],
                )
                break
            if not self._is_telegram_chat_list_screen():
                opened = self.safe_click(
                    "telegram_chats_tab_recover",
                    lambda: self.device.xpath(CHATS).click(),
                    expected_state_name="telegram_chat_list_screen",
                    expected_predicate=self._is_telegram_chat_list_screen,
                    timeout=2.5,
                    settle_sec=0.1,
                )
                if not opened:
                    self.log_action("collect_chatrooms", selector="pre_snapshot_chat_list", result="fail")
                    break
            chatroom_rows = self._prepare_chatroom_list_snapshot(reason="pre_list_snapshot")
            if chatroom_rows is None:
                continue
            end_anchor_visible = any(p and self.device(text=p).exists for p in CHATRROM_END_ANCHOR)
            growth_before = len(chatroom_keys)

            if not chatroom_rows:
                self.log_action("chatroom_snapshot_empty", selector="direction=Down")
                try:
                    if not end_anchor_visible:
                        before_signature = self._chatroom_list_signature()
                        self._scroll_chatroom_list_down()
                        self.log_action("swipe", selector="scroll_down")
                        changed = self.wait_for_list_changed(
                            "chatroom_list_scroll_down",
                            before_signature,
                            self._chatroom_list_signature,
                            timeout=1.5,
                            interval=0.2,
                        )
                        if not changed:
                            self.log_action(
                                "get_chatroom_list",
                                selector=account or "default",
                                result="stop_unchanged_scroll",
                                artifacts=[
                                    f"pass_idx={pass_idx}",
                                    f"loop_count={loop_count}",
                                    f"count={len(chatrooms)}",
                                ],
                            )
                            break
                    else:
                        duration = time.time() - pass_start
                        logger.info("%s Chatroom pass %d (Down) (duration=%.2fs)", self.aura_prefix(), pass_idx, duration)
                        self.log_action("chatroom_pass", selector="Down", artifacts=[f"duration={duration:.2f}s"])
                        pass_idx += 1
                        pass_start = time.time()
                        self.go_chat_list_top()
                except Exception as e:
                    logger.warning("%s Chatroom recovery failed: %s", self.aura_prefix(), e)
                    self.log_action("collect_chatrooms", selector="recovery", result="fail", error=e)
                    break
                continue
            else:
                for row in chatroom_rows:
                    opened = self.safe_click(
                        "chatroom_list_item",
                        lambda row=row: self._click_chatroom_row_or_raise(row),
                        expected_state_name="telegram_chatroom_screen",
                        expected_predicate=self._is_telegram_chatroom_screen,
                        timeout=3.0,
                    )
                    if not opened:
                        self.log_action("chatroom_skip", selector="open_failed", artifacts=[f"bounds={row['bounds']}", f"text={row['text']}"])
                        continue

                    self.log_action("click", selector="CHATROOM_ITEM", artifacts=[f"bounds={row['bounds']}", f"text={row['text']}"])

                    if self.input_bound is None:
                        self.input_bound = self.device.xpath(CHATROOM_COMPOSER_BOX).info.get("bounds")

                    chat_type, chat_name, chat_user_id, logical_chatroom_id, chat_mobile = self.check_chatroom_type()
                    if self._is_invalid_chatroom_target(chat_type, chat_name):
                        self.log_action(
                            "chatroom_skip",
                            selector=f"invalid_target:{chat_type}:{chat_name}",
                            artifacts=[f"bounds={row['bounds']}"],
                        )
                        returned = self._press_back_to_chat_list(reason="invalid_chatroom_target")
                        if not returned:
                            self.log_action(
                                "collect_chatrooms",
                                selector="invalid_target_return",
                                result="fail",
                                artifacts=[f"chat_type={chat_type}", f"chat_name={chat_name}"],
                            )
                            return
                        continue
                    ambiguous_deleted = self._is_ambiguous_deleted_account(
                        chat_name,
                        chat_user_id,
                        chat_mobile,
                        chat_type,
                    )
                    storage_chatroom_id = self._make_storage_chatroom_id(logical_chatroom_id, ambiguous_deleted)
                    returned = self._press_back_to_chat_list(reason="inspect_chatroom_done")
                    if not returned:
                        self.log_action(
                            "collect_chatrooms",
                            selector="inspect_chatroom_return",
                            result="fail",
                            artifacts=[f"chat_type={chat_type}", f"chat_name={chat_name}"],
                        )
                        return
                    self.log_action(
                        "inspect_chatroom",
                        selector=f"{chat_type}:{chat_name}",
                        artifacts=[
                            f"logical_chatroom_id={logical_chatroom_id}",
                            f"storage_chatroom_id={storage_chatroom_id}",
                            f"ambiguous_deleted_account={ambiguous_deleted}",
                            f"identity_status={'ambiguous' if ambiguous_deleted else 'identified'}",
                            f"dedup_applied={False if ambiguous_deleted else True}",
                        ],
                    )
                    if ambiguous_deleted:
                        self.log_action(
                            "chatroom_identity",
                            selector="profile",
                            result="ambiguous_deleted_account_no_dedup",
                            artifacts=[
                                f"logical_chatroom_id={logical_chatroom_id}",
                                f"storage_chatroom_id={storage_chatroom_id}",
                                f"chatroom_name={chat_name}",
                                f"chatroom_type={chat_type}",
                                f"chatroom_user_id={chat_user_id}",
                                f"chatroom_mobile={chat_mobile}",
                                "identity_status=ambiguous",
                                "dedup_applied=False",
                            ],
                        )

                    target = {
                        "chat_id": storage_chatroom_id,
                        "logical_chatroom_id": logical_chatroom_id,
                        "name": chat_name,
                        "type": chat_type,
                        "user_id": chat_user_id,
                        "mobile": chat_mobile,
                        "ambiguous_deleted_account": ambiguous_deleted,
                        "identity_status": "ambiguous" if ambiguous_deleted else "identified",
                        "dedup_applied": False if ambiguous_deleted else True,
                        "artifacts": [],
                    }
                    chatroom_payloads.append(target)

                    dedup_key = (
                        storage_chatroom_id if ambiguous_deleted else logical_chatroom_id
                    )
                    if dedup_key not in chatroom_keys:
                        chatrooms.append(target)
                        chatroom_keys.add(dedup_key)

                growth_after = len(chatroom_keys)
                if growth_after == growth_before:
                    no_growth_loops += 1
                else:
                    no_growth_loops = 0
                if no_growth_loops >= max_no_growth_loops:
                    self.log_action(
                        "get_chatroom_list",
                        selector=account or "default",
                        result="stop_no_growth",
                        artifacts=[
                            f"no_growth_loops={no_growth_loops}",
                            f"max_no_growth_loops={max_no_growth_loops}",
                            f"pass_idx={pass_idx}",
                            f"count={len(chatrooms)}",
                        ],
                    )
                    break
                try:
                    if not end_anchor_visible:
                        before_signature = self._chatroom_list_signature()
                        self._scroll_chatroom_list_down()
                        self.log_action("swipe", selector="scroll_down")
                        changed = self.wait_for_list_changed(
                            "chatroom_list_scroll_down",
                            before_signature,
                            self._chatroom_list_signature,
                            timeout=1.5,
                            interval=0.2,
                        )
                        if not changed:
                            self.log_action(
                                "get_chatroom_list",
                                selector=account or "default",
                                result="stop_unchanged_scroll",
                                artifacts=[
                                    f"pass_idx={pass_idx}",
                                    f"loop_count={loop_count}",
                                    f"count={len(chatrooms)}",
                                ],
                            )
                            break
                    else:
                        duration = time.time() - pass_start
                        logger.info("%s Chatroom pass %d (Down) (duration=%.2fs)", self.aura_prefix(), pass_idx, duration)
                        self.log_action("chatroom_pass", selector="Down", artifacts=[f"duration={duration:.2f}s"])
                        pass_idx += 1
                        pass_start = time.time()
                        self.go_chat_list_top()
                except Exception as e:
                    logger.warning("%s Chatroom swipe failed: %s", self.aura_prefix(), e)
                    self.log_action("collect_chatrooms", selector="swipe", result="fail", error=e)
                    break

            if (pass_idx - 1) >= PATIENCE:
                break

        total_duration = time.time() - total_start
        logger.info(
            "%s Chatroom list collected: finished_at=%s (count=%d, duration=%.2fs)",
            self.aura_prefix(),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
            len(chatrooms),
            total_duration,
        )
        if account:
            self.storage.upsert_chatrooms(self.app_id, account, chatroom_payloads, phase=self.current_phase or "")
        return chatrooms
