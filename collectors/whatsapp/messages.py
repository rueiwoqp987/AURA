import hashlib
import re

from collectors.whatsapp.mixin_base import WhatsAppCollectorDeps
from collectors.whatsapp.xpath import (
    CHAT_NAME_FALLBACK_XPATH,
    CHAT_NAME_PRIMARY_XPATH,
    NAV_BLACKLIST,
)


class WhatsAppMessagesMixin(WhatsAppCollectorDeps):
    _TIME_PREFIX_RE = re.compile(r"^\d{1,2}:\d{2}")

    def _build_chat_id(self, chat_name: str) -> str:
        return hashlib.sha256(chat_name.encode("utf-8")).hexdigest()

    def _is_valid_chat_name(self, text: str) -> bool:
        if not text:
            return False
        if text in NAV_BLACKLIST:
            return False
        if self._TIME_PREFIX_RE.match(text):
            return False
        if len(text) < 2:
            return False
        return True

    def _extract_chat_names(self) -> list[str]:
        names: list[str] = []
        seen = set()

        for node in self.device.xpath(CHAT_NAME_PRIMARY_XPATH).all():
            text = (node.text or "").strip()
            if not self._is_valid_chat_name(text):
                continue
            if text in seen:
                continue
            seen.add(text)
            names.append(text)
        if names:
            return names

        for node in self.device.xpath(CHAT_NAME_FALLBACK_XPATH).all():
            text = (node.text or "").strip()
            if not self._is_valid_chat_name(text):
                continue
            if text in seen:
                continue
            seen.add(text)
            names.append(text)
        return names
