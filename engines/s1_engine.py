import logging

from .base import BaseMethodEngine

logger = logging.getLogger(__name__)


class S1Engine(BaseMethodEngine):
    method_id = "S1"

    def run(self, adapter, device, run_root, target_dir, audit_log_path, profile):
        collector = adapter.create_s1_collector(device=device, target_dir=target_dir, audit_log_path=audit_log_path)
        if collector is None:
            logger.info("S1 skipped: adapter %s has no S1 collector", adapter.app_key)
            return {"status": "skipped", "reason": "collector_not_implemented"}

        try:
            ret = collector.collect()
            if isinstance(ret, dict) and ret.get("status") in {"preflight_failed", "failed"}:
                return {
                    "status": "failed",
                    "reason": ret.get("status"),
                    "collector": collector.__class__.__name__,
                    "detail": ret,
                }
            return {"status": "done", "collector": collector.__class__.__name__, "result": ret}
        finally:
            try:
                close_fn = getattr(collector, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                logger.exception("collector close failed: %s", collector.__class__.__name__)
