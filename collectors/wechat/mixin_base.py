from __future__ import annotations

from pathlib import Path
from typing import Any

from collectors.common.ui_state import UiStateMixin


class WeChatCollectorDeps(UiStateMixin):

    device: Any
    storage: Any
    app_id: str
    packageName: str
    artifact_dir: Path
    profile: dict[str, Any]

    log_action: Any
    register_artifact: Any
    capture_visual_evidence: Any
    aura_prefix: Any
    _sleep: Any

    current_phase: str | None
    current_account: str | None
    current_chat_id: str | None
    current_message_id: str | None

    _foreground_package: Any
    _is_app_foreground: Any

    ocr_image: Any
    write_ocr_artifact: Any
