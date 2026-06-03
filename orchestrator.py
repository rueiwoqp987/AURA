import logging
import time
from pathlib import Path
from typing import Dict, List

from adapters import ADAPTER_REGISTRY
from engines import ENGINE_REGISTRY
from utils.utils import write_audit

logger = logging.getLogger(__name__)


class AURAOrchestrator:
    def __init__(
        self,
        target: str,
        profile: Dict,
        device,
        run_root: Path,
        target_dir: Path,
        audit_log_path: Path,
    ):
        self.target = target
        self.profile = profile
        self.device = device
        self.run_root = run_root
        self.target_dir = target_dir
        self.audit_log_path = audit_log_path

    def _resolve_adapter(self):
        adapter_cls = ADAPTER_REGISTRY.get(self.target)
        if not adapter_cls:
            raise SystemExit(f"No adapter registered for target: {self.target}")
        return adapter_cls(profile=self.profile)

    def _resolve_methods(self) -> List[str]:
        methods = self.profile.get("collection_methods", [])
        if not isinstance(methods, list):
            raise SystemExit("collection_methods must be a list in profile")
        return [m.upper() for m in methods]

    def _summarize_results(self, results: Dict) -> Dict:
        summary = {}
        for method, payload in (results or {}).items():
            if not isinstance(payload, dict):
                summary[method] = payload
                continue

            item = {"status": payload.get("status")}
            phases = payload.get("phases")
            if isinstance(phases, list):
                item["phases"] = [
                    {
                        "phase": p.get("phase"),
                        "status": p.get("status"),
                        "duration_sec": ((p.get("timing") or {}).get("duration_sec")),
                    }
                    for p in phases
                    if isinstance(p, dict)
                ]
            if payload.get("reason") is not None:
                item["reason"] = payload.get("reason")
            summary[method] = item
        return summary

    def _audit(self, action: str, *, selector=None, result="success", artifacts=None, error=None):
        write_audit(
            log_path=self.audit_log_path,
            package_name=self.profile.get("package_name", self.target),
            action=action,
            selector=selector,
            result=result,
            error=error,
            artifacts=list(artifacts or []),
            run_id=self.run_root.name,
            phase="orchestration",
        )

    def run(self):
        adapter = self._resolve_adapter()
        methods = self._resolve_methods()

        logger.info("Orchestrator started: target=%s methods=%s", self.target, methods)
        started_ts = time.time()
        self._audit(
            "orchestrator_start",
            selector=self.target,
            artifacts=[
                f"target={self.target}",
                f"methods={','.join(methods)}",
                f"run_root={self.run_root}",
            ],
        )

        results = {}
        try:
            for method in methods:
                engine = ENGINE_REGISTRY.get(method)
                if not engine:
                    logger.warning("No engine registered for method: %s", method)
                    results[method] = {"status": "skipped", "reason": "engine_not_registered"}
                    self._audit(
                        "method_skip",
                        selector=method,
                        result="skipped",
                        artifacts=["reason=engine_not_registered"],
                    )
                    continue

                logger.info("Running method engine: %s", method)
                method_started_ts = time.time()
                self._audit("method_start", selector=method, artifacts=[f"method={method}"])
                try:
                    results[method] = engine.run(
                        adapter=adapter,
                        device=self.device,
                        run_root=self.run_root,
                        target_dir=self.target_dir,
                        audit_log_path=self.audit_log_path,
                        profile=self.profile,
                    )
                    status = "unknown"
                    if isinstance(results[method], dict):
                        status = str(results[method].get("status") or "unknown")
                    self._audit(
                        "method_end",
                        selector=method,
                        artifacts=[
                            f"method={method}",
                            f"status={status}",
                            f"elapsed={time.time() - method_started_ts:.2f}s",
                        ],
                    )
                except Exception as e:
                    self._audit(
                        "method_end",
                        selector=method,
                        result="fail",
                        error=e,
                        artifacts=[
                            f"method={method}",
                            f"elapsed={time.time() - method_started_ts:.2f}s",
                        ],
                    )
                    raise
        except Exception as e:
            self._audit(
                "orchestrator_end",
                selector=self.target,
                result="fail",
                error=e,
                artifacts=[f"elapsed={time.time() - started_ts:.2f}s"],
            )
            raise

        summary = self._summarize_results(results)
        logger.info("Orchestrator finished: %s", summary)
        self._audit(
            "orchestrator_end",
            selector=self.target,
            artifacts=[
                f"elapsed={time.time() - started_ts:.2f}s",
                f"summary={summary}",
            ],
        )
        return results
