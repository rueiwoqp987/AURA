from collectors.whatsapp.collector import WhatsAppCollector

from .base import BaseAppAdapter


class WhatsAppAdapter(BaseAppAdapter):
    app_key = "whatsapp"

    def create_s2_collector(self, device, target_dir, audit_log_path):
        runtime = self.profile.get("_runtime", {})
        return WhatsAppCollector(
            device=device,
            artifact_dir=str(target_dir),
            audit_log_path=str(audit_log_path),
            profile=self.profile,
            serial=runtime.get("serial"),
        )
