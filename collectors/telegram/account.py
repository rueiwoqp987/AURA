import time
import logging
import json
import re
import xml.etree.ElementTree as ET
from collectors.telegram.ui_selectors import (
    ACCOUNTS_SECTION_TITLE,
    CHATS,
    CHAT_LIST_HEADER_ACTION_DESCS,
    CHAT_LIST_SEARCH_TEXT,
    CHAT_LIST_SEARCH_XPATH,
    CHAT_LIST_TOP_TEXTS,
    PROFILE,
    PROFILE_ACTION_TEXTS,
    PROFILE_HEADER_DESCS,
    PROFILE_MOBILE_LABEL,
    PROFILE_MOBILE_PREFIX,
    PROFILE_USERNAME_LABEL,
    PROFILE_USERNAME_PREFIX,
    SETTINGS,
)
from collectors.telegram.mixin_base import TelegramCollectorDeps

logger = logging.getLogger(__name__)


class TelegramAccountMixin(TelegramCollectorDeps):
    def _get_account_name_and_bounds(self, account):
        if isinstance(account, dict):
            return account.get("user_name"), account.get("bounds")
        return account, None

    def _account_summary_artifacts(self, accounts):
        names = []
        bounds_count = 0

        for item in accounts or []:
            user_name, bounds = self._get_account_name_and_bounds(item)
            if user_name:
                names.append(user_name)
            if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                bounds_count += 1

        return [
            f"count={len(accounts or [])}",
            f"with_bounds={bounds_count}",
            f"names={json.dumps(names, ensure_ascii=False)}",
        ]

    def _bounds_artifact(self, bounds):
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            return "bounds=None"
        return f"bounds={list(bounds)}"

    def _is_account_meta_text(self, value):
        text = (value or "").strip()
        return (
            "@" in text
            or "\u2022" in text
            or re.search(r"\+\d{1,3}[\s\d\-()]+", text) is not None
        )

    def _extract_current_account_from_rows(self, rows):
        for row in rows:
            texts = self._xml_texts(row)
            if not texts:
                continue

            metas = [text for text in texts if self._is_account_meta_text(text)]
            names = [text for text in texts if not self._is_account_meta_text(text)]

            if metas and names:
                return {
                    "user_name": names[0],
                    "bounds": self._xml_bounds(row),
                }

        return None

    def init_download_path(self):
        # Before/after snapshot diff no longer requires moving user folders.
        # Keeping /sdcard/Download, /Pictures/Telegram, and /Movies/Telegram in
        # place avoids hiding user-visible media if a run is interrupted.
        self.telegram_download_backups = []
        snapshot_dirs = getattr(self, "telegram_attachment_snapshot_dirs", {}) or {}
        flattened = []
        if isinstance(snapshot_dirs, dict):
            for paths in snapshot_dirs.values():
                flattened.extend(paths or [])
        self.log_action(
            "init_telegram_download_paths",
            selector="snapshot_diff_no_backup",
            artifacts=[f"snapshot_dirs={list(dict.fromkeys(flattened))}"],
        )

    def restore_download_path(self):
        self.log_action("restore_telegram_download_paths", selector="snapshot_diff_no_backup", result="skip")
        self.telegram_download_backups = []

    def get_user_account_from_xml(self, dump_xml):
        try:
            root = ET.fromstring(dump_xml)
        except Exception:
            return []

        # Settings/Profile screen usually has one large RecyclerView.
        # Do not require the "Accounts" section, because single-account UI may omit it.
        rvs = [
            n for n in root.iter()
            if self._xml_class(n) == "androidx.recyclerview.widget.RecyclerView"
        ]
        if not rvs:
            return []

        # Prefer RecyclerView that contains an account-like meta text.
        account_like_rvs = []
        for rv in rvs:
            rv_texts = self._xml_texts(rv)
            if any(self._is_account_meta_text(x) for x in rv_texts):
                account_like_rvs.append(rv)

        if account_like_rvs:
            rv = max(account_like_rvs, key=lambda n: len(list(n)))
        else:
            rv = max(rvs, key=lambda n: len(list(n)))

        children = list(rv)
        users = []

        # 1. Current account: always try profile-header-style row first.
        current = self._extract_current_account_from_rows(children)
        if current:
            users.append(current)

        # 2. Additional accounts: only if "Accounts" section exists.
        idx = next((i for i, x in enumerate(children) if self._xml_has_text(x, ACCOUNTS_SECTION_TITLE)), None)
        if idx is None:
            return users

        for row in children[idx + 1:]:
            ts = self._xml_texts(row)

            if not ts:
                break

            # Additional account rows are single-TextView LinearLayout rows.
            if self._xml_class(row) == "android.widget.LinearLayout" and len(ts) == 1:
                users.append({
                    "user_name": ts[0],
                    "bounds": self._xml_bounds(row),
                })
                continue

            # Normal settings item starts.
            break

        return users

    def collect_user_account(self):
        start_ts = time.time()
        opened = self.safe_click(
            "telegram_settings_tab",
            lambda: self.device.xpath(SETTINGS).click(),
            expected_state_name="telegram_settings_screen",
            expected_predicate=self._is_telegram_settings_screen,
            timeout=2.5,
        )
        if not opened:
            logger.warning("%s User Account open settings failed", self.aura_prefix())
            self.log_action("collect_user_account", selector=SETTINGS, result="fail_open_settings")
            return
        
        try:
            screenshot_name = self.artifact_dir / f'user_accounts.jpg'
            xml_name = self.artifact_dir / "user_accounts.xml"
            self.device.screenshot(str(screenshot_name))
            self.register_artifact(screenshot_name, kind="screenshot_accounts")

            hierarchy = self.device.dump_hierarchy()
            xml_name.write_text(hierarchy, encoding="utf-8")
            self.register_artifact(xml_name, kind="uitree_accounts")
            self.accounts = self.get_user_account_from_xml(hierarchy)
             
            returned = self._return_to_chat_list(reason="collect_user_account_done")
            self.log_action(
                "collect_user_account",
                selector='user_account_list',
                result="success" if self.accounts and returned else "empty" if returned else "success_return_failed",
                artifacts=[
                    str(screenshot_name),
                    str(xml_name),
                    *self._account_summary_artifacts(self.accounts),
                ],
            )
            account_names = [item.get("user_name") for item in self.accounts if isinstance(item, dict) and item.get("user_name")]
            logger.info(
                "%s User Account collected: finished_at=%s (count=%d, names=%s, duration=%.2fs)",
                self.aura_prefix(),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                len(self.accounts),
                account_names,
                time.time() - start_ts,
            )
        except Exception as e:
            logger.warning("%s User Account read failed: %s", self.aura_prefix(), e)
            self.log_action("collect_user_account", selector='user_account_list', result="fail", error=e)

    def select_user_account(self, account):
        start_ts = time.time()
        account_name, cached_bounds = self._get_account_name_and_bounds(account)

        if not account_name:
            logger.warning("%s Select account skipped: missing user_name", self.aura_prefix())
            self.log_action("click", selector="account=unknown", result="fail")
            return

        # The first parsed row is the currently selected account on the snapshot screen.
        if self.accounts and account == self.accounts[0]:
            self.log_action(
                "select_user_account",
                selector=f"account={account_name}",
                result="skip_current",
                artifacts=[self._bounds_artifact(cached_bounds)],
            )
            logger.info(
                "%s Select account skipped for current account: account=%s finished_at=%s (duration=%.2fs)",
                self.aura_prefix(),
                account_name,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                time.time() - start_ts,
            )
            return

        if not self.device.xpath(SETTINGS).exists:
            logger.warning("%s Select account failed: settings entry not found for account=%s", self.aura_prefix(), account_name)
            self.log_action("click", selector=f"account={account_name}", result="fail")
            return

        fresh_bounds = None

        try:
            opened = self.safe_click(
                "telegram_settings_tab",
                lambda: self.device.xpath(SETTINGS).click(),
                expected_state_name="telegram_settings_screen",
                expected_predicate=self._is_telegram_settings_screen,
                timeout=2.5,
            )
            if not opened:
                raise RuntimeError("settings_screen_not_confirmed")

            accounts = self.get_user_account_from_xml(self.device.dump_hierarchy())
            target = next((item for item in accounts if item.get("user_name") == account_name), None)
            if target:
                fresh_bounds = target.get("bounds")
            self.log_action(
                "select_user_account_refresh",
                selector=f"account={account_name}",
                result="success" if target else "not_found",
                artifacts=[
                    *self._account_summary_artifacts(accounts),
                    self._bounds_artifact(fresh_bounds),
                ],
            )
        except Exception as e:
            logger.warning("%s Select account refresh failed: account=%s error=%s", self.aura_prefix(), account_name, e)
            self.log_action("select_user_account_refresh", selector=f"account={account_name}", result="fail", error=e)

        target_bounds = fresh_bounds or cached_bounds
        if not self._click_bounds_center(target_bounds):
            logger.warning("%s Select account failed: no clickable bounds for account=%s", self.aura_prefix(), account_name)
            self.log_action(
                "click",
                selector=f"account={account_name}",
                result="fail",
                artifacts=[
                    "bounds_source=fresh" if fresh_bounds else "bounds_source=cached",
                    self._bounds_artifact(target_bounds),
                ],
            )
            return

        self.wait_for_screen_state(
            "telegram_account_switch_settled",
            lambda: self._is_telegram_settings_screen() or self._is_telegram_chat_list_screen(),
            timeout=4.0,
            capture_on_timeout=False,
        )
        self.log_action(
            "click",
            selector=f"account={account_name}",
            artifacts=[
                "bounds_source=fresh" if fresh_bounds else "bounds_source=cached",
                self._bounds_artifact(target_bounds),
            ],
        )
        logger.info("%s Select account: account=%s finished_at=%s (duration=%.2fs)", self.aura_prefix(), account_name, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())), time.time() - start_ts)

    def get_telegram_profile_identifiers(self) -> dict:
        return {
            "mobile": self._get_text_value_after_colon(PROFILE_MOBILE_PREFIX),
            "username": self._get_text_value_after_colon(PROFILE_USERNAME_PREFIX),
        }

    def _get_text_value_after_colon(self, prefix: str):
        obj = self.device(textStartsWith=prefix)
        if not obj.exists:
            return None

        text = obj.info.get("text", "").strip()
        if ":" not in text:
            return None

        return text.split(":", 1)[1].strip() or None

    def _is_telegram_chat_list_screen(self) -> bool:
        try:
            if self.device(text=CHAT_LIST_SEARCH_TEXT).exists:
                return True
            if self.device.xpath(CHAT_LIST_SEARCH_XPATH).exists:
                return True
        except Exception:
            pass

        try:
            root = ET.fromstring(self.device.dump_hierarchy())
        except Exception:
            return False

        recycler_exists = any(self._xml_class(n) == "androidx.recyclerview.widget.RecyclerView" for n in root.iter())
        selected_chats_tab = any(
            self._xml_class(n) == "android.widget.FrameLayout"
            and n.attrib.get("selected") == "true"
            and "Chats" in self._xml_texts(n)
            for n in root.iter()
        )
        visible_chats_tab = any(
            self._xml_class(n) == "android.widget.TextView"
            and self._xml_text(n) == "Chats"
            and (bounds := self._xml_bounds(n))
            and bounds[1] >= 1800
            for n in root.iter()
        )

        header_text_found = False
        header_action_found = False
        for n in root.iter():
            bounds = self._xml_bounds(n)
            if not bounds:
                continue
            if bounds[1] > 320:
                continue

            if self._xml_class(n) == "android.widget.TextView" and self._xml_text(n) in CHAT_LIST_TOP_TEXTS:
                header_text_found = True
            if self._xml_class(n) == "android.widget.ImageButton" and self._xml_desc(n) in CHAT_LIST_HEADER_ACTION_DESCS:
                header_action_found = True

        return recycler_exists and (selected_chats_tab or visible_chats_tab) and (header_text_found or header_action_found)

    def _is_telegram_settings_screen(self) -> bool:
        try:
            if self.device(text=ACCOUNTS_SECTION_TITLE).exists:
                return True
            if self.device(text="Account").exists:
                return True
            if self.device(text="Chat Settings").exists:
                return True
            if self.device(text="Privacy & Security").exists:
                return True
            if self.device(text="Notifications").exists:
                return True
        except Exception:
            pass

        try:
            root = ET.fromstring(self.device.dump_hierarchy())
        except Exception:
            return False

        selected_settings_tab = any(
            self._xml_class(n) == "android.widget.FrameLayout"
            and n.attrib.get("selected") == "true"
            and "Settings" in self._xml_texts(n)
            for n in root.iter()
        )
        settings_markers = {"Account", "Chat Settings", "Privacy & Security", "Notifications", ACCOUNTS_SECTION_TITLE}
        marker_found = any(self._xml_text(n) in settings_markers for n in root.iter())
        return selected_settings_tab and marker_found

    def _is_telegram_profile_screen(self) -> bool:
        try:
            if self.device(textStartsWith=PROFILE_USERNAME_PREFIX).exists:
                return True
            if self.device(textStartsWith=PROFILE_MOBILE_PREFIX).exists:
                return True
            if self.device(text=PROFILE_USERNAME_LABEL).exists or self.device(text=PROFILE_MOBILE_LABEL).exists:
                return True
            action_count = sum(1 for text in PROFILE_ACTION_TEXTS if self.device(text=text).exists)
            header_count = sum(1 for desc in PROFILE_HEADER_DESCS if self.device(description=desc).exists)
            if action_count >= 2 and header_count >= 1:
                return True
            return False
        except Exception:
            return False

    def _return_to_chat_list(self, *, reason: str = "return_to_chat_list", max_back: int = 1) -> bool:
        returned = self.press_back_to_state(
            reason=reason,
            state_name="telegram_chat_list_screen",
            predicate=self._is_telegram_chat_list_screen,
            max_back=max_back,
            timeout=2.0,
        )
        if returned:
            return True

        opened = self.safe_click(
            "telegram_chats_tab_return",
            lambda: self.device.xpath(CHATS).click(),
            expected_state_name="telegram_chat_list_screen",
            expected_predicate=self._is_telegram_chat_list_screen,
            timeout=2.5,
            settle_sec=0.1,
        )
        self.log_action("chat_list_return", selector=reason, result="success_click_tab" if opened else "fail")
        return opened

    def _return_from_profile_to_chat_list(self) -> bool:
        return self._return_to_chat_list(reason="profile_done")

    def collect_user_profile(self, _account):
        start_ts = time.time()
        opened = self.safe_click(
            "telegram_profile_tab",
            lambda: self.device.xpath(PROFILE).click(),
            expected_state_name="telegram_profile_screen",
            expected_predicate=self._is_telegram_profile_screen,
            timeout=3.0,
        )
        if not opened:
            logger.warning("%s User Profile open failed: account=%s", self.aura_prefix(), _account)
            self.log_action("collect_user_profile", selector=PROFILE, result="fail_open_profile")
            return

        try:
            profile = self.get_telegram_profile_identifiers()
            username = profile['username']
            mobile = profile['mobile']

            profile_dir = self.artifact_dir / _account / "Profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            screenshot_name = profile_dir / f'user_profile_{username}_{_account}.jpg'
            self.device.screenshot(str(screenshot_name))
            self.register_artifact(screenshot_name, kind="screenshot_profile", account=_account)
            returned = self._return_from_profile_to_chat_list()

            self.user_profiles[_account] = [_account, username, mobile]
            self.log_action(
                "collect_user_profile",
                selector=PROFILE,
                result="success" if returned else "success_return_failed",
                artifacts=[str(screenshot_name)],
            )
            logger.info("%s User Profile collected: account=%s finished_at=%s (duration=%.2fs)", self.aura_prefix(), _account, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())), time.time() - start_ts)

        except Exception as e:
            logger.warning("%s User Profile read failed: %s", self.aura_prefix(), e)
            self.log_action("collect_user_profile", selector=PROFILE, result="fail", error=e)
