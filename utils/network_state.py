import re
import time

import adbutils

def _adb_device(serial=None):
    if serial:
        return adbutils.adb.device(serial=serial)
    return adbutils.adb.device()

def _safe_shell(command: str, serial=None) -> str:
    try:
        return _adb_device(serial=serial).shell(command).strip()
    except Exception:
        return ""

def get_airplane_mode(serial=None) -> str:
    return _safe_shell("settings get global airplane_mode_on", serial=serial)

def get_dnd_mode(serial=None) -> str:
    return _safe_shell("settings get global zen_mode", serial=serial)

def get_wifi_enabled(serial=None) -> str:
    ret = _safe_shell("settings get global wifi_on", serial=serial)
    if ret in {"0", "1"}:
        return ret

    status = _safe_shell("cmd wifi status", serial=serial)
    if "enabled" in status.lower():
        return "1"
    if "disabled" in status.lower():
        return "0"
    return ""

def get_wifi_connected(serial=None) -> bool:

    status = _safe_shell("cmd wifi status", serial=serial).lower()
    if "connected to" in status:
        return True
    if "not connected" in status or "disconnected" in status:
        return False

    dump = _safe_shell("dumpsys wifi", serial=serial).lower()
    return (
        "networkinfo[state: connected" in dump
        or "supplicant state: completed" in dump
    )

def get_wifi_ssid(serial=None) -> str:
    dump = _safe_shell("dumpsys wifi", serial=serial)
    patterns = [
        r"SSID:\s*([^\n,]+)",
        r"WifiInfo.*SSID:\s*([^\n,]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, dump, re.IGNORECASE)
        if m:
            ssid = m.group(1).strip().strip('"')
            if ssid and ssid.lower() != "<unknown ssid>":
                return ssid
    return ""

def _on_off(value: str) -> str:
    v = str(value or '').strip()
    if v == '1':
        return 'On'
    if v == '0':
        return 'Off'
    return ''

def snapshot_network_state(serial=None) -> dict:
    airplane_mode = get_airplane_mode(serial=serial)
    dnd_mode = get_dnd_mode(serial=serial)
    wifi_enabled = get_wifi_enabled(serial=serial)



    if wifi_enabled == "1":
        wifi_connected = get_wifi_connected(serial=serial)
        wifi_ssid = get_wifi_ssid(serial=serial)
    else:
        wifi_connected = False
        wifi_ssid = ""

    return {
        "ts": time.time(),
        "airplane_mode": airplane_mode,
        "airplane_mode_state": _on_off(airplane_mode),
        "dnd_mode": dnd_mode,
        "dnd_mode_state": _on_off(dnd_mode),
        "wifi_enabled": wifi_enabled,
        "wifi_connected": wifi_connected,
        "wifi_ssid": wifi_ssid,
    }

def evaluate_network_policy(snapshot: dict, policy: dict) -> list[str]:
    failures: list[str] = []
    mode = (policy or {}).get("mode", "offline_airplane")
    require_airplane = (policy or {}).get("require_airplane_enabled", True)
    require_dnd = (policy or {}).get("require_dnd_enabled", True)

    if mode == "offline_airplane":
        if require_airplane and snapshot.get("airplane_mode") != "1":
            failures.append("airplane_mode_not_enabled")
        if require_dnd and str(snapshot.get("dnd_mode", "")) == "0":
            failures.append("dnd_not_enabled")



    if mode == "online_wifi":
        if require_airplane and snapshot.get("airplane_mode") != "1":
            failures.append("airplane_mode_not_enabled")
        if require_dnd and str(snapshot.get("dnd_mode", "")) == "0":
            failures.append("dnd_not_enabled")

        if not snapshot.get("wifi_connected"):
            failures.append("wifi_not_connected")

    return failures
