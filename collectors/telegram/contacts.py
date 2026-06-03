import time
import logging
import xml.etree.ElementTree as ET
from utils.utils import scroll_down
from collectors.telegram.ui_selectors import (
    CHAT_LIST_TOP_TEXTS,
    CHATROOM_COMPOSER_DESC,
    CONTACT_LIST_HEADER_ACTION_DESCS,
    CONTACT_LIST_SEARCH_TEXT,
    CONTACT_LIST_SEARCH_XPATH,
    CONTACTS,
)
from collectors.telegram.mixin_base import TelegramCollectorDeps

logger = logging.getLogger(__name__)


class TelegramContactsMixin(TelegramCollectorDeps):
    def _get_contacts_actionable_bounds(self, root):
        safe_top = None
        safe_bottom = None
        screen_width = int(root.attrib.get("width") or 0)
        screen_height = int(root.attrib.get("height") or 0)

        for node in root.iter():
            if node.attrib.get("class") != "android.widget.EditText":
                continue
            text = (node.attrib.get("text") or "").strip()
            desc = (node.attrib.get("content-desc") or "").strip()
            if text != CONTACT_LIST_SEARCH_TEXT and desc != CONTACT_LIST_SEARCH_TEXT:
                continue
            bounds = self._xml_bounds(node)
            if bounds:
                safe_top = bounds[3]
                break

        if safe_top is None:
            top_bar_candidates = []
            for node in root.iter():
                if node.attrib.get("class") != "android.widget.FrameLayout":
                    continue
                bounds = self._xml_bounds(node)
                if not bounds:
                    continue
                left, top, right, bottom = bounds
                width = right - left
                height = bottom - top
                if top > 5 or height <= 0 or height > 320:
                    continue
                if screen_width and width < int(screen_width * 0.5):
                    continue

                descs = self._xml_descs(node)
                texts = self._xml_texts(node)
                has_contact_action = any(desc in CONTACT_LIST_HEADER_ACTION_DESCS for desc in descs)
                has_top_text = any(text in CHAT_LIST_TOP_TEXTS for text in texts)
                if has_contact_action or has_top_text:
                    top_bar_candidates.append(bounds)

            if top_bar_candidates:
                safe_top = max(bounds[3] for bounds in top_bar_candidates)

        web_tabs_candidates = []
        nav_candidates = []
        for node in root.iter():
            bounds = self._xml_bounds(node)
            if not bounds:
                continue

            desc = (node.attrib.get("content-desc") or "").strip()
            if desc == CHATROOM_COMPOSER_DESC:
                web_tabs_candidates.append(bounds[1])

            if node.attrib.get("class") == "android.widget.FrameLayout" and node.attrib.get("selected") == "true":
                texts = self._xml_texts(node)
                if any(text in ("Chats", "Contacts", "Settings", "Profile") for text in texts):
                    nav_candidates.append(bounds[1])

        if web_tabs_candidates:
            safe_bottom = min(web_tabs_candidates)
        elif nav_candidates:
            safe_bottom = min(nav_candidates)

        resolved_safe_top = safe_top if safe_top is not None else 0
        resolved_safe_bottom = safe_bottom if safe_bottom is not None else screen_height or None
        return resolved_safe_top, resolved_safe_bottom

    def _contact_row_visibility_reason(self, bounds, safe_top, safe_bottom, margin=8):
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            return "invalid_bounds"
        _left, top, _right, bottom = [int(v) for v in bounds]
        if bottom <= top:
            return "invalid_height"
        if isinstance(safe_top, int) and top < safe_top + margin:
            return "overlap_top"
        if isinstance(safe_bottom, int) and bottom > safe_bottom - margin:
            return "overlap_bottom"
        return None

    def _snapshot_contacts_page(self, root, safe_top, safe_bottom):
        page_contacts = []
        skipped = []

        for row in root.iter():
            if row.attrib.get("class") != "android.widget.CheckBox":
                continue

            bounds = self._xml_bounds(row)
            texts = self._xml_texts(row)
            if not texts:
                skipped.append({"reason": "empty_text", "bounds": bounds})
                continue

            reason = self._contact_row_visibility_reason(bounds, safe_top, safe_bottom)
            if reason:
                skipped.append({"reason": reason, "bounds": bounds, "name": texts[0]})
                continue

            page_contacts.append({
                "name": texts[0],
                "presence_text": texts[1] if len(texts) > 1 else None,
                "bounds": bounds,
            })

        return page_contacts, skipped

    def _contacts_page_signature(self, contacts, limit=12):
        signature = []
        for contact in (contacts or [])[:limit]:
            bounds = contact.get("bounds") or []
            top = int(bounds[1]) // 32 if isinstance(bounds, (list, tuple)) and len(bounds) == 4 else 0
            bottom = int(bounds[3]) // 32 if isinstance(bounds, (list, tuple)) and len(bounds) == 4 else 0
            signature.append((contact.get("name") or "", contact.get("presence_text") or "", top, bottom))
        return tuple(signature)

    def _current_contacts_signature(self, limit=12):
        try:
            root = ET.fromstring(self.device.dump_hierarchy())
            safe_top, safe_bottom = self._get_contacts_actionable_bounds(root)
            page_contacts, _ = self._snapshot_contacts_page(root, safe_top, safe_bottom)
            return self._contacts_page_signature(page_contacts, limit=limit)
        except Exception:
            return tuple()

    def _is_telegram_contacts_screen(self):
        try:
            if self.device(text=CONTACT_LIST_SEARCH_TEXT).exists:
                return True
            if self.device.xpath(CONTACT_LIST_SEARCH_XPATH).exists:
                return True
            if self.device(description=CONTACT_LIST_SEARCH_TEXT).exists and self.device(description="Change sorting").exists:
                return True
            return False
        except Exception:
            return False

    def collect_contacts(self, account):
        start_ts = time.time()
        opened = self.safe_click(
            "telegram_contacts_tab",
            lambda: self.device.xpath(CONTACTS).click(),
            expected_state_name="telegram_contacts_screen",
            expected_predicate=self._is_telegram_contacts_screen,
            timeout=3.0,
        )
        if not opened:
            logger.warning("%s Contacts open failed: account=%s", self.aura_prefix(), account)
            self.log_action("collect_contacts", selector=CONTACTS, result="fail_open_contacts")
            return

        contacts = []
        seen_names = set()
        no_new_count = 0
        PATIENCE = 2
        screenshot_counter = 0
        pages_since_capture = 0
        page_index = 0
        capture_interval_pages = int((self.profile or {}).get("contact_snapshot_interval_pages", 2))
        capture_interval_pages = max(capture_interval_pages, 1)
        contacts_dir = (self.artifact_dir / account / "Contact")
        contacts_dir.mkdir(parents=True, exist_ok=True)

        while True:
            page_index += 1

            try:
                xml = self.device.dump_hierarchy()
                root = ET.fromstring(xml)
            except Exception as e:
                logger.warning("%s Contacts XML read failed: %s", self.aura_prefix(), e)
                break

            safe_top, safe_bottom = self._get_contacts_actionable_bounds(root)
            page_contacts, skipped_contacts = self._snapshot_contacts_page(root, safe_top, safe_bottom)
            self.log_action(
                "contacts_page_state",
                selector="Contacts_list",
                artifacts=[
                    f"page={page_index}",
                    f"safe_top={safe_top}",
                    f"safe_bottom={safe_bottom}",
                    f"visible_contacts={len(page_contacts)}",
                    f"skipped_contacts={len(skipped_contacts)}",
                ],
            )
            for skipped in skipped_contacts[:8]:
                self.log_action(
                    "contact_row_skipped",
                    selector=skipped.get("reason"),
                    artifacts=[
                        f"page={page_index}",
                        f"name={skipped.get('name')}",
                        f"bounds={skipped.get('bounds')}",
                        f"safe_top={safe_top}",
                        f"safe_bottom={safe_bottom}",
                    ],
                )

            new_contacts = [
                c for c in page_contacts
                if c["name"] not in seen_names
            ]

            for contact in new_contacts:
                contacts.append(contact)
                seen_names.add(contact["name"])

            if not new_contacts:
                no_new_count += 1
            else:
                no_new_count = 0

            if no_new_count >= PATIENCE:
                self.log_action(
                    "collect_contacts",
                    selector="scroll",
                    result="end",
                    artifacts=[
                        f"page={page_index}",
                        f"total={len(seen_names)}",
                    ],
                )
                break

            should_capture = (
                pages_since_capture >= capture_interval_pages
                or len(new_contacts) > 0
            )

            if should_capture:
                screenshot_name = contacts_dir / f'contacts_{screenshot_counter}.jpg'
                ev = self.capture_visual_evidence(
                    screenshot_name,
                    screenshot_kind="screenshot_contacts",
                    uitree_kind="uitree_contacts",
                    account=account,
                )
                artifacts = [ev["screenshot_path"]]
                if ev.get("uitree_path"):
                    artifacts.append(ev["uitree_path"])
                self.log_action("screenshot", selector="Contacts_list", artifacts=artifacts)
                screenshot_counter += 1
                pages_since_capture = 0
            else:
                pages_since_capture += 1
                self.log_action(
                    "screenshot_skip",
                    selector="Contacts_list",
                    artifacts=[
                        f"page={page_index}",
                        f"new={len(new_contacts)}",
                        f"since_last={pages_since_capture}",
                    ],
                )
            try:
                before_signature = self._contacts_page_signature(page_contacts)
                scroll_down(
                    self.device,
                    top_bound=safe_top,
                    bottom_bound=safe_bottom,
                )
                self.log_action(
                    "swipe",
                    selector="scroll_down",
                    artifacts=[
                        f"safe_top={safe_top}",
                        f"safe_bottom={safe_bottom}",
                    ],
                )
                self.wait_for_list_changed(
                    "contacts_scroll_down",
                    before_signature,
                    self._current_contacts_signature,
                    timeout=1.5,
                    interval=0.2,
                )
            except Exception as e:
                logger.warning("%s Contacts scroll_down failed: %s", self.aura_prefix(), e)
                self.log_action("collect_contacts", selector="scroll_down", result="fail", error=e)
                break

        self.contacts[account] = contacts
        self.storage.upsert_contacts(
            self.app_id,
            account,
            self.contacts[account],
            phase=self.current_phase or "",
        )

        returned = self._return_to_chat_list(reason="collect_contacts_done")
        self.log_action("collect_contacts", selector="Contacts", artifacts=self.contacts)
        if not returned:
            self.log_action("collect_contacts", selector="Contacts", result="return_failed")

        logger.info(
            "%s Contacts collected: account=%s finished_at=%s (count=%d, duration=%.2fs)",
            self.aura_prefix(),
            account,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
            len(self.contacts[account]),
            time.time() - start_ts,
        )
