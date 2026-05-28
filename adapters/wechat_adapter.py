from collectors.wechat.collector import WeChatCollector

from .base import BaseAppAdapter


class WeChatAdapter(BaseAppAdapter):
    app_key = "wechat"

    def create_s3_collector(self, device, target_dir, audit_log_path):
        runtime = self.profile.get("_runtime", {})
        return WeChatCollector(
            device=device,
            artifact_dir=str(target_dir),
            audit_log_path=str(audit_log_path),
            profile=self.profile,
            serial=runtime.get("serial"),
        )
