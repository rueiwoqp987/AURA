import logging
import re
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from collectors.wechat.chat_list import ChatCandidate
from collectors.wechat.messages import parse_ocr_page_to_messages
from collectors.wechat.mixin_base import WeChatCollectorDeps
from utils.ocr import is_time_like

logger = logging.getLogger(__name__)


def _wechat_history_roi_boxes(width: int, height: int, ratios: tuple[tuple[float, float], ...] | None = None) -> list[tuple[int, int, int, int]]:
    ranges = ratios or ((0.20, 0.80), (0.28, 0.72), (0.36, 0.64))
    boxes = []
    for top_ratio, bottom_ratio in ranges:
        top = max(0, min(height - 1, int(height * float(top_ratio))))
        bottom = max(top + 1, min(height, int(height * float(bottom_ratio))))
        boxes.append((0, top, width, bottom))
    return boxes


def _roi_pixel_similarity(before: Image.Image, after: Image.Image, box: tuple[int, int, int, int]) -> float:
    before_crop = before.crop(box).resize((32, 32)).convert("RGB")
    after_crop = after.crop(box).resize((32, 32)).convert("RGB")
    before_bytes = before_crop.tobytes()
    after_bytes = after_crop.tobytes()
    if len(before_bytes) != len(after_bytes) or not before_bytes:
        return 0.0
    diff = sum(abs(a - b) for a, b in zip(before_bytes, after_bytes))
    return max(0.0, 1.0 - (diff / (len(before_bytes) * 255.0)))


def _wechat_history_visual_similarity(before_path: str, after_path: str) -> float:
    try:
        before = Image.open(before_path).convert("RGB")
        after = Image.open(after_path).convert("RGB")
    except Exception:
        return 0.0

    if before.size != after.size:
        after = after.resize(before.size)

    width, height = before.size
    scores = [_roi_pixel_similarity(before, after, box) for box in _wechat_history_roi_boxes(width, height)]
    return min(scores) if scores else 0.0


