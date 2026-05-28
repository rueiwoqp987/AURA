from __future__ import annotations

from pathlib import Path
from typing import Any

from collectors.common.ui_state import UiStateMixin


class TelegramCollectorDeps(UiStateMixin):
    """Type-only dependency surface shared by Telegram mixins."""

    device: Any
    storage: Any
    app_id: str
    packageName: str
    artifact_dir: Path
    profile: dict[str, Any]
    input_bound: Any

    accounts: list[dict[str, Any] | str]
    user_profiles: dict[str, Any]
    contacts: dict[str, Any]
    targets: dict[str, Any]
    completed_targets: dict[str, Any]
    chatrooms: dict[str, Any]
    chatroom_list_bounds: tuple[int | None, int | None]
    _ambiguous_deleted_account_counter: int

    download_path: str
    download_path_candidates: list[str]
    origin_download_path: str | None
    pictures_path: str
    origin_pictures_path: str | None
    telegram_attachment_snapshot_dirs: dict[str, list[str]]
    telegram_download_backup_dirs: list[str]
    telegram_download_backup_root: str
    telegram_download_backups: list[dict[str, str]]

    _sleep: Any
    wait_for_screen_state: Any
    wait_for_list_changed: Any
    safe_click: Any
    log_action: Any
    register_artifact: Any
    capture_visual_evidence: Any
    aura_prefix: Any
    run_id: str
    current_phase: str | None
    current_account: str | None
    current_chat_id: str | None
    current_message_id: str | None

    # Cross-mixin methods used by other mixins.
    init_download_path: Any
    restore_download_path: Any
    get_user_account_from_xml: Any
    _get_account_name_and_bounds: Any
    _click_bounds_center: Any
    _chatroom_list_bounds: Any
    _is_clickable_chatroom_row: Any
    _is_telegram_chat_list_screen: Any
    _is_telegram_chatroom_screen: Any
    _is_telegram_settings_screen: Any
    _return_to_chat_list: Any
    collect_user_account: Any
    select_user_account: Any
    get_telegram_profile_identifiers: Any
    collect_user_profile: Any
    collect_contacts: Any
    collect_chatrooms: Any

    get_chatroom_list: Any
    check_chatroom_type: Any
    gen_chatroom_id: Any
    _is_blank: Any
    _is_unknown_mobile: Any
    _is_ambiguous_deleted_account: Any
    _make_storage_chatroom_id: Any
    chatroom_go_to_bottom: Any
    process_chatroom_dm: Any
    process_chatroom_group: Any
    process_chatroom_channel: Any
    _process_chatroom_generic: Any

    process_message_dm: Any
    process_message_group: Any
    _peek_message_type: Any
    _build_attachment_action_observation_key: Any
    _build_message_id: Any
    _classify_message_type: Any
    _normalize_timestamp: Any
    _parse_date_marker: Any

    _size_to_bytes: Any
    _download_attachment: Any
