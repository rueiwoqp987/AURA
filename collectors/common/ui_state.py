from __future__ import annotations

import hashlib
import re
from typing import Any


class XmlUiMixin:
    """Small XML node helpers for Android UI hierarchy parsing."""

    def _xml_class(self, node) -> str:
        return node.attrib.get("class", "")

    def _xml_text(self, node) -> str:
        return (node.attrib.get("text") or "").strip()

    def _xml_desc(self, node) -> str:
        return (node.attrib.get("content-desc") or "").strip()

    def _xml_package(self, node) -> str:
        return (node.attrib.get("package") or "").strip()

    def _parse_bounds_str(self, raw) -> list[int] | None:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
        return [int(v) for v in m.groups()] if m else None

    def _xml_bounds(self, node) -> list[int] | None:
        return self._parse_bounds_str(node.attrib.get("bounds", ""))

    def _xml_texts(self, node) -> list[str]:
        return [
            self._xml_text(child)
            for child in node.iter()
            if self._xml_class(child) == "android.widget.TextView" and self._xml_text(child)
        ]

    def _xml_descs(self, node) -> list[str]:
        return [
            self._xml_desc(child)
            for child in node.iter()
            if self._xml_desc(child)
        ]

    def _xml_has_text(self, node, text: str) -> bool:
        return text in self._xml_texts(node)


class BoundsUiMixin:
    """Bounds normalization, safe-area checks, and click targeting helpers."""

    device: Any

    def _bounds_to_list(self, bounds) -> list[int] | None:
        if isinstance(bounds, dict):
            keys = ("left", "top", "right", "bottom")
            if not all(isinstance(bounds.get(key), int) for key in keys):
                return None
            return [bounds[key] for key in keys]

        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            try:
                return [int(v) for v in bounds]
            except (TypeError, ValueError):
                return None

        return None

    def _bounds_visibility_reason(
        self,
        bounds,
        safe_top,
        safe_bottom,
        *,
        margin: int = 8,
        min_height: int = 32,
    ) -> str | None:
        resolved = self._bounds_to_list(bounds)
        if not resolved:
            return "invalid_bounds"

        _, top, _, bottom = resolved
        if bottom <= top:
            return "invalid_height"
        if bottom - top < min_height:
            return "too_short"
        if isinstance(safe_top, int) and top < safe_top + margin:
            return "overlap_top"
        if isinstance(safe_bottom, int) and bottom > safe_bottom - margin:
            return "overlap_bottom"
        return None

    def _bounds_readability_reason(
        self,
        bounds,
        safe_top,
        safe_bottom,
        *,
        margin: int = 0,
        min_visible_height: int = 48,
        min_visible_ratio: float = 0.15,
    ) -> str | None:
        resolved = self._bounds_to_list(bounds)
        if not resolved:
            return "invalid_bounds"

        _, top, _, bottom = resolved
        height = bottom - top
        if height <= 0:
            return "invalid_height"

        visible_top = top
        visible_bottom = bottom
        if isinstance(safe_top, int):
            visible_top = max(visible_top, safe_top + margin)
        if isinstance(safe_bottom, int):
            visible_bottom = min(visible_bottom, safe_bottom - margin)

        visible_height = max(0, visible_bottom - visible_top)
        visible_ratio = visible_height / height if height else 0.0
        if visible_height < min_visible_height and visible_ratio < min_visible_ratio:
            return "insufficient_visible_area"
        return None

    def _click_bounds_center(self, bounds) -> bool:
        resolved = self._bounds_to_list(bounds)
        if not resolved:
            return False

        left, top, right, bottom = resolved
        self.device.click((left + right) // 2, (top + bottom) // 2)
        return True


class ObservationIdMixin(BoundsUiMixin):
    """Run-local UI observation identifiers for weakly identified rows."""

    def _build_page_observation_id(
        self,
        raw_text,
        type_hint,
        bounds,
        page_index,
        page_row_index,
    ) -> str:
        resolved = self._bounds_to_list(bounds)
        if resolved:
            bounds_part = ",".join(str(v) for v in resolved)
        else:
            bounds_part = str(bounds)

        raw_hash = hashlib.sha1(((raw_text or "").strip()).encode("utf-8")).hexdigest()[:12]
        payload = "|".join(
            [
                f"page={page_index}",
                f"row={page_row_index}",
                f"type={type_hint}",
                f"bounds={bounds_part}",
                f"raw={raw_hash}",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class UiStateMixin(XmlUiMixin, ObservationIdMixin):
    """Composite UI helper mixin for app collectors."""
