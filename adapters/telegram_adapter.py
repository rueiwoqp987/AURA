from collectors.telegram.collector import TelegramCollector

from .base import BaseAppAdapter


class TelegramAdapter(BaseAppAdapter):
    app_key = "telegram"

    def create_s1_collector(self, device, target_dir, audit_log_path):
        runtime = self.profile.get("_runtime", {})
        return TelegramCollector(
            device=device,
            artifact_dir=str(target_dir),
            audit_log_path=str(audit_log_path),
            profile=self.profile,
            serial=runtime.get("serial"),
        )
