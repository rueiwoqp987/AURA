from __future__ import annotations

from pathlib import Path
from typing import Any

from collectors.common.ui_state import UiStateMixin


class WhatsAppCollectorDeps(UiStateMixin):
    """Type-only dependency surface shared by WhatsApp mixins."""

    device: Any
    storage: Any
    app_id: str
    packageName: str
    artifact_dir: Path
    profile: dict[str, Any]
    serial: str | None
    bluetooth_target_name: str

    run_id: str
    current_phase: str | None
    current_account: str | None
    current_chat_id: str | None
    current_message_id: str | None

    log_action: Any
    capture_visual_evidence: Any
    register_artifact: Any
    aura_prefix: Any
    _sleep: Any
    safe_click: Any
    wait_for_screen_state: Any
    wait_for_list_changed: Any

    _TIME_PREFIX_RE: Any
    _BT_TARGET_SKIP_KEYWORDS: Any

    _tap_chats_tab: Any
    _scroll_to_top: Any
    _click_first_text: Any
    _click_first_desc: Any
    _is_valid_chat_name: Any
    _discover_chatrooms: Any
    _open_chat_by_name: Any
    _back_to_chat_list: Any

    _chat_export_dir: Any
    _export_current_chat: Any
    _collect_exports: Any
    _select_bluetooth_target: Any

    _extract_chat_names: Any
    _build_chat_id: Any
