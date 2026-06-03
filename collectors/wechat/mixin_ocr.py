import logging
from pathlib import Path

from PIL import Image

from utils.ocr import items_to_payload, open_image, pick_ocr_engine, write_ocr_json
from collectors.wechat.mixin_base import WeChatCollectorDeps

logger = logging.getLogger(__name__)


class WeChatOcrMixin(WeChatCollectorDeps):
    def _init_ocr(self) -> None:
        cfg = (self.profile or {}).get("ocr") or {}
        preferred = cfg.get("backend") or cfg.get("engine") or None
        languages = cfg.get("languages")
        tesseract_lang = cfg.get("tesseract_lang")
        self._ocr_engine = pick_ocr_engine(preferred, languages=languages, tesseract_lang=tesseract_lang)
        logger.info("%s OCR ready: engine=%s", self.aura_prefix(self.current_phase or "setup"), self._ocr_engine.name)
        self.log_action("ocr_ready", selector=self._ocr_engine.name)

    def ocr_image(self, image_input) -> list:
        if isinstance(image_input, Image.Image):
            img = image_input.convert("RGB")
        else:
            img = open_image(image_input)
        return self._ocr_engine.read(img)

    def write_ocr_artifact(self, screenshot_path: str, items: list, *, kind: str, meta: dict | None = None) -> str:
        p = Path(screenshot_path)
        out_path = p.with_suffix(p.suffix + ".ocr.json")
        payload = items_to_payload(items, engine=self._ocr_engine.name, image_path=str(p.resolve()), meta=meta or {})
        out_json_path = write_ocr_json(out_path, payload)
        self.register_artifact(out_json_path, kind=kind)
        return out_json_path
