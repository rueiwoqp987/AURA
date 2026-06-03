from abc import ABC


class BaseAppAdapter(ABC):
    app_key = "unknown"

    def __init__(self, profile):
        self.profile = profile or {}

    def create_s1_collector(self, device, target_dir, audit_log_path):
        return None

    def create_s2_collector(self, device, target_dir, audit_log_path):
        return None

    def create_s3_collector(self, device, target_dir, audit_log_path):
        return None
