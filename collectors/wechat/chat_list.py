import hashlib
from dataclasses import dataclass

from utils.ocr import OcrItem, is_time_like


@dataclass(frozen=True)
class ChatCandidate:
    chat_id: str
    chat_name: str
    cx: int
    cy: int
    name_bbox: list[list[float]]
    time_text: str | None = None
    row_signature: str | None = None
    swipe_index: int = 0

def _stable_chat_id(chat_name: str, row_signature: str | None = None, time_text: str | None = None) -> str:
    # Dates are OCR-volatile on WeChat rows, so keep IDs based on stable row semantics only.
    key = f"{chat_name.strip()}|{(row_signature or '').strip()}".encode("utf-8", errors="ignore")
    return hashlib.sha256(key).hexdigest()

def _center_of_bbox(bbox: list[list[float]]) -> tuple[int, int]:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    cx = int(sum(xs) / max(1, len(xs)))
    cy = int(sum(ys) / max(1, len(ys)))
    return cx, cy

def _bbox_width(bbox: list[list[float]]) -> float:
    xs = [p[0] for p in bbox]
    return float(max(xs) - min(xs)) if xs else 0.0

def _bbox_left(bbox: list[list[float]]) -> float:
    xs = [p[0] for p in bbox]
    return float(min(xs)) if xs else 0.0

def _preview_text_for_row(
    cleaned: list[OcrItem],
    name_item: OcrItem,
    *,
    row_tol_px: float,
    nav_blacklist: set[str],
) -> str:
    preview_gap_max = max(44.0, row_tol_px * 3.5)
    preview_left_tol = 56.0
    preview_x_tol = 220.0
    name_left = _bbox_left(name_item.bbox)

    candidates: list[OcrItem] = []
    for it in cleaned:
        text = (it.text or "").strip()
        if not text or text in nav_blacklist or is_time_like(text):
            continue
        gap = float(it.cy) - float(name_item.cy)
        if not (0 < gap <= preview_gap_max):
            continue
        same_left_column = abs(_bbox_left(it.bbox) - name_left) <= preview_left_tol
        same_row_band = abs(float(it.cx) - float(name_item.cx)) <= preview_x_tol
        if same_left_column or same_row_band:
            candidates.append(it)

    candidates.sort(key=lambda it: (float(it.cy), float(it.cx)))
    return " ".join((it.text or "").strip() for it in candidates[:2]).strip()

def extract_chat_candidates(
    items: list[OcrItem],
    nav_blacklist: set[str],
    *,
    row_tol_px: float = 24.0,
    min_conf: float = 0.35,
    swipe_index: int = 0,
    safe_top: float | None = None,
    safe_bottom: float | None = None,
) -> list[ChatCandidate]:
    cleaned: list[OcrItem] = []
    for it in items:
        txt = (it.text or "").strip()
        if not txt:
            continue
        if it.conf < min_conf:
            continue
        if txt in nav_blacklist:
            continue
        if safe_top is not None and float(it.cy) < float(safe_top):
            continue
        if safe_bottom is not None and float(it.cy) > float(safe_bottom):
            continue
        cleaned.append(it)

    time_items = [it for it in cleaned if is_time_like(it.text)]

    candidates: list[ChatCandidate] = []
    seen_ids: set[str] = set()
    selected_name_rows: list[tuple[int, str]] = []

    for t in time_items:
        row_items = [
            it
            for it in cleaned
            if (it.cx < t.cx)
            and (abs(it.cy - t.cy) <= row_tol_px)
            and (it.text not in nav_blacklist)
            and (len(it.text.strip()) >= 2)
            and (not is_time_like(it.text))
        ]
        if not row_items:
            continue

        # Prefer the left-most plausible token as chat title.
        row_items.sort(key=lambda it: (it.cx, -_bbox_width(it.bbox)))
        name_item = row_items[0]

        preview_text = _preview_text_for_row(
            cleaned,
            name_item,
            row_tol_px=row_tol_px,
            nav_blacklist=nav_blacklist,
        )
        row_signature = "|".join(
            part
            for part in [
                "|".join((it.text or "").strip() for it in sorted(row_items, key=lambda x: x.cx)[:4]).strip(),
                preview_text,
            ]
            if part
        ).strip()
        chat_id = _stable_chat_id(name_item.text.strip(), row_signature=row_signature, time_text=t.text.strip())
        if chat_id in seen_ids:
            continue

        cx, cy = _center_of_bbox(name_item.bbox)
        seen_ids.add(chat_id)
        selected_name_rows.append((cy, name_item.text.strip().lower()))
        candidates.append(
            ChatCandidate(
                chat_id=chat_id,
                chat_name=name_item.text.strip(),
                cx=cx,
                cy=cy,
                name_bbox=name_item.bbox,
                time_text=t.text.strip(),
                row_signature=row_signature,
                swipe_index=swipe_index,
            )
        )

    # Limited fallback rows are considered so partially-clipped time anchors do not hide a row.
    # We only accept a row when it looks like a title with a nearby preview row below it.
    rows: list[list[OcrItem]] = []
    for it in sorted(cleaned, key=lambda x: x.cy):
        if is_time_like(it.text):
            continue
        if not rows or abs(rows[-1][0].cy - it.cy) > row_tol_px:
            rows.append([it])
        else:
            rows[-1].append(it)

    preview_gap_max = max(44.0, row_tol_px * 3.5)
    preview_x_tol = 220.0
    preview_left_tol = 56.0

    for idx, row in enumerate(rows):
        row.sort(key=lambda x: x.cx)
        name_item = row[0]
        name = (name_item.text or "").strip()
        if name in nav_blacklist or len(name) < 2:
            continue

        cx, cy = _center_of_bbox(name_item.bbox)
        lowered = name.lower()
        if any(abs(existing_cy - cy) <= row_tol_px * 2.5 and existing_name == lowered for existing_cy, existing_name in selected_name_rows):
            continue

        next_row = rows[idx + 1] if idx + 1 < len(rows) else None
        has_preview_row = False
        if next_row:
            next_row.sort(key=lambda x: x.cx)
            next_item = next_row[0]
            next_name = (next_item.text or "").strip()
            next_gap = float(next_item.cy) - float(name_item.cy)
            same_left_column = abs(_bbox_left(next_item.bbox) - _bbox_left(name_item.bbox)) <= preview_left_tol
            if (
                next_name
                and next_name not in nav_blacklist
                and not is_time_like(next_name)
                and 0 < next_gap <= preview_gap_max
                and (
                    same_left_column
                    or abs(float(next_item.cx) - float(name_item.cx)) <= preview_x_tol
                )
            ):
                has_preview_row = True

        if not has_preview_row:
            continue

        row_signature = "|".join((it.text or "").strip() for it in row[:4]).strip()
        chat_id = _stable_chat_id(name, row_signature=row_signature, time_text=None)
        if chat_id in seen_ids:
            continue

        seen_ids.add(chat_id)
        selected_name_rows.append((cy, lowered))
        candidates.append(
            ChatCandidate(
                chat_id=chat_id,
                chat_name=name,
                cx=cx,
                cy=cy,
                name_bbox=name_item.bbox,
                time_text=None,
                row_signature=row_signature,
                swipe_index=swipe_index,
            )
        )

    candidates.sort(key=lambda c: c.cy)
    return candidates
