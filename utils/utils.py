import time
import re
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
import adbutils

dnd_type_detector = ["Your phone won't"]
_ADB_SERIAL = None
_AUDIT_SEQ_BY_PATH = {}
_AUDIT_SEQ_LOCK = threading.Lock()
_DND_HIDE_ALL_VERIFIED = set()
_DND_HIDE_ALL_INITIAL = {}

def set_adb_serial(serial=None):
    global _ADB_SERIAL
    _ADB_SERIAL = serial

def get_adb_device(serial=None):
    resolved = serial if serial is not None else _ADB_SERIAL
    if resolved:
        return adbutils.adb.device(serial=resolved)
    return adbutils.adb.device()

def _audit_log_key(log_path):
    try:
        return str(Path(log_path).resolve())
    except Exception:
        return str(log_path)

def get_audit_max_seq(log_path):
    if not log_path:
        return 0
    key = _audit_log_key(log_path)
    with _AUDIT_SEQ_LOCK:
        if key in _AUDIT_SEQ_BY_PATH:
            return _AUDIT_SEQ_BY_PATH[key]

        max_seq = 0
        try:
            log_file = Path(log_path)
            if log_file.exists():
                with log_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            value = json.loads(line).get("seq")
                            if isinstance(value, int) and value > max_seq:
                                max_seq = value
                        except Exception:
                            continue
        except Exception:
            max_seq = 0

        _AUDIT_SEQ_BY_PATH[key] = max_seq
        return max_seq

