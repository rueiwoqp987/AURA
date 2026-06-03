from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


PROFILE_ROOT = Path(__file__).resolve().parent.parent / "profiles" / "system_ui"
DEFAULT_PROFILE = "generic"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=16)
def load_system_ui_profile(profile_name: str = DEFAULT_PROFILE) -> dict[str, Any]:
    name = (profile_name or DEFAULT_PROFILE).strip().lower()
    profile = _read_json(PROFILE_ROOT / f"{name}.json")
    if profile:
        return profile
    if name != DEFAULT_PROFILE:
        return load_system_ui_profile(DEFAULT_PROFILE)
    return profile


def _adb_getprop(serial: str | None, prop: str) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "getprop", prop])
    try:
        ret = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5)
    except Exception:
        return ""
    if ret.returncode != 0:
        return ""
    return (ret.stdout or "").strip()


def detect_system_ui_profile_name(serial: str | None = None, manufacturer: str | None = None) -> str:
    maker = (manufacturer or _adb_getprop(serial, "ro.product.manufacturer") or "").strip().lower()
    if not maker:
        return DEFAULT_PROFILE

    for profile_path in sorted(PROFILE_ROOT.glob("*.json")):
        profile = _read_json(profile_path)
        matches = profile.get("manufacturer_match") or []
        for token in matches:
            token = str(token or "").strip().lower()
            if token and token in maker:
                return str(profile.get("name") or profile_path.stem).strip().lower()
    return DEFAULT_PROFILE


def resolve_system_ui_profile(
    serial: str | None = None,
    *,
    manufacturer: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    selected = (profile_name or "").strip().lower() or detect_system_ui_profile_name(
        serial=serial,
        manufacturer=manufacturer,
    )
    return load_system_ui_profile(selected)


def dnd_profile_for(
    serial: str | None = None,
    *,
    manufacturer: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    profile = resolve_system_ui_profile(serial, manufacturer=manufacturer, profile_name=profile_name)
    dnd = profile.get("dnd") or {}
    return dnd if isinstance(dnd, dict) else {}


def airplane_profile_for(
    serial: str | None = None,
    *,
    manufacturer: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    profile = resolve_system_ui_profile(serial, manufacturer=manufacturer, profile_name=profile_name)
    airplane = profile.get("airplane") or {}
    return airplane if isinstance(airplane, dict) else {}


def bluetooth_profile_for(
    serial: str | None = None,
    *,
    manufacturer: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    profile = resolve_system_ui_profile(serial, manufacturer=manufacturer, profile_name=profile_name)
    bluetooth = profile.get("bluetooth") or {}
    return bluetooth if isinstance(bluetooth, dict) else {}


def network_profile_for(
    serial: str | None = None,
    *,
    manufacturer: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    profile = resolve_system_ui_profile(serial, manufacturer=manufacturer, profile_name=profile_name)
    network = profile.get("network") or {}
    return network if isinstance(network, dict) else {}


def recent_apps_profile_for(
    serial: str | None = None,
    *,
    manufacturer: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    profile = resolve_system_ui_profile(serial, manufacturer=manufacturer, profile_name=profile_name)
    recent_apps = profile.get("recent_apps") or {}
    return recent_apps if isinstance(recent_apps, dict) else {}
