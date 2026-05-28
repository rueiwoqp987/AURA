import time

import adbutils

def _adb_device(serial=None):
    if serial:
        return adbutils.adb.device(serial=serial)
    return adbutils.adb.device()

def _shell(d, cmd: str) -> str:
    try:
        return d.shell(cmd).strip()
    except Exception:
        return ""

def collect_device_identifiers(serial=None) -> dict:
    d = _adb_device(serial=serial)
    return {
        "collected_ts": time.time(),
        "serial": serial or "",
        "ro_product_manufacturer": _shell(d, "getprop ro.product.manufacturer"),
        "ro_product_model": _shell(d, "getprop ro.product.model"),
        "ro_product_device": _shell(d, "getprop ro.product.device"),
        "ro_build_fingerprint": _shell(d, "getprop ro.build.fingerprint"),
        "ro_build_version_release": _shell(d, "getprop ro.build.version.release"),
        "ro_build_version_sdk": _shell(d, "getprop ro.build.version.sdk"),
        "persist_sys_timezone": _shell(d, "getprop persist.sys.timezone"),
        "device_epoch": _shell(d, "date +%s"),
    }