def _resolve_audit_seq(log_path, seq):
    key = _audit_log_key(log_path)
    with _AUDIT_SEQ_LOCK:
        if key not in _AUDIT_SEQ_BY_PATH:
            max_seq = 0
            try:
                log_file = Path(log_path)
                if log_file.exists():
                    with log_file.open("r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                value = json.loads(line).get("seq")
                                if isinstance(value, int) and value > max_seq:
                                    max_seq = value
                            except Exception:
                                continue
            except Exception:
                max_seq = 0
            _AUDIT_SEQ_BY_PATH[key] = max_seq

        if isinstance(seq, int) and seq > _AUDIT_SEQ_BY_PATH[key]:
            _AUDIT_SEQ_BY_PATH[key] = seq
            return seq

        _AUDIT_SEQ_BY_PATH[key] += 1
        return _AUDIT_SEQ_BY_PATH[key]

def write_audit(
    log_path,
    package_name,
    action,
    selector=None,
    result="success",
    error=None,
    artifacts=None,
    side_effect=None,
    seq=None,
    run_id=None,
    phase=None,
    account=None,
    chat_id=None,
    source_func=None,
    source_class=None,
):
    if not log_path:
        return
    seq = _resolve_audit_seq(log_path, seq)
    entry = {
        "ts": time.time(),
        "seq": seq,
        "run_id": run_id,
        "phase": phase,
        "account": account,
        "chat_id": chat_id,
        "source_class": source_class,
        "source_func": source_func,
        "app": package_name,
        "action": action,
        "selector": selector,
        "result": result,
        "error": str(error) if error else None,
        "artifacts": artifacts or [],
        "side_effect_hint": side_effect,
    }
    try:
        log_file = Path(log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def init_tool(device, audit_log_path=None, package_name="system", serial=None, run_id=None, phase="setup"):
    pkg = package_name or "system"
    set_adb_serial(serial)
    def audit(action, selector=None, result="success", error=None, artifacts=None):
        write_audit(
            audit_log_path,
            pkg,
            action,
            selector=selector,
            result=result,
            error=error,
            artifacts=artifacts,
            run_id=run_id,
            phase=phase,
        )

    audit("unlock")

    device.unlock()
    time.sleep(0.5)
    device.press('recent')
    audit("press", selector="recent")
    time.sleep(1)
    if device(text="Close all").exists:
        device(text="Close all").click()
        audit("clear_recent", selector="Close all")
    else:
        device.press('home')
        audit("press", selector="home")

    try:
        dnd = check_dnd_mode(serial=serial)
        airplane = check_airplane_mode(serial=serial)

        if dnd == '0':
            ret = ensure_dnd_mode(device, target_mode="1", serial=serial, timeout_sec=8.0, reapply_if_target=True)
            try:
                write_audit(
                    audit_log_path,
                    pkg,
                    action="toggle_dnd_mode",
                    selector="settings_ui",
                    result="success" if ret.get("ok") else "fail",
                    artifacts=[
                        f"target={ret.get('target')}",
                        f"current={ret.get('current')}",
                        f"method={ret.get('method')}",
                        f"hide_all_verified={ret.get('hide_all_verified')}",
                    ],
                    run_id=run_id,
                    phase=phase,
                )
            except Exception:
                pass
            audit("toggle_dnd_mode", selector="settings", artifacts=["on"])
        if airplane == '0':
            toggle_airplane_mode(True, serial=serial)
            audit("toggle_airplane_mode", selector="adb", artifacts=["on"])
    except Exception as e:
        audit("init_tool", result="fail", error=e)

def is_allowed_text(list, prefix, text: str) -> bool:
    lower_text = text.lower()

    if any(lower_text.startswith(p) for p in prefix):
        return False

    if text in list:
        return False

    return True

def get_device_time(serial=None):
    d = get_adb_device(serial)
    return d.shell("date +%s").strip()

def get_last_N_file(path, N=1, serial=None):
    d = get_adb_device(serial)
    return d.shell(f"ls -t {path} | head -n {N}")

def get_file_last_modified_time(file_path, serial=None):
    d = get_adb_device(serial)
    return d.shell(f"stat -c %Y '{file_path}'")

def download_file(file_path, download_path, serial=None):
    d = get_adb_device(serial)
    return d.sync.pull(file_path, download_path)

def get_file_size(file_path, serial=None):
    d = get_adb_device(serial)
    return d.shell(f"stat -c %s '{file_path}'")

def check_dnd_mode(serial=None):
    d = get_adb_device(serial)
    return d.shell("settings get global zen_mode").strip()

def check_airplane_mode(serial=None):
    d = get_adb_device(serial)
    return d.shell("settings get global airplane_mode_on").strip()

def toggle_airplane_mode(flag, serial=None):
    d = get_adb_device(serial)
    if flag:
        d.shell("settings put global airplane_mode_on 1")
    else:
        d.shell("settings put global airplane_mode_on 0")

def _apply_dnd_mode_via_settings(device, target_mode, serial=None, timeout_sec=8.0, restore_hide_all: bool = False):
    """Apply DND via Settings UI and honor target_mode ('1' on / '0' off)."""
    target_mode = str(target_mode)
    dnd_cache_key = serial if serial else "__default__"

    def _parse_bounds(raw: str | None):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
        if not m:
            return None
        return [int(v) for v in m.groups()]

    def _click_bounds_center(bounds) -> bool:
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            return False
        left, top, right, bottom = bounds
        try:
            device.click((int(left) + int(right)) // 2, (int(top) + int(bottom)) // 2)
            return True
        except Exception:
            return False

    def _tap_text_from_xml(*, exact_labels=(), contains_tokens=(), regex_patterns=()) -> bool:
        try:
            root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return False

        parent_map = {}
        for parent in root.iter():
            for child in list(parent):
                parent_map[child] = parent

        exact_set = {str(v).strip().lower() for v in exact_labels if str(v or "").strip()}
        contains_set = [str(v).strip().lower() for v in contains_tokens if str(v or "").strip()]
        compiled_patterns = []
        for pattern in regex_patterns:
            try:
                compiled_patterns.append(re.compile(pattern))
            except Exception:
                continue

        candidates = []

        for node in root.iter():
            text = (node.attrib.get("text") or "").strip()
            if not text:
                continue

            lowered = text.lower()
            matched = lowered in exact_set
            if not matched and contains_set:
                matched = any(token in lowered for token in contains_set)
            if not matched and compiled_patterns:
                matched = any(p.search(text) for p in compiled_patterns)
            if not matched:
                continue

            chosen_bounds = None
            chosen_rank = None
            candidate = node
            depth = 0
            while candidate is not None:
                candidate_bounds = _parse_bounds(candidate.attrib.get("bounds"))
                if candidate_bounds:
                    clickable = str(candidate.attrib.get("clickable", "")).lower() == "true"
                    focusable = str(candidate.attrib.get("focusable", "")).lower() == "true"
                    class_name = str(candidate.attrib.get("class", ""))
                    is_row_container = class_name in {
                        "android.widget.LinearLayout",
                        "android.widget.RelativeLayout",
                        "android.widget.FrameLayout",
                    }
                    if clickable or focusable or is_row_container:
                        width = max(0, candidate_bounds[2] - candidate_bounds[0])
                        height = max(0, candidate_bounds[3] - candidate_bounds[1])
                        area = width * height
                        rank = (
                            0 if clickable else 1,
                            0 if focusable else 1,
                            0 if is_row_container else 1,
                            depth,
                            area,
                        )
                        if chosen_rank is None or rank < chosen_rank:
                            chosen_rank = rank
                            chosen_bounds = candidate_bounds
                candidate = parent_map.get(candidate)
                depth += 1

            if chosen_bounds is None:
                chosen_bounds = _parse_bounds(node.attrib.get("bounds"))
                if chosen_bounds:
                    width = max(0, chosen_bounds[2] - chosen_bounds[0])
                    height = max(0, chosen_bounds[3] - chosen_bounds[1])
                    area = width * height
                    chosen_rank = (2, 2, 2, 999, area)

            if chosen_bounds and chosen_rank is not None:
                candidates.append((chosen_rank, chosen_bounds))

        seen_bounds = set()
        for _, bounds in sorted(candidates, key=lambda item: item[0]):
            bounds_key = tuple(bounds)
            if bounds_key in seen_bounds:
                continue
            seen_bounds.add(bounds_key)
            if _click_bounds_center(bounds):
                return True

        return False

    def _tap_visible_text(*labels) -> bool:
        for label in labels:
            if not label:
                continue
            try:
                node = device(text=label)
                if node.exists:
                    node.click()
                    return True
            except Exception:
                continue
        return False

    def _tap_visible_text_matches(*patterns) -> bool:
        for pattern in patterns:
            if not pattern:
                continue
            try:
                node = device(textMatches=pattern)
                if node.exists:
                    node.click()
                    return True
            except Exception:
                continue
        return False

    def _tap_visible_text_contains(*tokens) -> bool:
        for token in tokens:
            if not token:
                continue
            try:
                node = device(textContains=token)
                if node.exists:
                    node.click()
                    return True
            except Exception:
                continue
        return False

    def _find_and_tap_settings_row(*, exact_labels=(), contains_tokens=(), regex_patterns=(), max_swipes=4) -> bool:
        for _ in range(max(1, int(max_swipes))):
            if _tap_visible_text(*exact_labels):
                return True
            if _tap_visible_text_matches(*regex_patterns):
                return True
            if _tap_visible_text_contains(*contains_tokens):
                return True
            if _tap_text_from_xml(
                exact_labels=exact_labels,
                contains_tokens=contains_tokens,
                regex_patterns=regex_patterns,
            ):
                return True
            try:
                device(scrollable=True).scroll.vert.forward(steps=20)
                time.sleep(0.4)
            except Exception:
                break
        return False

    def _coerce_checked(value):
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        lowered = str(value).strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return None

    def _find_main_dnd_switch_bounds():
        try:
            root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return None

        def cls(n):
            return n.attrib.get("class", "")

        def rid(n):
            return n.attrib.get("resource-id", "")

        def desc(n):
            return (n.attrib.get("content-desc") or "").strip()

        for node in root.iter():
            if cls(node) != "android.widget.Switch":
                continue
            if rid(node) != "android:id/switch_widget":
                continue
            if desc(node):
                continue
            return _parse_bounds(node.attrib.get("bounds"))

        return None

    def _set_do_not_disturb_enabled(enable: bool) -> bool:
        try:
            switches = device.xpath('//android.widget.Switch[@resource-id="android:id/switch_widget"]').all()
        except Exception:
            switches = []

        for sw in switches:
            try:
                info = sw.info or {}
            except Exception:
                info = {}

            desc = (
                info.get("contentDescription")
                or info.get("content-desc")
                or ""
            ).strip()
            checked = _coerce_checked(info.get("checked"))

            # Exclude schedule switches such as "Sleeping".
            if desc:
                continue

            if enable and checked is False:
                sw.click()
                time.sleep(1.0)
                return True
            if enable and checked is True:
                return True
            if (not enable) and checked is True:
                sw.click()
                time.sleep(1.0)
                return True
            if (not enable) and checked is False:
                return True

        bounds = _find_main_dnd_switch_bounds()
        if bounds and _click_bounds_center(bounds):
            time.sleep(1.0)
            return True

        if switches:
            try:
                info = switches[0].info or {}
            except Exception:
                info = {}

            checked = _coerce_checked(info.get("checked"))

            if enable and checked is False:
                switches[0].click()
                time.sleep(1.0)
                return True
            if enable and checked is True:
                return True
            if (not enable) and checked is True:
                switches[0].click()
                time.sleep(1.0)
                return True
            if (not enable) and checked is False:
                return True

        return False

    def _is_target(current: str) -> bool:
        cur = str(current).strip()
        if target_mode == "1":
            return cur not in {"", "0", "null", "None"}
        return cur == "0"

    def _looks_like_dnd_settings_screen() -> bool:
        try:
            if device(text="Do not disturb").exists or device(text="Do Not Disturb").exists:
                return True
        except Exception:
            pass
        try:
            return device.xpath('//android.widget.Switch[@resource-id="android:id/switch_widget"]').exists
        except Exception:
            return False

    def _open_dnd_settings_direct() -> bool:
        commands = (
            "am start -a android.settings.ZEN_MODE_SETTINGS",
            "am start -a android.settings.ZEN_MODE_SETTINGS -f 0x10008000",
        )
        bad_tokens = ("error", "exception", "permission denial", "not found", "unable", "denied")

        try:
            device.app_stop('com.android.settings')
            time.sleep(0.4)
        except Exception:
            pass

        for cmd in commands:
            try:
                out = (get_adb_device(serial).shell(cmd) or "").strip()
                lower_out = out.lower()
                if any(token in lower_out for token in bad_tokens):
                    continue
                time.sleep(0.8)
                if _looks_like_dnd_settings_screen():
                    return True
            except Exception:
                continue
        return False

    def _find_switch_for_settings_label(*, exact_labels=(), contains_tokens=()):
        try:
            root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return None, None

        parent_map = {}
        for parent in root.iter():
            for child in list(parent):
                parent_map[child] = parent

        exact_set = {str(v).strip().lower() for v in exact_labels if str(v or "").strip()}
        contains_set = [str(v).strip().lower() for v in contains_tokens if str(v or "").strip()]

        def _matches(text: str) -> bool:
            lowered = (text or "").strip().lower()
            if not lowered:
                return False
            if lowered in exact_set:
                return True
            return any(token in lowered for token in contains_set)

        def _extract_switch(node):
            state_checked = None
            click_bounds = None
            for candidate in node.iter():
                candidate_class = candidate.attrib.get("class", "")
                candidate_rid = candidate.attrib.get("resource-id", "")
                if candidate_rid.endswith("switch_background"):
                    click_bounds = _parse_bounds(candidate.attrib.get("bounds"))
                    continue
                if candidate_class != "android.widget.Switch" and not candidate_rid.endswith("switch_widget"):
                    continue
                state_checked = _coerce_checked(candidate.attrib.get("checked"))
                state_bounds = _parse_bounds(candidate.attrib.get("bounds"))
                if click_bounds is None:
                    click_bounds = state_bounds
                return state_checked, click_bounds
            return None, click_bounds

        for node in root.iter():
            node_text = (node.attrib.get("text") or "").strip()
            if not _matches(node_text):
                continue

            current = node
            for _ in range(5):
                checked, bounds = _extract_switch(current)
                if bounds is not None:
                    return checked, bounds
                current = parent_map.get(current)
                if current is None:
                    break

        return None, None

    def _read_hide_all_top_switch():
        try:
            root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return None

        parent_map = {}
        for parent in root.iter():
            for child in list(parent):
                parent_map[child] = parent

        switch_candidates = []

        for node in root.iter():
            rid = node.attrib.get("resource-id", "")
            if not rid.endswith("switch_widget"):
                continue
            bounds = _parse_bounds(node.attrib.get("bounds"))
            checked = _coerce_checked(node.attrib.get("checked"))
            if bounds is None:
                continue

            click_bounds = bounds
            text_bounds = None
            text_value = ""
            row_bounds = None
            current = node
            for _ in range(6):
                current = parent_map.get(current)
                if current is None:
                    break
                current_rid = current.attrib.get("resource-id", "")
                candidate_bounds = _parse_bounds(current.attrib.get("bounds"))
                if candidate_bounds is not None and row_bounds is None:
                    row_bounds = candidate_bounds
                if current_rid.endswith("switch_background"):
                    if candidate_bounds is not None:
                        click_bounds = candidate_bounds
                for child in list(current):
                    child_rid = child.attrib.get("resource-id", "")
                    if child_rid.endswith("switch_text"):
                        text_value = (child.attrib.get("text") or "").strip()
                        text_candidate = _parse_bounds(child.attrib.get("bounds"))
                        if text_candidate is not None:
                            text_bounds = text_candidate

            if text_value.lower() != "hide all":
                continue

            switch_candidates.append(
                {
                    "checked": checked,
                    "bounds": bounds,
                    "click_bounds": click_bounds,
                    "text_bounds": text_bounds,
                    "text": text_value,
                    "row_bounds": row_bounds,
                    "top": bounds[1],
                    "left": bounds[0],
                }
            )

        if not switch_candidates:
            return None

        switch_candidates.sort(key=lambda item: (item["top"], item["left"]))
        return switch_candidates[0]

    def _looks_like_hide_notifications_screen() -> bool:
        try:
            root = ET.fromstring(device.dump_hierarchy())
        except Exception:
            return False

        has_top_title = False
        has_hide_all_switch_text = False
        for node in root.iter():
            text = (node.attrib.get("text") or "").strip()
            rid = node.attrib.get("resource-id", "")
            bounds = _parse_bounds(node.attrib.get("bounds"))
            if text == "Hide notifications" and bounds and bounds[1] <= 360:
                has_top_title = True
            if text == "Hide all" and rid.endswith("switch_text"):
                has_hide_all_switch_text = True
        return has_top_title and has_hide_all_switch_text

    def _wait_for_hide_notifications_screen(timeout_sec: float = 2.0) -> bool:
        deadline = time.time() + max(0.5, float(timeout_sec))
        while time.time() < deadline:
            if _looks_like_hide_notifications_screen():
                return True
            time.sleep(0.2)
        return _looks_like_hide_notifications_screen()

    def _configure_hide_notifications(target_enabled: bool | None = True) -> dict:
        debug = []

        def _switch_debug(top_switch):
            if not top_switch:
                return None
            return {
                "checked": top_switch.get("checked"),
                "text": top_switch.get("text"),
                "bounds": top_switch.get("bounds"),
                "text_bounds": top_switch.get("text_bounds"),
                "row_bounds": top_switch.get("row_bounds"),
                "click_bounds": top_switch.get("click_bounds"),
            }

        already_on_screen = _looks_like_hide_notifications_screen()
        tapped = already_on_screen or _find_and_tap_settings_row(
            exact_labels=('Hide notifications',),
            contains_tokens=('Hide notifications',),
            max_swipes=3,
        )
        debug.append(
            {
                "event": "tap_hide_notifications_row",
                "ok": bool(tapped),
                "already_on_screen": bool(already_on_screen),
            }
        )
        if tapped:
            entered = _wait_for_hide_notifications_screen(timeout_sec=2.0)
            debug.append({"event": "hide_notifications_screen", "ok": bool(entered)})
            if not entered:
                return {"verified": False, "checked": None, "debug": debug}

            try:
                top_switch = _read_hide_all_top_switch()
                debug.append({"event": "top_switch_before", "switch": _switch_debug(top_switch)})
                if target_enabled is not None and top_switch and top_switch.get("checked") is not bool(target_enabled):
                    click_candidates = []
                    for key in ("text_bounds", "row_bounds", "click_bounds", "bounds"):
                        candidate = top_switch.get(key)
                        if isinstance(candidate, (list, tuple)) and len(candidate) == 4:
                            if tuple(candidate) not in {tuple(v) for v in click_candidates}:
                                click_candidates.append(candidate)
                    for index, switch_bounds in enumerate(click_candidates[:4]):
                        clicked = _click_bounds_center(switch_bounds)
                        debug.append(
                            {
                                "event": "hide_all_click_attempt",
                                "index": index,
                                "bounds": list(switch_bounds),
                                "clicked": bool(clicked),
                            }
                        )
                        if clicked:
                            time.sleep(0.4)
                            top_switch = _read_hide_all_top_switch()
                            debug.append({"event": "top_switch_after_click", "switch": _switch_debug(top_switch)})
                            if top_switch and top_switch.get("checked") is bool(target_enabled):
                                break
                checked = top_switch.get("checked") if top_switch else None
                verified = checked is bool(target_enabled) if target_enabled is not None else checked is not None
            except Exception:
                checked = None
                verified = False
                debug.append({"event": "hide_all_exception"})

            debug.append({"event": "hide_all_verified", "ok": bool(verified), "target": target_enabled, "checked": checked})
            return {"verified": verified, "checked": checked, "debug": debug}
        return {"verified": False, "checked": None, "debug": debug}

    try:
        initial = check_dnd_mode(serial=serial)
    except Exception:
        initial = ""

    if _is_target(initial) and target_mode == "0" and not restore_hide_all:
        try:
            device.press('home')
        except Exception:
            pass
        return {"ok": True, "method": "state_check_noop", "current": str(initial), "target": str(target_mode)}

    deadline = time.time() + max(2.0, float(timeout_sec))
    while time.time() < deadline:
        try:
            opened_direct = _open_dnd_settings_direct()

            if not opened_direct:
                try:
                    device.app_stop('com.android.settings')
                    time.sleep(0.4)
                except Exception:
                    pass
                device.app_start('com.android.settings')
                time.sleep(0.8)

                if _find_and_tap_settings_row(
                    exact_labels=('Notifications',),
                    contains_tokens=('Notification',),
                    max_swipes=4,
                ):
                    time.sleep(0.5)

                if _find_and_tap_settings_row(
                    exact_labels=('Do Not Disturb', 'Do not disturb'),
                    contains_tokens=('disturb', 'Disturb'),
                    regex_patterns=(r'(?i)^do\s+not\s+disturb$',),
                    max_swipes=3,
                ):
                    time.sleep(0.5)

            hide_all_verified = False
            hide_all_debug = []
            initial_hide_all_debug = []
            hide_all_checked = None
            hide_all_target = None
            cached_initial_hide_all = _DND_HIDE_ALL_INITIAL.get(dnd_cache_key)

            if target_mode == '1':
                _set_do_not_disturb_enabled(True)

                if dnd_cache_key not in _DND_HIDE_ALL_INITIAL:
                    initial_hide_ret = _configure_hide_notifications(target_enabled=None)
                    if initial_hide_ret.get("checked") is not None:
                        _DND_HIDE_ALL_INITIAL[dnd_cache_key] = bool(initial_hide_ret.get("checked"))
                    initial_hide_all_debug = [
                        {"phase": "initial_hide_all_state", **item}
                        for item in (initial_hide_ret.get("debug") or [])
                    ]
                    cached_initial_hide_all = _DND_HIDE_ALL_INITIAL.get(dnd_cache_key)

                hide_all_target = bool(cached_initial_hide_all) if restore_hide_all and cached_initial_hide_all is not None else True
                hide_ret = _configure_hide_notifications(target_enabled=hide_all_target)
                hide_all_verified = bool(hide_ret.get("verified"))
                hide_all_checked = hide_ret.get("checked")
                hide_all_debug = initial_hide_all_debug + (hide_ret.get("debug") or [])
                if hide_all_verified:
                    _DND_HIDE_ALL_VERIFIED.add(dnd_cache_key)
            elif target_mode == '0':
                restore_initial_hide_all = (
                    _DND_HIDE_ALL_INITIAL[dnd_cache_key]
                    if dnd_cache_key in _DND_HIDE_ALL_INITIAL
                    else None
                )
                if restore_initial_hide_all is not None:
                    hide_all_target = bool(restore_initial_hide_all)
                    hide_ret = _configure_hide_notifications(target_enabled=hide_all_target)
                    hide_all_verified = bool(hide_ret.get("verified"))
                    hide_all_checked = hide_ret.get("checked")
                    hide_all_debug = hide_ret.get("debug") or []
                    try:
                        device.press('back')
                        deadline_back = time.time() + 2.0
                        while time.time() < deadline_back:
                            if _looks_like_dnd_settings_screen():
                                break
                            time.sleep(0.2)
                    except Exception:
                        pass
                _set_do_not_disturb_enabled(False)
                _DND_HIDE_ALL_VERIFIED.discard(dnd_cache_key)

            current = check_dnd_mode(serial=serial)
            ok = _is_target(current)
            try:
                device.press('home')
            except Exception:
                pass
            if ok:
                if target_mode == '0':
                    _DND_HIDE_ALL_INITIAL.pop(dnd_cache_key, None)
                return {
                    "ok": True,
                    "method": "settings_ui_targeted_flow",
                    "current": str(current),
                    "target": str(target_mode),
                    "hide_all_verified": hide_all_verified if target_mode == '1' or restore_hide_all else None,
                    "hide_all_checked": hide_all_checked,
                    "hide_all_target": hide_all_target,
                    "hide_all_debug": hide_all_debug if target_mode == '1' or restore_hide_all else None,
                }
        except Exception:
            pass
        time.sleep(0.4)

    try:
        device.press('home')
    except Exception:
        pass

    try:
        current = check_dnd_mode(serial=serial)
    except Exception:
        current = ""
    return {
        "ok": _is_target(current),
        "method": "settings_ui_targeted_flow_failed",
        "current": str(current),
        "target": str(target_mode),
        "hide_all_verified": (dnd_cache_key in _DND_HIDE_ALL_VERIFIED) if target_mode == '1' else None,
        "hide_all_checked": None,
        "hide_all_target": None,
        "hide_all_debug": [],
    }

def set_wifi_enabled(flag, serial=None):
    d = get_adb_device(serial)
    commands = (
        ["svc wifi enable", "cmd wifi set-wifi-enabled enabled", "settings put global wifi_on 1"]
        if flag
        else ["svc wifi disable", "cmd wifi set-wifi-enabled disabled", "settings put global wifi_on 0"]
    )
    for cmd in commands:
        try:
            d.shell(cmd)
        except Exception:
            pass

def _read_bluetooth_manager_dump(serial=None):
    d = get_adb_device(serial)
    chunks = []
    commands = (
        "dumpsys bluetooth_manager",
        "dumpsys bluetooth",
    )
    for cmd in commands:
        try:
            out = d.shell(cmd) or ""
            out = out.strip()
            if out and out not in chunks:
                chunks.append(out)
        except Exception:
            continue
    return "\n".join(chunks)

def bluetooth_target_connected(target_name, serial=None):
    if not target_name:
        return False
    dump = _read_bluetooth_manager_dump(serial=serial)
    if not dump:
        return False

    lower_dump = dump.lower()
    lower_target = target_name.lower()
    if lower_target not in lower_dump:
        return False

    positive_markers = (
        "connected: true",
        "connected=true",
        "state=connected",
        "state: connected",
        "connection state: connected",
        "connectionstate: connected",
        "acl connected",
        "state: 2",
    )
    negative_markers = (
        "connected: false",
        "connected=false",
        "state=disconnected",
        "state: disconnected",
        "connection state: disconnected",
        "connectionstate: disconnected",
        "state: 0",
    )

    for match in re.finditer(re.escape(lower_target), lower_dump):
        start = max(0, match.start() - 500)
        end = min(len(lower_dump), match.end() + 500)
        segment = lower_dump[start:end]
        has_positive = any(marker in segment for marker in positive_markers)
        has_negative = any(marker in segment for marker in negative_markers)
        if has_positive and not has_negative:
            return True
        if has_positive:
            return True
    return False

def bluetooth_target_paired(target_name, serial=None):
    if not target_name:
        return False
    dump = _read_bluetooth_manager_dump(serial=serial)
    if not dump:
        return False

    lower_dump = dump.lower()
    lower_target = target_name.lower()
    if lower_target not in lower_dump:
        return False

    positive_markers = (
        "bond state: bonded",
        "bondstate: bonded",
        "bondstate=12",
        "bond state=12",
        "paired: true",
        "paired=true",
        "ispaired=true",
        "bonded",
    )
    negative_markers = (
        "bond state: none",
        "bondstate: none",
        "bondstate=10",
        "bond state=10",
        "paired: false",
        "paired=false",
        "ispaired=false",
        "unbonded",
        "not bonded",
    )

    for match in re.finditer(re.escape(lower_target), lower_dump):
        start = max(0, match.start() - 500)
        end = min(len(lower_dump), match.end() + 500)
        segment = lower_dump[start:end]
        has_positive = any(marker in segment for marker in positive_markers)
        has_negative = any(marker in segment for marker in negative_markers)
        if has_positive and not has_negative:
            return True
        if has_positive:
            return True
    return False

def bluetooth_any_device_paired(serial=None):
    dump = _read_bluetooth_manager_dump(serial=serial)
    if not dump:
        return False
    lower_dump = dump.lower()

    if "bond state: bonded" in lower_dump or "bondstate: bonded" in lower_dump:
        return True
    if "bondstate=12" in lower_dump or "bond state=12" in lower_dump:
        return True

    if "bonded devices" in lower_dump:
        if re.search(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", lower_dump):
            return True
    return False

def bluetooth_any_device_connected(serial=None):
    dump = _read_bluetooth_manager_dump(serial=serial)
    if not dump:
        return False
    lower_dump = dump.lower()
    positive_markers = (
        "state=connected",
        "state: connected",
        "connection state: connected",
        "connectionstate: connected",
        "connected: true",
        "connected=true",
        "acl connected",
        "state: 2",
    )
    return any(marker in lower_dump for marker in positive_markers)

def wait_for_bluetooth_target_connected(target_name, timeout_sec: float = 20, poll_sec: float = 1.0, serial=None):
    deadline = time.time() + max(1.0, float(timeout_sec))
    while time.time() < deadline:
        if bluetooth_target_connected(target_name, serial=serial):
            return True
        time.sleep(max(0.1, float(poll_sec)))
    return False

def prepare_windows_bluetooth_receiver(timeout_sec: float = 10.0, *, prefer_fresh_start: bool = False) -> dict:
    allow_existing_window_lookup = not prefer_fresh_start

    def _ret(ok: bool, reason: str, detail: str = "") -> dict:
        return {"ok": ok, "reason": reason, "detail": detail}

    def _close_existing_fsquirt_processes():
        if os.name != "nt":
            return
        try:
            subprocess.run(
                ["taskkill", "/IM", "fsquirt.exe", "/F"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            pass

    try:
        from pywinauto import Application, findwindows
    except Exception as e:
        if os.name == "nt":
            try:
                subprocess.Popen(["fsquirt.exe"], shell=False)
                return _ret(True, "pywinauto_unavailable_manual_receiver", "fsquirt_started_manually_accept_receive")
            except Exception as e2:
                return _ret(False, "pywinauto_unavailable", f"{e}; fallback_failed:{e2}")
        return _ret(False, "pywinauto_unavailable", str(e))

    def _connect_latest_window(title_patterns):
        if not allow_existing_window_lookup:
            return None
        patterns = title_patterns if isinstance(title_patterns, (list, tuple)) else [title_patterns]
        wins = []
        seen_handles = set()
        for title_re in patterns:
            try:
                found = findwindows.find_elements(title_re=title_re, backend="uia")
            except Exception:
                found = []
            for win in found:
                handle = getattr(win, "handle", None)
                if handle in seen_handles:
                    continue
                seen_handles.add(handle)
                wins.append(win)
        if not wins:
            return None
        latest = wins[-1]
        try:
            app = Application(backend="uia").connect(handle=latest.handle)
            return app.window(handle=latest.handle)
        except Exception:
            return None

    def _prepare_window(window) -> dict:
        def collect_window_texts(limit: int = 160) -> list[str]:
            texts = []
            try:
                title = (window.window_text() or "").strip()
                if title:
                    texts.append(title)
            except Exception:
                pass
            try:
                for node in window.descendants()[:limit]:
                    try:
                        text = (node.window_text() or "").strip()
                    except Exception:
                        text = ""
                    if text:
                        texts.append(text)
            except Exception:
                pass
            return texts

        def is_receive_waiting_screen() -> bool:
            try:
                text_blob = " | ".join(t.lower() for t in collect_window_texts())
            except Exception:
                text_blob = ""
            markers = (
                "receive files",
                "waiting for",
                "waiting to receive",
                "waiting for a connection",
                "bluetooth file transfer wizard",
            )
            if any(marker in text_blob for marker in markers):
                return True
            if ("bluetooth" in text_blob and "transfer" in text_blob and "waiting" in text_blob):
                return True
            patterns = (
                ".*Receive files.*",
                ".*Waiting for.*",
                ".*Bluetooth File Transfer Wizard.*",
                ".*Waiting to receive.*",
                ".*Bluetooth.*File.*Transfer.*",
                ".*파일.*수신.*",
                ".*수신.*대기.*",
                ".*대기 중.*",
                ".*Bluetooth.*전송.*",
            )
            try:
                for pat in patterns:
                    node = window.child_window(title_re=pat)
                    if node.exists(timeout=0.2):
                        return True
            except Exception:
                pass
            return False

        def wait_until_receive_ready(wait_sec: float = 2.5) -> bool:
            deadline2 = time.time() + max(0.5, float(wait_sec))
            while time.time() < deadline2:
                if is_receive_waiting_screen():
                    return True
                time.sleep(0.2)
            return False

        def click_next_button() -> bool:
            try:
                next_btn = window.child_window(auto_id="1", control_type="Button")
                if next_btn.exists(timeout=0.2) and next_btn.is_enabled():
                    next_btn.click_input()
                    time.sleep(0.4)
                    return True
            except Exception:
                pass

            try:
                buttons = window.descendants(control_type="Button")
                for btn in buttons:
                    try:
                        if btn.is_enabled():
                            btn.click_input()
                            time.sleep(0.4)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            return False

        try:
            if is_receive_waiting_screen():
                return _ret(True, "receive_waiting_screen_detected", "")
            receive_button = window.child_window(title_re=".*Receive.*", control_type="Button")
            if receive_button.exists(timeout=0.2):
                receive_button.click_input()
                time.sleep(0.4)
                if click_next_button() and wait_until_receive_ready():
                    return _ret(True, "receive_button_then_next_clicked", "")
                if wait_until_receive_ready():
                    return _ret(True, "receive_button_clicked", "")
        except Exception:
            pass

        try:
            links = window.descendants(control_type="Hyperlink")
            enabled_links = []
            preferred_links = []
            for link in links:
                try:
                    if link.is_enabled():
                        enabled_links.append(link)
                        label = (link.window_text() or "").strip().lower()
                        if ("receive" in label) or label.endswith("(r)"):
                            preferred_links.append(link)
                except Exception:
                    continue
            if enabled_links:
                target_link = preferred_links[0] if preferred_links else (enabled_links[1] if len(enabled_links) >= 2 else enabled_links[0])
                target_link.click_input()
                time.sleep(0.4)
                if click_next_button() and wait_until_receive_ready():
                    return _ret(True, "receive_hyperlink_then_next_clicked", "")
                if wait_until_receive_ready():
                    return _ret(True, "receive_hyperlink_clicked", "")
        except Exception:
            pass

        try:
            window.set_focus()
            for keys in ("!r", "r", "{TAB}{TAB}{ENTER}", "{DOWN}{ENTER}", "{TAB}{DOWN}{ENTER}"):
                try:
                    window.type_keys(keys)
                    time.sleep(0.3)
                    if is_receive_waiting_screen():
                        return _ret(True, "receive_shortcut_waiting_screen", keys)
                    if click_next_button() and wait_until_receive_ready():
                        return _ret(True, "receive_shortcut_then_next_clicked", keys)
                    if wait_until_receive_ready():
                        return _ret(True, "receive_shortcut_sent", keys)
                except Exception:
                    continue
            return _ret(False, "receive_shortcut_failed", "no_shortcut_accepted")
        except Exception as e:
            return _ret(False, "receive_shortcut_failed", str(e))

    existing_window = _connect_latest_window(
        (
            r".*Bluetooth File Transfer.*",
            r".*Bluetooth.*",
            r".*블루투스.*",
            r".*파일.*전송.*",
        )
    )
    if existing_window is not None:
        ret = _prepare_window(existing_window)
        if ret.get("ok"):
            ret["reason"] = f"existing_{ret.get('reason', 'receiver_ready')}"
        return ret

    app = None
    start_errors = []
    start_commands = (
        "fsquirt.exe -receive",
        "fsquirt.exe",
    )
    if prefer_fresh_start:
        _close_existing_fsquirt_processes()
    for cmd in start_commands:
        try:
            app = Application(backend="uia").start(cmd)
            if prefer_fresh_start:
                time.sleep(0.8)
                return _ret(True, "fresh_start_launched", cmd)
            break
        except Exception as e:
            start_errors.append(f"{cmd}:{e}")

    allow_existing_window_lookup = True
    if app is None:
        fallback_commands = (
            ["fsquirt.exe", "-receive"],
            ["fsquirt.exe"],
        )
        for cmd in fallback_commands:
            try:
                subprocess.Popen(cmd, shell=False)
                # Give the process a brief moment, then try to attach.
                time.sleep(0.8)
                existing_window = _connect_latest_window(
                    (
                        r".*Bluetooth File Transfer.*",
                        r".*Bluetooth.*",
                        r".*블루투스.*",
                        r".*파일.*전송.*",
                    )
                )
                if existing_window is not None:
                    ret = _prepare_window(existing_window)
                    if ret.get("ok"):
                        ret["reason"] = f"fallback_{ret.get('reason', 'receiver_ready')}"
                    return ret
                return _ret(True, "fsquirt_started_fallback", "; ".join(start_errors))
            except Exception as e:
                start_errors.append(f"{cmd}:{e}")
        return _ret(False, "fsquirt_start_failed", "; ".join(start_errors))

    attach_timeout_sec = min(float(timeout_sec), 3.0) if prefer_fresh_start else float(timeout_sec)
    deadline = time.time() + max(1.0, attach_timeout_sec)
    window = None
    while time.time() < deadline:
        try:
            candidate = app.window(title_re="Bluetooth.*")
            if candidate.exists(timeout=0.2):
                window = candidate
                break
        except Exception:
            pass
        time.sleep(0.3)

    if window is None:
        return _ret(False, "receiver_window_not_found", "")

    if prefer_fresh_start:
        return _ret(True, "fresh_window_detected", "")

    ret = _prepare_window(window)
    return ret

def ensure_windows_bluetooth_service_running(timeout_sec: float = 8.0) -> dict:
    if os.name != "nt":
        return {"ok": True, "reason": "non_windows", "detail": ""}

    def query_state() -> str:
        try:
            ret = subprocess.run(
                ["sc", "query", "bthserv"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception as e:
            return f"query_failed:{e}"
        out = f"{ret.stdout}\n{ret.stderr}".lower()
        if "running" in out:
            return "running"
        if "stopped" in out:
            return "stopped"
        return "unknown"

    state = query_state()
    if state == "running":
        return {"ok": True, "reason": "already_running", "detail": ""}

    try:
        subprocess.run(
            ["sc", "start", "bthserv"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception as e:
        return {"ok": False, "reason": "start_failed", "detail": str(e)}

    deadline = time.time() + max(1.0, float(timeout_sec))
    while time.time() < deadline:
        if query_state() == "running":
            return {"ok": True, "reason": "started", "detail": ""}
        time.sleep(0.5)
    return {"ok": False, "reason": "start_timeout", "detail": ""}

def open_windows_bluetooth_settings() -> dict:
    if os.name != "nt":
        return {"ok": True, "reason": "non_windows", "detail": ""}
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:bluetooth"], shell=False)
        return {"ok": True, "reason": "opened", "detail": ""}
    except Exception as e:
        return {"ok": False, "reason": "open_failed", "detail": str(e)}

def accept_windows_bluetooth_pairing_request(
    target_name: str = "",
    timeout_sec: float = 8.0,
    poll_sec: float = 0.5,
) -> dict:
    try:
        from pywinauto import Application, Desktop, findwindows
    except Exception as e:
        return {"ok": False, "reason": "pywinauto_unavailable", "detail": str(e)}

    button_patterns = (
        "Pair",
        "Yes",
        "Allow",
        "Accept",
        "Connect",
        "OK",
    )
    negative_button_patterns = (
        "Cancel",
        "Dismiss",
        "Not now",
        "Ignore",
        "No",
        "Close",
    )
    deadline = time.time() + max(1.0, float(timeout_sec))
    target_lower = (target_name or "").strip().lower()

    while time.time() < deadline:
        # Path 1: explicit Bluetooth windows.
        try:
            windows = findwindows.find_elements(title_re=".*Bluetooth.*", backend="uia")
        except Exception:
            windows = []

        for w in windows:
            try:
                app = Application(backend="uia").connect(handle=w.handle)
                window = app.window(handle=w.handle)
            except Exception:
                continue
            for label in button_patterns:
                try:
                    btn = window.child_window(title_re=f".*{label}.*", control_type="Button")
                    if btn.exists(timeout=0.2):
                        btn.click_input()
                        return {"ok": True, "reason": "pairing_accepted", "detail": label}
                except Exception:
                    pass

        # Path 2: toast/notification style popups with target text.
        try:
            desktop = Desktop(backend="uia")
            top_windows = desktop.windows()
        except Exception:
            top_windows = []

        for window in top_windows:
            try:
                descendants = window.descendants()
            except Exception:
                continue

            has_target_text = False
            if target_lower:
                try:
                    title = (window.window_text() or "").lower()
                except Exception:
                    title = ""
                if target_lower in title:
                    has_target_text = True
                if not has_target_text:
                    for node in descendants[:200]:
                        try:
                            text = (node.window_text() or "").lower()
                        except Exception:
                            text = ""
                        if text and target_lower in text:
                            has_target_text = True
                            break

            buttons = []
            for node in descendants[:300]:
                try:
                    if node.element_info.control_type == "Button":
                        buttons.append(node)
                except Exception:
                    continue
            if not buttons:
                continue

            # First try explicit positive labels.
            for btn in buttons:
                try:
                    label = (btn.window_text() or "").strip()
                except Exception:
                    continue
                if not label:
                    continue
                if any(p.lower() in label.lower() for p in negative_button_patterns):
                    continue
                if any(p.lower() in label.lower() for p in button_patterns):
                    try:
                        btn.click_input()
                        return {"ok": True, "reason": "pairing_accepted_notification", "detail": label}
                    except Exception:
                        pass

            # Fallback: if target device text is visible in this notification,
            # click the first non-negative button.
            if has_target_text:
                for btn in buttons:
                    try:
                        label = (btn.window_text() or "").strip()
                    except Exception:
                        label = ""
                    if label and any(p.lower() in label.lower() for p in negative_button_patterns):
                        continue
                    try:
                        btn.click_input()
                        return {"ok": True, "reason": "pairing_fallback_notification", "detail": label or "first_button"}
                    except Exception:
                        continue

        time.sleep(max(0.1, float(poll_sec)))
    return {"ok": False, "reason": "pairing_prompt_not_found", "detail": ""}

def prepare_windows_bluetooth_connection(
    target_name: str = "",
    *,
    ensure_service: bool = True,
    open_settings: bool = True,
    pairing_wait_sec: float = 8.0,
    receiver_timeout_sec: float = 10.0,
    enforce_pairing_prompt: bool = False,
    manual_pairing_approval: bool = False,
) -> dict:
    failures = []

    service_ret = {"ok": True, "reason": "skipped", "detail": ""}
    if ensure_service:
        service_ret = ensure_windows_bluetooth_service_running(timeout_sec=pairing_wait_sec)
        if not service_ret.get("ok"):
            failures.append("host_bt_service_not_ready")

    settings_ret = {"ok": True, "reason": "skipped", "detail": ""}
    if open_settings:
        settings_ret = open_windows_bluetooth_settings()
        if not settings_ret.get("ok"):
            failures.append("host_bt_settings_open_failed")

    if manual_pairing_approval:
        pair_ret = {
            "ok": True,
            "reason": "manual_approval_required",
            "detail": "accept_windows_bluetooth_notification",
        }
    else:
        pair_ret = accept_windows_bluetooth_pairing_request(
            target_name=target_name,
            timeout_sec=pairing_wait_sec,
        )
    if (not manual_pairing_approval) and enforce_pairing_prompt and not pair_ret.get("ok"):
        failures.append("host_bt_pairing_not_accepted")

    receiver_ret = prepare_windows_bluetooth_receiver(
        timeout_sec=receiver_timeout_sec,
        prefer_fresh_start=manual_pairing_approval,
    )
    if not receiver_ret.get("ok"):
        failures.append("host_bt_receiver_not_ready")

    return {
        "ok": len(failures) == 0,
        "target": target_name,
        "service": service_ret,
        "settings": settings_ret,
        "pairing": pair_ret,
        "receiver": receiver_ret,
        "failures": failures,
    }

def _windows_bt_receive_candidate_dirs(strict_exchange_only: bool = False) -> list[Path]:
    if os.name != "nt":
        return []
    home = Path.home()
    exchange_dir = home / "Documents" / "Bluetooth Exchange Folder"
    if strict_exchange_only and exchange_dir.exists() and exchange_dir.is_dir():
        return [exchange_dir]

    candidates = [home / "Downloads", exchange_dir, home / "Documents"]
    unique: list[Path] = []
    seen = set()
    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_dir():
            unique.append(p)
    return unique

def _scan_files_under(base_dir: Path, max_depth: int = 2) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not base_dir.exists() or not base_dir.is_dir():
        return result
    base_depth = len(base_dir.parts)
    for root, dirs, files in os.walk(base_dir):
        current_depth = len(Path(root).parts) - base_depth
        if current_depth >= max_depth:
            dirs[:] = []
        for name in files:
            p = Path(root) / name
            try:
                st = p.stat()
            except Exception:
                continue
            result[str(p.resolve())] = {
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
                "source_dir": str(base_dir),
            }
    return result

def list_windows_bluetooth_receive_files(max_depth: int = 2, strict_exchange_only: bool = False) -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    for base in _windows_bt_receive_candidate_dirs(strict_exchange_only=strict_exchange_only):
        snapshot.update(_scan_files_under(base, max_depth=max_depth))
    return snapshot

def collect_windows_bluetooth_received_files(
    dest_dir,
    *,
    baseline: dict[str, dict] | None = None,
    since_ts: float | None = None,
    timeout_sec: float = 45.0,
    poll_sec: float = 1.0,
    quiet_cycles: int = 2,
    max_depth: int = 2,
    strict_exchange_only: bool = False,
    allowed_extensions: list[str] | None = None,
    min_size_bytes: int = 1,
    expected_names: list[str] | None = None,
    expected_name_contains: list[str] | None = None,
) -> dict:
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)

    if os.name != "nt":
        return {"ok": False, "reason": "non_windows", "files": [], "detail": ""}

    before = (
        baseline
        if isinstance(baseline, dict)
        else list_windows_bluetooth_receive_files(max_depth=max_depth, strict_exchange_only=strict_exchange_only)
    )
    before_keys = set(before.keys())
    allowed_ext_set = None
    if allowed_extensions:
        allowed_ext_set = set()
        for ext in allowed_extensions:
            e = str(ext or "").strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = f".{e}"
            allowed_ext_set.add(e)
    start_ts = float(since_ts) if since_ts is not None else time.time()
    deadline = time.time() + max(1.0, float(timeout_sec))
    stable_count = 0
    candidates: dict[str, dict] = {}
    last_signature = ()
    expected_names_set = None
    expected_contains = None
    expected_skipped = 0

    if expected_names:
        expected_names_set = set()
        for raw in expected_names:
            name = (raw or "").strip()
            if name:
                expected_names_set.add(name.lower())
        if not expected_names_set:
            expected_names_set = None

    if expected_name_contains:
        expected_contains = []
        for raw in expected_name_contains:
            token = (raw or "").strip().lower()
            if token:
                expected_contains.append(token)
        if not expected_contains:
            expected_contains = None


    while time.time() < deadline:
        current = list_windows_bluetooth_receive_files(
            max_depth=max_depth,
            strict_exchange_only=strict_exchange_only,
        )
        current_candidates = {}
        for path_str, meta in current.items():
            p = Path(path_str)
            if allowed_ext_set is not None:
                if p.suffix.lower() not in allowed_ext_set:
                    continue
            if expected_names_set is not None or expected_contains is not None:
                name = p.name.lower()
                matches = False
                if expected_names_set is not None and name in expected_names_set:
                    matches = True
                if (not matches) and expected_contains is not None:
                    matches = any(token in name for token in expected_contains)
                if not matches:
                    expected_skipped += 1
                    continue
            mtime = float(meta.get("mtime", 0.0))
            size = int(meta.get("size", -1))
            if size < int(min_size_bytes):
                continue
            if path_str in before_keys:
                prev = before.get(path_str, {})
                prev_mtime = float(prev.get("mtime", 0.0))
                prev_size = int(prev.get("size", -1))
                changed = (mtime > (prev_mtime + 1e-6)) or (size != prev_size)
                if (not changed) or (mtime + 1e-6 < (start_ts - 2.0)):
                    continue
                current_candidates[path_str] = meta
                continue
            if mtime + 1e-6 < (start_ts - 2.0):
                continue
            current_candidates[path_str] = meta

        if current_candidates:
            signature = tuple(
                sorted(
                    (k, int(v.get("size", -1)), int(float(v.get("mtime", 0.0))))
                    for k, v in current_candidates.items()
                )
            )
            if signature == last_signature:
                stable_count += 1
            else:
                stable_count = 0
            last_signature = signature
            candidates = current_candidates
            if stable_count >= max(1, int(quiet_cycles)):
                break
        time.sleep(max(0.1, float(poll_sec)))

    if not candidates:
        detail = ""
        if expected_names_set is not None or expected_contains is not None:
            detail = f"expected_filter_applied; skipped={expected_skipped}"
            return {"ok": False, "reason": "no_new_files_detected_expected_filter", "files": [], "detail": detail}
        return {"ok": False, "reason": "no_new_files_detected", "files": [], "detail": detail}

    moved_files: list[str] = []
    failed_files: list[str] = []

    def to_ascii_safe_filename(name: str) -> str:
        stem = Path(name).stem
        suffix = Path(name).suffix
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
        if not safe_stem:
            safe_stem = "received_file"
        safe_suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffix)
        if safe_suffix and not safe_suffix.startswith("."):
            safe_suffix = f".{safe_suffix}"
        return f"{safe_stem}{safe_suffix}"

    for src_str in sorted(candidates.keys()):
        src = Path(src_str)
        if not src.exists() or (not src.is_file()):
            continue
        dst = destination / to_ascii_safe_filename(src.name)
        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            index = 1
            while True:
                candidate = destination / f"{stem}_{index}{suffix}"
                if not candidate.exists():
                    dst = candidate
                    break
                index += 1
        try:
            shutil.move(str(src), str(dst))
            moved_files.append(str(dst))
        except Exception:
            failed_files.append(str(src))

    return {
        "ok": len(moved_files) > 0,
        "reason": "moved" if moved_files else "move_failed",
        "files": moved_files,
        "failed": failed_files,        "detail": f"expected_filter_skipped={expected_skipped}" if expected_skipped else "",
    }

def toggle_dnd_mode(
    device,
    audit_log_path=None,
    package_name="system",
    run_id=None,
    phase="setup",
    serial=None,
    target_mode: str = "1",
    timeout_sec: float = 8.0,
):
    """Public DND toggle entrypoint used by collectors/init flow."""
    ret = _apply_dnd_mode_via_settings(device, target_mode=target_mode, serial=serial, timeout_sec=timeout_sec)

    try:
        write_audit(
            audit_log_path,
            package_name,
            action="toggle_dnd_mode",
            selector="settings_ui",
            result="success" if ret.get("ok") else "fail",
            artifacts=[
                f"target={ret.get('target')}",
                f"current={ret.get('current')}",
                f"method={ret.get('method')}",
            ],
            run_id=run_id,
            phase=phase,
        )
    except Exception:
        pass
    return ret


def ensure_dnd_mode(
    device,
    *,
    serial=None,
    target_mode: str = "1",
    timeout_sec: float = 8.0,
    reapply_if_target: bool = False,
    restore_hide_all: bool = False,
):
    """Common DND policy helper used by collectors.

    - If current state already matches target and reapply_if_target is False,
      skip UI entry and return a noop result.
    - Otherwise apply the full Settings-based flow.
    """
    target_mode = str(target_mode)
    try:
        current = check_dnd_mode(serial=serial)
    except Exception:
        current = ""

    current_str = str(current).strip()
    dnd_cache_key = serial if serial else "__default__"
    target_reached = (
        current_str not in {"", "0", "null", "None"}
        if target_mode == "1"
        else current_str == "0"
    )
    if target_mode == "0" and target_reached and not reapply_if_target and not restore_hide_all:
        _DND_HIDE_ALL_VERIFIED.discard(dnd_cache_key)
        return {
            "ok": True,
            "method": "state_check_noop",
            "current": current_str,
            "target": target_mode,
        }
    if target_mode == "1" and target_reached and not reapply_if_target and not restore_hide_all and dnd_cache_key in _DND_HIDE_ALL_VERIFIED:
        return {
            "ok": True,
            "method": "state_check_noop",
            "current": current_str,
            "target": target_mode,
            "hide_all_verified": True,
        }

    return _apply_dnd_mode_via_settings(
        device,
        target_mode=target_mode,
        serial=serial,
        timeout_sec=timeout_sec,
        restore_hide_all=restore_hide_all,
    )


def scroll_down(
    device,
    duration: float = 0.2,
    distance_ratio: float = 0.4,
    *,
    top_bound: int | None = None,
    bottom_bound: int | None = None,
):
    w, h = device.window_size()
    x = w // 2
    ratio = min(max(float(distance_ratio), 0.05), 0.9)
    safe_top = max(int(top_bound) if isinstance(top_bound, int) else 0, 0)
    safe_bottom = min(int(bottom_bound) if isinstance(bottom_bound, int) else h, h)

    if safe_bottom <= safe_top + 1:
        safe_top = 0
        safe_bottom = h

    safe_height = max(safe_bottom - safe_top, 1)
    half = ratio / 2.0
    center_y = safe_top + (safe_height / 2.0)
    delta = safe_height * half
    start_y = int(center_y + delta)
    end_y = int(center_y - delta)
    start_y = min(max(start_y, safe_top), safe_bottom - 1)
    end_y = min(max(end_y, safe_top), safe_bottom - 1)
    device.swipe(x, start_y, x, end_y, duration)

def scroll_up(
    device,
    duration: float = 0.2,
    distance_ratio: float = 0.4,
    *,
    top_bound: int | None = None,
    bottom_bound: int | None = None,
):
    w, h = device.window_size()
    x = w // 2
    ratio = min(max(float(distance_ratio), 0.05), 0.9)
    safe_top = max(int(top_bound) if isinstance(top_bound, int) else 0, 0)
    safe_bottom = min(int(bottom_bound) if isinstance(bottom_bound, int) else h, h)

    if safe_bottom <= safe_top + 1:
        safe_top = 0
        safe_bottom = h

    safe_height = max(safe_bottom - safe_top, 1)
    half = ratio / 2.0
    center_y = safe_top + (safe_height / 2.0)
    delta = safe_height * half
    start_y = int(center_y - delta)
    end_y = int(center_y + delta)
    start_y = min(max(start_y, safe_top), safe_bottom - 1)
    end_y = min(max(end_y, safe_top), safe_bottom - 1)
    device.swipe(x, start_y, x, end_y, duration)

def wait_for_windows_bluetooth_receive_complete(
    timeout_sec: float = 45.0,
    poll_sec: float = 1.0,
    save_dir: str | None = None,
) -> dict:
    """
    Host-side confirmation for Windows Bluetooth receive flow.
    Success is returned only when an explicit Finish button click succeeds.
    If transfer is still in progress, timeout is extended within a hard cap.
    """
    try:
        from pywinauto import Application, findwindows
    except Exception as e:
        return {
            "ok": False,
            "reason": "pywinauto_unavailable",
            "detail": str(e),
        }

    base_timeout = max(1.0, float(timeout_sec))
    hard_timeout = max(base_timeout * 4.0, 180.0)
    started_ts = time.time()
    base_deadline = started_ts + base_timeout
    hard_deadline = started_ts + hard_timeout
    progress_grace_sec = max(20.0, min(90.0, base_timeout))

    last_detail = ""
    saw_receiver_window = False
    missing_after_seen = 0
    missing_limit = max(5, int(round(5.0 / max(0.1, float(poll_sec)))))
    last_progress_ts = started_ts
    last_file_names: list[str] = []
    captured_paths: list[str] = []
    progress_captured = False

    def _capture_window(window, tag: str) -> str:
        if not save_dir:
            return ""
        try:
            out_dir = Path(save_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            shot_path = out_dir / f"host_bluetooth_{tag}_{int(time.time() * 1000)}.png"
            window.capture_as_image().save(str(shot_path))
            captured_paths.append(str(shot_path))
            return str(shot_path)
        except Exception:
            return ""

    def _window_exists(handle: int) -> bool:
        try:
            elems = findwindows.find_elements(handle=handle, backend="uia")
            return len(elems) > 0
        except Exception:
            return False

    def _extract_received_file_names(window) -> list[str]:
        names: list[str] = []
        try:
            items = window.descendants(control_type="ListItem")
        except Exception:
            items = []
        for item in items[:80]:
            try:
                label = (item.window_text() or "").strip()
            except Exception:
                continue
            if not label:
                continue
            lower = label.lower()
            if lower in ("file name", "size"):
                continue
            names.append(label)
        unique: list[str] = []
        seen = set()
        for n in names:
            key = n.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(n.strip())
        return unique

    def _click_finish_button(window) -> tuple[bool, str]:
        def _select_first_list_item() -> bool:
            try:
                items = window.descendants(control_type="ListItem")
            except Exception:
                items = []
            for item in items:
                try:
                    label = (item.window_text() or "").strip()
                except Exception:
                    label = ""
                lower = label.lower()
                if not label or lower in ("file name", "size"):
                    continue
                try:
                    item.click_input()
                    time.sleep(0.2)
                    return True
                except Exception:
                    continue
            return False

        try:
            btn = window.child_window(title="Finish", control_type="Button")
            if btn.exists(timeout=0.2):
                if not btn.is_enabled():
                    _select_first_list_item()
                if btn.is_enabled():
                    _capture_window(window, "finish")
                    btn.click_input()
                    return True, "Finish"
        except Exception:
            pass

        try:
            btn = window.child_window(title_re=".*Finish.*", control_type="Button")
            if btn.exists(timeout=0.2):
                if not btn.is_enabled():
                    _select_first_list_item()
                if btn.is_enabled():
                    _capture_window(window, "finish")
                    btn.click_input()
                    return True, "Finish"
        except Exception:
            pass

        try:
            for btn in window.descendants(control_type="Button"):
                try:
                    label = (btn.window_text() or "").strip()
                    if label.lower() == "finish" and btn.is_enabled():
                        _capture_window(window, "finish")
                        btn.click_input()
                        return True, "Finish"
                except Exception:
                    continue
        except Exception:
            pass

        return False, ""

    def _is_transfer_in_progress(window) -> bool:
        keywords = (
            "receiving",
            "transferring",
            "progress",
            "remaining",
            "bytes",
            "kb",
            "mb",
            "gb",
            "%",
        )
        texts = []
        try:
            title_text = (window.window_text() or "").strip()
            if title_text:
                texts.append(title_text)
        except Exception:
            pass

        try:
            descendants = window.descendants()[:300]
            for node in descendants:
                try:
                    t = (node.window_text() or "").strip()
                    if t:
                        texts.append(t)
                except Exception:
                    continue
        except Exception:
            pass

        if not texts:
            return False

        joined = " ".join(texts).lower()
        if any(k in joined for k in keywords):
            return True
        if re.search(r"\b\d{1,3}\s*%\b", joined):
            return True
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:kb|mb|gb)\b", joined):
            return True
        return False

    while time.time() < hard_deadline:
        now_ts = time.time()
        try:
            windows = findwindows.find_elements(title_re="Bluetooth.*", backend="uia")
        except Exception as e:
            last_detail = f"window_scan_failed:{e}"
            time.sleep(max(0.1, float(poll_sec)))
            continue

        if not windows:
            if saw_receiver_window:
                missing_after_seen += 1
                if missing_after_seen >= missing_limit:
                    return {
                        "ok": False,
                        "reason": "receiver_window_closed",
                        "detail": last_detail or "bluetooth_window_disappeared_during_receive",
                        "file_names": last_file_names,
                        "screenshots": captured_paths,
                    }
            else:
                last_detail = "bluetooth_window_not_found"
            time.sleep(max(0.1, float(poll_sec)))
            continue

        saw_receiver_window = True
        missing_after_seen = 0

        latest = windows[-1]
        handle = latest.handle
        try:
            app = Application(backend="uia").connect(handle=handle)
            window = app.window(handle=handle)
        except Exception as e:
            last_detail = f"window_connect_failed:{e}"
            time.sleep(max(0.1, float(poll_sec)))
            continue

        try:
            names = _extract_received_file_names(window)
            if names:
                last_file_names = names
        except Exception:
            pass

        clicked, label = _click_finish_button(window)
        if clicked:
            time.sleep(0.4)
            return {
                "ok": True,
                "reason": "finish_button_clicked",
                "detail": label,
                "file_names": last_file_names,
                "screenshots": captured_paths,
            }

        if _is_transfer_in_progress(window):
            last_progress_ts = now_ts
            last_detail = "transfer_in_progress"
            if not progress_captured:
                _capture_window(window, "progress")
                progress_captured = True
        else:
            last_detail = "finish_button_not_found"

        if (now_ts > base_deadline) and ((now_ts - last_progress_ts) > progress_grace_sec):
            break

        time.sleep(max(0.1, float(poll_sec)))

    return {
        "ok": False,
        "reason": "timeout",
        "detail": f"{last_detail}; elapsed={round(time.time() - started_ts, 2)}s",
        "file_names": last_file_names,
        "screenshots": captured_paths,
    }

def detect_host_device_name() -> str:
    candidates = [
        os.environ.get("AURA_BT_TARGET_NAME", ""),
        os.environ.get("BLUETOOTH_DEVICE_NAME", ""),
        os.environ.get("COMPUTERNAME", ""),
        platform.node(),
        socket.gethostname(),
    ]
    for raw in candidates:
        name = (raw or "").strip()
        if name:
            return name
    return ""





