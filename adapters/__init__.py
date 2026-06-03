from .telegram_adapter import TelegramAdapter
from .wechat_adapter import WeChatAdapter
from .whatsapp_adapter import WhatsAppAdapter

ADAPTER_REGISTRY = {
    "telegram": TelegramAdapter,
    "wechat": WeChatAdapter,
    "whatsapp": WhatsAppAdapter,
}
