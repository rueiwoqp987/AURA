from abc import ABC, abstractmethod


class BaseMethodEngine(ABC):
    method_id = "UNKNOWN"

    @abstractmethod
    def run(self, adapter, device, run_root, target_dir, audit_log_path, profile):
        raise NotImplementedError
