import hashlib
import json
from typing import Any

from utils.ocr import OcrItem, is_time_like

def _group_items_into_lines(items: list[OcrItem], *, line_tol_px: float = 18.0) -> list[list[OcrItem]]:
    lines: list[list[OcrItem]] = []
    for it in sorted(items, key=lambda x: (x.cy, x.cx)):
        if not lines or abs(lines[-1][0].cy - it.cy) > line_tol_px:
            lines.append([it])
        else:
            lines[-1].append(it)
    for line in lines:
        line.sort(key=lambda x: x.cx)
    return lines

def parse_ocr_page_to_messages(
    items: list[OcrItem],
    *,
    app: str,
    phase: str,
    account: str,
    chat_id: str,
    screenshot_path: str,
    uitree_path: str | None,
    ocr_json_path: str | None,
    nav_blacklist: set[str] | None = None,
    page_index: int = 0,
    min_conf: float = 0.25,
) -> list[dict[str, Any]]:
    nav_blacklist = nav_blacklist or set()
    filtered: list[OcrItem] = []
    for it in items:
        txt = (it.text or "").strip()
        if not txt:
            continue
        if it.conf < min_conf:
            continue
        if txt in nav_blacklist:
            continue
        if is_time_like(txt):
            continue
        filtered.append(it)

    lines = _group_items_into_lines(filtered)
    out: list[dict[str, Any]] = []

    for idx, line in enumerate(lines):
        text = " ".join((it.text or "").strip() for it in line if (it.text or "").strip())
        if not text:
            continue

        raw = {
            "page_index": page_index,
            "line_index": idx,
            "items": [
                {
                    "text": it.text,
                    "conf": it.conf,
                    "bbox": it.bbox,
                    "cx": it.cx,
                    "cy": it.cy,
                }
                for it in line
            ],
        }

        msg_key = f"{app}|{phase}|{account}|{chat_id}|{page_index}|{idx}|{text}".encode("utf-8", errors="ignore")
        message_id = hashlib.sha256(msg_key).hexdigest()

        artifacts: list[str] = []
        if ocr_json_path:
            artifacts.append(ocr_json_path)

        screenshots = [screenshot_path] if screenshot_path else []
        uitrees = [uitree_path] if uitree_path else []

        out.append(
            {
                "message_id": message_id,
                "type": "ocr_line",
                "sender": None,
                "timestamp": None,
                "content": text,
                "raw": json.dumps(raw, ensure_ascii=False),
                "artifacts": artifacts,
                "screenshot_paths": screenshots,
                "uitree_paths": uitrees,
            }
        )

    return out