def _normalize_wechat_ocr_token(text: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", (text or "").lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def _wechat_meaningful_ocr_tokens(items: list, nav_blacklist: set[str] | None = None, min_conf: float = 0.45) -> set[str]:
    blacklist = {str(x).strip().lower() for x in (nav_blacklist or set())}
    tokens: set[str] = set()
    for it in items or []:
        raw = (getattr(it, "text", "") or "").strip()
        if not raw:
            continue
        if float(getattr(it, "conf", 0.0) or 0.0) < min_conf:
            continue
        if raw.lower() in blacklist or is_time_like(raw):
            continue
        normalized = _normalize_wechat_ocr_token(raw)
        if not normalized:
            continue
        for token in normalized.split():
            if len(token) < 3:
                continue
            if token in blacklist or is_time_like(token):
                continue
            tokens.add(token)
    return tokens


def _wechat_ocr_overlap(before_items: list, after_items: list, nav_blacklist: set[str] | None = None) -> float:
    before_tokens = _wechat_meaningful_ocr_tokens(before_items, nav_blacklist)
    after_tokens = _wechat_meaningful_ocr_tokens(after_items, nav_blacklist)
    if not before_tokens and not after_tokens:
        return 1.0
    if not before_tokens or not after_tokens:
        return 0.0
    return len(before_tokens & after_tokens) / max(1, min(len(before_tokens), len(after_tokens)))


def evaluate_wechat_history_stagnation(
    before_screenshot_path: str,
    after_screenshot_path: str,
    before_ocr_items: list,
    after_ocr_items: list,
    *,
    nav_blacklist: set[str] | None = None,
    visual_threshold: float = 0.985,
    ocr_overlap_threshold: float = 0.72,
) -> dict:
    visual_similarity = _wechat_history_visual_similarity(before_screenshot_path, after_screenshot_path)
    ocr_overlap = _wechat_ocr_overlap(before_ocr_items, after_ocr_items, nav_blacklist)
    stagnant = visual_similarity >= float(visual_threshold) and ocr_overlap >= float(ocr_overlap_threshold)
    return {
        "stagnant": stagnant,
        "visual_similarity": round(visual_similarity, 6),
        "ocr_overlap": round(ocr_overlap, 6),
        "visual_threshold": float(visual_threshold),
        "ocr_overlap_threshold": float(ocr_overlap_threshold),
    }


class WeChatChatroomsMixin(WeChatCollectorDeps):
    def _compute_wechat_safe_area(self, ocr_items: list | None = None) -> dict[str, int]:
        try:
            w, h = self.device.window_size()
        except Exception:
            w, h = 1080, 1920

        items = ocr_items or []
        top_bound = int(h * float(self.profile.get("wechat_safe_top_ratio", 0.08)))
        bottom_bound = int(h * float(self.profile.get("wechat_safe_bottom_ratio", 0.88)))
        left_bound = int(w * float(self.profile.get("wechat_safe_left_ratio", 0.06)))
        right_bound = int(w * float(self.profile.get("wechat_safe_right_ratio", 0.94)))

        top_markers = ("WeChat", "Search")
        bottom_markers = ("WeChat", "Contacts", "Discover", "Me")

        top_hits = [
            int(max(p[1] for p in (it.bbox or [])))
            for it in items
            if any(marker.lower() in ((it.text or "").strip().lower()) for marker in top_markers)
            and float(it.cy) <= float(h * 0.28)
            and getattr(it, "bbox", None)
        ]
        if top_hits:
            top_bound = max(top_bound, max(top_hits) + int(h * 0.01))

        bottom_hits = [
            int(min(p[1] for p in (it.bbox or [])))
            for it in items
            if any(marker.lower() == ((it.text or "").strip().lower()) for marker in bottom_markers)
            and float(it.cy) >= float(h * 0.72)
            and getattr(it, "bbox", None)
        ]
        if bottom_hits:
            bottom_bound = min(bottom_bound, min(bottom_hits) - int(h * 0.01))

        if bottom_bound <= top_bound:
            top_bound = int(h * 0.08)
            bottom_bound = int(h * 0.88)

        return {
            "left": max(0, left_bound),
            "top": max(0, top_bound),
            "right": min(w, right_bound),
            "bottom": min(h, bottom_bound),
        }

    def _ocr_current_screen_items(self) -> list:
        try:
            shot = self.device.screenshot()
            return self.ocr_image(shot)
        except Exception:
            return []

    def _tap(self, x: int, y: int) -> None:
        try:
            self.device.click(x, y)
        except Exception:
            try:
                self.device.shell(["input", "tap", str(int(x)), str(int(y))])
            except Exception:
                self.device.click(x, y)

    def _back(self) -> None:
        try:
            self.device.press("back")
        except Exception:
            pass

    def _looks_like_chat_list(self, ocr_items: list) -> bool:
        nav = set(getattr(self, "NAV_BLACKLIST", set()) or set())
        hits = {(it.text or "").strip() for it in ocr_items if (it.text or "").strip() in nav}
        return len(hits) >= 2

    def _is_wechat_chat_list_visual_state(self) -> bool:
        if not self._is_app_foreground():
            return False
        if self._is_wechat_recent_overlay():
            return False
        items = self._ocr_current_screen_items()
        if not items:
            return False
        return self._looks_like_chat_list(items)

    def _is_wechat_chatroom_visual_state(self) -> bool:
        if not self._is_app_foreground():
            return False
        if self._is_wechat_recent_overlay():
            return False
        items = self._ocr_current_screen_items()
        if not items:
            return False
        return not self._looks_like_chat_list(items)

    def _is_expected_wechat_chatroom_visual_state(self, candidate_name: str) -> bool:
        if not self._is_app_foreground():
            return False
        if self._is_wechat_recent_overlay():
            return False
        items = self._ocr_current_screen_items()
        if not items:
            return False
        if self._looks_like_chat_list(items):
            return False
        return self._chat_header_match_state(items, candidate_name) is not False

    def _chat_header_match_state(self, items, candidate_name: str) -> bool | None:
        query = (candidate_name or "").strip().lower()
        if not query:
            return None

        token = query.split(" ")[0]
        try:
            _, h = self.device.window_size()
        except Exception:
            h = 1920

        top_limit = int(h * 0.18)
        generic = {"wechat", "search", "chats", "contacts", "discover", "me"}
        texts: list[str] = []
        for it in items:
            text = (it.text or "").strip()
            if not text:
                continue
            cy = int(float(it.cy))
            if cy < 80 or cy > top_limit:
                continue
            lowered = text.lower()
            if lowered in generic:
                continue
            if re.fullmatch(r"[\d:\.\sapmAPM]+", text):
                continue
            texts.append(text)

        lowered_texts = [t.lower() for t in texts]
        if any(t == query or query in t for t in lowered_texts):
            return True
        if token and len(token) >= 2 and any(token in t for t in lowered_texts):
            return True
        if texts:
            return False
        return None

    def _wait_wechat_view_stable(self, selector: str, *, timeout_sec: float | None = None, stable_polls: int | None = None, interval_sec: float | None = None) -> bool:
        return self.wait_for_visual_stable(
            selector,
            timeout=float(timeout_sec if timeout_sec is not None else self.profile.get("wechat_view_stable_timeout_sec", 1.4)),
            stable_polls=int(stable_polls if stable_polls is not None else self.profile.get("wechat_view_stable_polls", 2)),
            interval=float(interval_sec if interval_sec is not None else self.profile.get("wechat_view_stable_interval_sec", 0.20)),
        )

    def _is_wechat_recent_overlay(self) -> bool:
        checks = [
            {"text": "Recent"},
            {"textContains": "recently used Mini Programs"},
            {"textContains": "watched live streams/videos"},
        ]
        hit = 0
        for sel in checks:
            try:
                if self.device(**sel).exists:
                    hit += 1
            except Exception:
                continue

        return hit >= 1

    def _recover_from_recent_screen(self) -> bool:
        overlay = self._is_wechat_recent_overlay()
        fg = (self._foreground_package() or "").lower()

        likely_os_recent = any(
            k in fg
            for k in (
                "systemui",
                "launcher",
                "quickstep",
                "miui.home",
                "oneui",
            )
        )

        if not overlay and self._is_app_foreground() and not likely_os_recent:
            return True

        try:
            from utils.utils import scroll_up

            scroll_up(self.device, duration=0.12)
            self.log_action(
                "recover_recent",
                selector="scroll_up_once",
                artifacts=[f"foreground={fg}", f"overlay={overlay}"],
            )
            self.wait_for_screen_state(
                "wechat_recent_recovered",
                lambda: self._is_app_foreground() and (not self._is_wechat_recent_overlay()),
                timeout=1.2,
                interval=0.2,
                capture_on_timeout=False,
            )
        except Exception as e:
            self.log_action(
                "recover_recent",
                selector="scroll_up_once",
                result="fail",
                error=e,
                artifacts=[f"foreground={fg}", f"overlay={overlay}"],
            )
            return False

        if self._is_wechat_recent_overlay():
            try:
                self.device.press("back")
                self.log_action("recover_recent", selector="press_back_fallback")
                self.wait_for_screen_state(
                    "wechat_recent_recovered",
                    lambda: self._is_app_foreground() and (not self._is_wechat_recent_overlay()),
                    timeout=1.2,
                    interval=0.2,
                    capture_on_timeout=False,
                )
            except Exception:
                pass

        return self._is_app_foreground() and (not self._is_wechat_recent_overlay())

    def _back_to_chat_list(self, *, reason: str, max_back: int = 3, timeout: float = 2.0) -> bool:
        return self.press_back_to_state(
            reason=reason,
            state_name="wechat_chat_list_screen",
            predicate=self._is_wechat_chat_list_visual_state,
            max_back=max_back,
            timeout=timeout,
            interval=0.3,
        )

    def _safe_open_chat_candidate(self, candidate: ChatCandidate, tap_x: int, tap_y: int, tap_mode: str) -> bool:
        return self.safe_click(
            f"open_chat={candidate.chat_name}",
            lambda: self._tap(tap_x, tap_y),
            expected_state_name="wechat_chatroom_screen",
            expected_predicate=lambda: self._is_expected_wechat_chatroom_visual_state(candidate.chat_name),
            timeout=float(self.profile.get("wechat_chat_open_timeout_sec", 2.5)),
            recovery_fn=self._recover_from_recent_screen,
            settle_sec=float(self.profile.get("wechat_chat_open_settle_sec", 0.5)),
        )

    def _scroll_down_safe(self) -> bool:
        try:
            w, h = self.device.window_size()
            x = int(w * 0.5)

            start_ratio = float(self.profile.get("wechat_scroll_down_start_ratio", 0.62))
            end_ratio = float(self.profile.get("wechat_scroll_down_end_ratio", 0.42))
            duration_sec = float(self.profile.get("wechat_scroll_down_duration_sec", 0.10))

            start_ratio = min(max(start_ratio, 0.45), 0.75)
            end_ratio = min(max(end_ratio, 0.20), start_ratio - 0.08)

            start_y = int(h * start_ratio)
            end_y = int(h * end_ratio)
            self.device.swipe(x, start_y, x, end_y, duration_sec)
            self.log_action(
                "swipe",
                selector="scroll_down_safe",
                artifacts=[f"start={start_ratio:.2f}", f"end={end_ratio:.2f}", f"duration={duration_sec:.2f}s"],
            )
        except Exception as e:
            self.log_action("swipe", selector="scroll_down_safe", result="fail", error=e)
            return False

        if self._is_wechat_recent_overlay():
            return self._recover_from_recent_screen()
        if not self._is_app_foreground():
            return self._recover_from_recent_screen()
        self._wait_wechat_view_stable("scroll_down_safe")
        return True

    def _scroll_chat_history_backward_safe(self) -> bool:
        try:
            w, h = self.device.window_size()
            x = int(w * 0.5)
            start_ratio = float(self.profile.get("wechat_history_scroll_start_ratio", 0.36))
            end_ratio = float(self.profile.get("wechat_history_scroll_end_ratio", 0.68))
            duration_sec = float(self.profile.get("wechat_history_scroll_duration_sec", 0.12))

            start_ratio = min(max(start_ratio, 0.22), 0.55)
            end_ratio = min(max(end_ratio, start_ratio + 0.12), 0.82)

            start_y = int(h * start_ratio)
            end_y = int(h * end_ratio)
            self.device.swipe(x, start_y, x, end_y, duration_sec)
            self.log_action(
                "swipe",
                selector="chat_history_scroll_backward_safe",
                artifacts=[f"start={start_ratio:.2f}", f"end={end_ratio:.2f}", f"duration={duration_sec:.2f}s"],
            )
        except Exception as e:
            self.log_action("swipe", selector="chat_history_scroll_backward_safe", result="fail", error=e)
            return False

        if self._is_wechat_recent_overlay():
            return self._recover_from_recent_screen()
        if not self._is_app_foreground():
            return self._recover_from_recent_screen()
        self._wait_wechat_view_stable("chat_history_scroll_backward_safe")
        return True

    def _scroll_up_safe(self) -> bool:
        try:
            w, h = self.device.window_size()
            x = int(w * 0.5)
            start_ratio = float(self.profile.get("wechat_scroll_up_start_ratio", 0.44))
            end_ratio = float(self.profile.get("wechat_scroll_up_end_ratio", 0.60))
            duration_sec = float(self.profile.get("wechat_scroll_up_duration_sec", 0.10))

            start_ratio = min(max(start_ratio, 0.25), 0.70)
            end_ratio = min(max(end_ratio, start_ratio + 0.08), 0.82)

            start_y = int(h * start_ratio)
            end_y = int(h * end_ratio)
            self.device.swipe(x, start_y, x, end_y, duration_sec)
            self.log_action(
                "swipe",
                selector="scroll_up_safe",
                artifacts=[f"start={start_ratio:.2f}", f"end={end_ratio:.2f}", f"duration={duration_sec:.2f}s"],
            )
        except Exception as e:
            self.log_action("swipe", selector="scroll_up_safe", result="fail", error=e)
            return False

        if self._is_wechat_recent_overlay():
            return self._recover_from_recent_screen()
        if not self._is_app_foreground():
            return self._recover_from_recent_screen()
        self._wait_wechat_view_stable("scroll_up_safe")
        return True

    def _go_chat_list_top(self, max_swipes: int = 3) -> None:

        try:
            same_count = 0
            prev_hash = self._screen_hash()
            for _ in range(max_swipes):
                if not self._scroll_up_safe():
                    break

                cur_hash = self._screen_hash()
                if cur_hash and prev_hash and cur_hash == prev_hash:
                    same_count += 1
                else:
                    same_count = 0

                prev_hash = cur_hash or prev_hash
                if same_count >= 1:
                    self.log_action("chat_list_top_aligned", selector="scroll_up", artifacts=["reason=stable_screen"])
                    break
        except Exception as e:
            self.log_action("chat_list_top_aligned", selector="scroll_up", result="fail", error=e)

    def _move_to_swipe_index(self, current_index: int, target_index: int) -> int:
        if target_index <= current_index:
            return current_index
        try:
            requested = target_index - current_index
            max_steps = int(self.profile.get("chat_history_reposition_max_swipes", 8))
            steps = min(requested, max_steps)

            applied = 0
            for _ in range(steps):
                if not self._scroll_down_safe():
                    break
                applied += 1

            if applied < requested:
                self.log_action(
                    "chat_history_reposition_clipped",
                    selector="scroll_down",
                    artifacts=[f"requested={requested}", f"applied={applied}", f"target={target_index}"],
                )
            return current_index + applied
        except Exception:
            return current_index

    def _resolve_chat_tap_point(self, candidate: ChatCandidate) -> tuple[int, int, str]:

        try:
            shot = self.device.screenshot()
            items = self.ocr_image(shot)
            safe_area = self._compute_wechat_safe_area(items)

            query = (candidate.chat_name or "").strip().lower()
            token = query.split(" ")[0] if query else ""

            matches = [
                it for it in items
                if (it.text or "").strip().lower() == query
                and safe_area["top"] <= int(float(it.cy)) <= safe_area["bottom"]
            ]
            mode = "exact"
            if not matches and token and len(token) >= 2:
                matches = [
                    it for it in items
                    if token in (it.text or "").strip().lower()
                    and safe_area["top"] <= int(float(it.cy)) <= safe_area["bottom"]
                ]
                mode = "contains"

            if matches:
                best = min(matches, key=lambda it: abs(float(it.cy) - float(candidate.cy)))

                left = min(int(p[0]) for p in best.bbox)
                right = max(int(p[0]) for p in best.bbox)
                width = max(1, right - left)
                x = left + max(28, min(56, int(width * 0.22)))
                x = max(safe_area["left"], min(x, safe_area["right"]))
                y = max(safe_area["top"], min(int(best.cy), safe_area["bottom"]))
                return x, y, mode
        except Exception:
            pass

        try:
            safe_area = self._compute_wechat_safe_area()
        except Exception:
            safe_area = {"left": 0, "top": 0, "right": 1080, "bottom": 1920}

        try:
            left = min(int(p[0]) for p in candidate.name_bbox)
            right = max(int(p[0]) for p in candidate.name_bbox)
            width = max(1, right - left)
            x = left + max(28, min(56, int(width * 0.22)))
        except Exception:
            x = candidate.cx
        x = max(safe_area["left"], min(x, safe_area["right"]))
        y = max(safe_area["top"], min(int(candidate.cy), safe_area["bottom"]))
        return x, y, "fallback"

    def _retry_open_chat(self, candidate: ChatCandidate) -> bool:

        try:
            node = self.device(text=candidate.chat_name)
            if node.exists:
                if self.safe_click(
                    f"chat_open_retry=text_exact:{candidate.chat_name}",
                    lambda: node.click(),
                    expected_state_name="wechat_chatroom_screen",
                    expected_predicate=lambda: self._is_expected_wechat_chatroom_visual_state(candidate.chat_name),
                    timeout=float(self.profile.get("wechat_chat_open_timeout_sec", 2.5)),
                    recovery_fn=self._recover_from_recent_screen,
                    settle_sec=0.4,
                ):
                    self.log_action("chat_open_retry", selector="text_exact", artifacts=[candidate.chat_name])
                    return True
        except Exception:
            pass

        token = (candidate.chat_name or "").strip().split(" ")[0]
        if token and len(token) >= 2:
            try:
                node = self.device(textContains=token)
                if node.exists:
                    if self.safe_click(
                        f"chat_open_retry=text_contains:{token}",
                        lambda: node.click(),
                        expected_state_name="wechat_chatroom_screen",
                        expected_predicate=lambda: self._is_expected_wechat_chatroom_visual_state(candidate.chat_name),
                        timeout=float(self.profile.get("wechat_chat_open_timeout_sec", 2.5)),
                        recovery_fn=self._recover_from_recent_screen,
                        settle_sec=0.4,
                    ):
                        self.log_action("chat_open_retry", selector="text_contains", artifacts=[token])
                        return True
            except Exception:
                pass

        try:
            rx, ry, mode = self._resolve_chat_tap_point(candidate)
            if self.safe_click(
                f"chat_open_retry=tap_repeat:{candidate.chat_name}",
                lambda: self._tap(rx, ry),
                expected_state_name="wechat_chatroom_screen",
                expected_predicate=lambda: self._is_expected_wechat_chatroom_visual_state(candidate.chat_name),
                timeout=float(self.profile.get("wechat_chat_open_timeout_sec", 2.5)),
                recovery_fn=self._recover_from_recent_screen,
                settle_sec=0.4,
            ):
                self.log_action("chat_open_retry", selector="tap_repeat", artifacts=[f"tap={rx},{ry}", f"mode={mode}"])
                return True
        except Exception:
            return False
        self.log_action("chat_open_retry", selector=candidate.chat_name, result="fail")
        return False

    def _collect_chat_history_for_candidate(self, candidate: ChatCandidate, phase_label: str) -> dict:
        self.current_chat_id = candidate.chat_id
        chat_dir = Path(self.artifact_dir) / "Chat History" / f"{candidate.chat_name}_{candidate.chat_id}"
        pages_dir = chat_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        started_at = datetime.now().astimezone().isoformat()
        start_ts = time.time()

        logger.info(
            "%s Chat open start: chat=%s chat_id=%s",
            self.aura_prefix(phase_label),
            candidate.chat_name,
            candidate.chat_id,
        )
        self.log_action(
            "chat_open_start",
            selector=candidate.chat_name,
            artifacts=[candidate.chat_id, f"swipe_index={candidate.swipe_index}", f"tap={candidate.cx},{candidate.cy}"],
        )

        if not self._is_app_foreground():
            fg = self._foreground_package()
            logger.error("%s Chat open aborted: app_not_foreground fg=%s", self.aura_prefix(phase_label), fg)
            self.log_action("chat_open_abort", selector=candidate.chat_name, result="fail", artifacts=[candidate.chat_id, f"fg={fg}"])
            return {
                "status": "failed",
                "reason": "app_not_foreground",
                "chat_id": candidate.chat_id,
                "chat_name": candidate.chat_name,
                "pages": 0,
                "messages": 0,
                "started_at": started_at,
                "ended_at": datetime.now().astimezone().isoformat(),
                "duration_sec": round(time.time() - start_ts, 3),
            }

        tap_x, tap_y, tap_mode = self._resolve_chat_tap_point(candidate)
        self.log_action("chat_open_target_adjust", selector=candidate.chat_name, artifacts=[f"tap={tap_x},{tap_y}", f"mode={tap_mode}"])
        opened = self._safe_open_chat_candidate(candidate, tap_x, tap_y, tap_mode)
        self.log_screen_transition(
            "wechat_chatroom_open",
            result="success" if opened else "unconfirmed",
            artifacts=[candidate.chat_id, f"mode={tap_mode}"],
        )

        max_pages = int(self.profile.get("chat_history_max_pages", 300))
        stagnant = 0
        stagnant_stop_count = int(self.profile.get("chat_history_stagnant_stop_count", 2))
        visual_threshold = float(self.profile.get("chat_history_visual_stagnant_threshold", 0.985))
        ocr_overlap_threshold = float(self.profile.get("chat_history_ocr_overlap_threshold", 0.72))
        previous_screenshot_path = None
        previous_ocr_items = None
        ingested = 0
        captured_pages = 0

        for page_idx in range(max_pages):
            if not self._is_app_foreground():
                fg = self._foreground_package()
                logger.error("%s Chat page aborted: app_not_foreground chat=%s fg=%s", self.aura_prefix(phase_label), candidate.chat_name, fg)
                self.log_action("chat_page_abort", selector=candidate.chat_name, result="fail", artifacts=[candidate.chat_id, f"fg={fg}", f"page={page_idx}"])
                return {
                    "status": "failed",
                    "reason": "app_not_foreground",
                    "chat_id": candidate.chat_id,
                    "chat_name": candidate.chat_name,
                    "pages": captured_pages,
                    "messages": ingested,
                    "started_at": started_at,
                    "ended_at": datetime.now().astimezone().isoformat(),
                    "duration_sec": round(time.time() - start_ts, 3),
                }

            shot = pages_dir / f"page_{page_idx:03d}.jpg"
            ev = self.capture_visual_evidence(
                shot,
                screenshot_kind="screenshot_chat_page",
                uitree_kind=None,
                account=self.current_account,
                chat_id=self.current_chat_id,
                dump_uitree=False,
            )

            screenshot_path = ev.get("screenshot_path")
            uitree_path = None
            items = self.ocr_image(screenshot_path)

            if page_idx == 0:
                header_match = self._chat_header_match_state(items, candidate.chat_name)
                wrong_chat_opened = header_match is False
                if self._looks_like_chat_list(items) or wrong_chat_opened:
                    reason = "chat_list_detected" if self._looks_like_chat_list(items) else "header_mismatch"
                    header_texts = []
                    if wrong_chat_opened:
                        header_texts = [
                            (it.text or "").strip()
                            for it in items
                            if 80 <= int(float(it.cy)) <= 240 and (it.text or "").strip()
                        ][:4]
                    self.log_action(
                        "chat_open_verify",
                        selector=candidate.chat_name,
                        result="fail",
                        artifacts=[candidate.chat_id, f"reason={reason}"] + ([f"header={header_texts}"] if header_texts else []),
                    )

                    self._back_to_chat_list(reason=f"chat_open_verify:{candidate.chat_name}")
                    retried = self._retry_open_chat(candidate)
                    if retried:
                        retry_shot = pages_dir / f"page_{page_idx:03d}_retry.jpg"
                        retry_ev = self.capture_visual_evidence(
                            retry_shot,
                            screenshot_kind="screenshot_chat_page",
                            uitree_kind=None,
                            account=self.current_account,
                            chat_id=self.current_chat_id,
                            dump_uitree=False,
                        )
                        screenshot_path = retry_ev.get("screenshot_path")
                        items = self.ocr_image(screenshot_path)
                        retry_header_match = self._chat_header_match_state(items, candidate.chat_name)
                        if (not self._looks_like_chat_list(items)) and retry_header_match is not False:
                            self.log_action("chat_open_verify_retry", selector=candidate.chat_name, artifacts=[candidate.chat_id, "result=entered"])
                        else:
                            retry_reason = "still_chat_list" if self._looks_like_chat_list(items) else "still_header_mismatch"
                            self.log_action("chat_open_verify_retry", selector=candidate.chat_name, result="fail", artifacts=[candidate.chat_id, f"reason={retry_reason}"])
                            self._back_to_chat_list(reason=f"chat_open_retry_failed:{candidate.chat_name}")
                            return {
                                "status": "failed",
                                "reason": "chat_not_opened",
                                "chat_id": candidate.chat_id,
                                "chat_name": candidate.chat_name,
                                "pages": 0,
                                "messages": 0,
                                "started_at": started_at,
                                "ended_at": datetime.now().astimezone().isoformat(),
                                "duration_sec": round(time.time() - start_ts, 3),
                            }
                    else:
                        self._back_to_chat_list(reason=f"chat_open_failed:{candidate.chat_name}")
                        return {
                            "status": "failed",
                            "reason": "chat_not_opened",
                            "chat_id": candidate.chat_id,
                            "chat_name": candidate.chat_name,
                            "pages": 0,
                            "messages": 0,
                            "started_at": started_at,
                            "ended_at": datetime.now().astimezone().isoformat(),
                            "duration_sec": round(time.time() - start_ts, 3),
                        }

            ocr_json_path = self.write_ocr_artifact(
                screenshot_path,
                items,
                kind="ocr_chat_page",
                meta={
                    "phase": phase_label,
                    "chat_name": candidate.chat_name,
                    "chat_id": candidate.chat_id,
                    "page_index": page_idx,
                },
            )

            msgs = parse_ocr_page_to_messages(
                items,
                app=self.app_id,
                phase=self.current_phase or "",
                account=self.current_account or "default",
                chat_id=self.current_chat_id,
                screenshot_path=screenshot_path,
                uitree_path=uitree_path,
                ocr_json_path=ocr_json_path,
                nav_blacklist=set(getattr(self, "NAV_BLACKLIST", set()) or set()),
                page_index=page_idx,
            )

            if msgs:
                self.storage.upsert_messages(
                    self.app_id,
                    self.current_account or "default",
                    self.current_chat_id,
                    msgs,
                    phase=self.current_phase or "",
                    chat_name=candidate.chat_name,
                    chat_type="Unknown",
                )
                ingested += len(msgs)

            captured_pages += 1
            self.log_action(
                "chat_page",
                selector=candidate.chat_name,
                artifacts=[
                    f"page={page_idx}",
                    f"ocr_items={len(items)}",
                    f"messages={len(msgs)}",
                    screenshot_path,
                    ocr_json_path or "",
                ],
            )

            if previous_screenshot_path and previous_ocr_items is not None:
                stop_eval = evaluate_wechat_history_stagnation(
                    previous_screenshot_path,
                    screenshot_path,
                    previous_ocr_items,
                    items,
                    nav_blacklist=set(getattr(self, "NAV_BLACKLIST", set()) or set()),
                    visual_threshold=visual_threshold,
                    ocr_overlap_threshold=ocr_overlap_threshold,
                )
                if stop_eval.get("stagnant"):
                    stagnant += 1
                else:
                    stagnant = 0
                self.log_action(
                    "chat_history_stagnation_check",
                    selector=candidate.chat_name,
                    artifacts=[
                        f"page={page_idx}",
                        f"stagnant={stagnant}",
                        f"required={stagnant_stop_count}",
                        f"visual={stop_eval.get('visual_similarity')}",
                        f"ocr_overlap={stop_eval.get('ocr_overlap')}",
                    ],
                )

                if stagnant >= max(1, stagnant_stop_count):
                    self.log_action(
                        "chat_history_stop",
                        selector=candidate.chat_name,
                        artifacts=[
                            candidate.chat_id,
                            f"reason=visual_ocr_stagnant",
                            f"page={page_idx}",
                            f"stagnant={stagnant}",
                        ],
                    )
                    break

            previous_screenshot_path = screenshot_path
            previous_ocr_items = items

            if page_idx >= max_pages - 1:
                self.log_action(
                    "chat_history_stop",
                    selector=candidate.chat_name,
                    result="safety_cap",
                    artifacts=[candidate.chat_id, f"max_pages={max_pages}"],
                )
                break

            self._scroll_chat_history_backward_safe()

        self._back_to_chat_list(reason=f"chat_done:{candidate.chat_name}")

        duration = round(time.time() - start_ts, 3)
        logger.info(
            "%s Chat collected: chat=%s chat_id=%s (pages=%d, messages=%d, duration=%.2fs)",
            self.aura_prefix(phase_label),
            candidate.chat_name,
            candidate.chat_id,
            captured_pages,
            ingested,
            duration,
        )
        self.log_action(
            "chat_done",
            selector=candidate.chat_name,
            artifacts=[
                candidate.chat_id,
                f"pages={captured_pages}",
                f"messages={ingested}",
                f"duration={duration:.2f}s",
            ],
        )

        return {
            "chat_id": candidate.chat_id,
            "chat_name": candidate.chat_name,
            "pages": captured_pages,
            "messages": ingested,
            "started_at": started_at,
            "ended_at": datetime.now().astimezone().isoformat(),
            "duration_sec": duration,
        }

    def collect_chatrooms_and_messages(self, candidates: list[ChatCandidate], phase_label: str) -> dict:
        logger.info("%s Chat history collection start (count=%d)", self.aura_prefix(phase_label), len(candidates))
        self.log_action("chat_history_start", selector=phase_label, artifacts=[f"count={len(candidates)}"])

        max_chats = int(self.profile.get("chat_history_max_chats", 50))
        targets = sorted(candidates[:max_chats], key=lambda c: (c.swipe_index, c.cy, c.cx))

        self._go_chat_list_top(max_swipes=int(self.profile.get("chat_list_top_reset_max_swipes", 3)))
        if self._scroll_down_safe():
            self.log_action("chat_history_reposition_prime", selector="scroll_down_after_top", artifacts=["applied=1"])
            current_swipe_index = 1
        else:
            self.log_action("chat_history_reposition_prime", selector="scroll_down_after_top", result="fail")
            current_swipe_index = 0

        results = []
        processed_ids: set[str] = set()

        for c in targets:
            if c.chat_id in processed_ids:
                continue

            if not self._is_app_foreground():
                fg = self._foreground_package()
                logger.error("%s Chat history aborted: app_not_foreground fg=%s", self.aura_prefix(phase_label), fg)
                self.log_action("chat_history_abort", selector=phase_label, result="fail", artifacts=[f"fg={fg}", f"processed={len(results)}"])
                return {"status": "failed", "reason": "app_not_foreground", "chats": results}

            target_index = c.swipe_index + (1 if current_swipe_index > 0 else 0)
            current_swipe_index = self._move_to_swipe_index(current_swipe_index, target_index)

            try:
                chat_ret = self._collect_chat_history_for_candidate(c, phase_label)
                results.append(chat_ret)
                processed_ids.add(c.chat_id)

                if chat_ret.get("status") == "failed":
                    logger.warning(
                        "%s Chat open/collect failed: chat=%s reason=%s",
                        self.aura_prefix(phase_label),
                        c.chat_name,
                        chat_ret.get("reason", "failed"),
                    )

            except Exception as e:
                logger.warning("%s Chat collect failed: chat=%s err=%s", self.aura_prefix(phase_label), c.chat_name, e)
                self.log_action("chat_failed", selector=c.chat_name, result="fail", error=e, artifacts=[c.chat_id])
                try:
                    self._back_to_chat_list(reason=f"chat_failed:{c.chat_name}")
                except Exception:
                    pass

        self.log_action("chat_history_done", selector=phase_label, artifacts=[f"count={len(results)}"])
        logger.info("%s Chat history collection done (count=%d)", self.aura_prefix(phase_label), len(results))
        return {"status": "done", "chats": results}
