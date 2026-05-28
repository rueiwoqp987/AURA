import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image


_TIME_RE = re.compile(r"^\s*(\d{1,2}[:\.]\d{2})(\s*(AM|PM))?\s*$", flags=re.I)


@dataclass(frozen=True)
class OcrItem:
    text: str
    conf: float
    bbox: list[list[float]]  # 4 points: [[x,y],...]

    @property
    def cx(self) -> float:
        xs = [p[0] for p in self.bbox]
        return float(sum(xs) / max(1, len(xs)))

    @property
    def cy(self) -> float:
        ys = [p[1] for p in self.bbox]
        return float(sum(ys) / max(1, len(ys)))


class OcrEngine:
    name = "base"

    def read(self, image: Image.Image) -> list[OcrItem]:
        raise NotImplementedError


class RapidOcrEngine(OcrEngine):
    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        self._ocr = RapidOCR()

    def read(self, image: Image.Image) -> list[OcrItem]:
        arr = np.array(image.convert("RGB"))
        results, _ = self._ocr(arr)
        items: list[OcrItem] = []
        if not results:
            return items
        for r in results:
            # [[x,y]x4, text, score]
            if not isinstance(r, (list, tuple)) or len(r) < 3:
                continue
            bbox, text, score = r[0], r[1], r[2]
            text = (text or "").strip()
            if not text:
                continue
            try:
                conf = float(score)
            except Exception:
                conf = 0.0
            try:
                pts = [[float(p[0]), float(p[1])] for p in bbox]
            except Exception:
                continue
            if len(pts) != 4:
                continue
            items.append(OcrItem(text=text, conf=conf, bbox=pts))
        return items


class EasyOcrEngine(OcrEngine):
    name = "easyocr"

    def __init__(self, languages: Iterable[str] | None = None):
        import easyocr  # type: ignore

        langs = list(languages or ["en"])
        self._reader = easyocr.Reader(langs, gpu=False, verbose=False)

    def read(self, image: Image.Image) -> list[OcrItem]:
        import numpy as _np

        arr = _np.array(image.convert("RGB"))
        out = self._reader.readtext(arr, detail=1)
        items: list[OcrItem] = []
        for bbox, text, conf in out:
            text = (text or "").strip()
            if not text:
                continue
            try:
                c = float(conf)
            except Exception:
                c = 0.0
            try:
                pts = [[float(p[0]), float(p[1])] for p in bbox]
            except Exception:
                continue
            if len(pts) != 4:
                continue
            items.append(OcrItem(text=text, conf=c, bbox=pts))
        return items


class TesseractOcrEngine(OcrEngine):
    name = "tesseract"

    def __init__(self, lang: str = "eng"):
        import pytesseract  # type: ignore

        self._pytesseract = pytesseract
        self._lang = lang

    def read(self, image: Image.Image) -> list[OcrItem]:
        data = self._pytesseract.image_to_data(image, lang=self._lang, output_type=self._pytesseract.Output.DICT)
        n = len(data.get("text", []))
        items: list[OcrItem] = []
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data.get("conf", ["0"])[i])
            except Exception:
                conf = 0.0
            x = float(data.get("left", [0])[i])
            y = float(data.get("top", [0])[i])
            w = float(data.get("width", [0])[i])
            h = float(data.get("height", [0])[i])
            pts = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
            items.append(OcrItem(text=text, conf=conf, bbox=pts))
        return items

def open_image(image_path: str | Path) -> Image.Image:
    return Image.open(str(image_path)).convert("RGB")

def is_time_like(text: str) -> bool:
    if not text:
        return False
    if _TIME_RE.match(text):
        return True
    lower = text.strip().lower()
    if lower in {"yesterday", "today"}:
        return True
    if lower in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
        return True
    if re.match(r"^\d{1,2}/\d{1,2}(/\d{2,4})?$", lower):
        return True
    return False

def write_ocr_json(path: str | Path, payload: dict[str, Any]) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(p)

def items_to_payload(items: list[OcrItem], *, engine: str, image_path: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "engine": engine,
        "image_path": image_path,
        "meta": meta or {},
        "items": [
            {
                "text": it.text,
                "conf": it.conf,
                "bbox": it.bbox,
                "cx": it.cx,
                "cy": it.cy,
            }
            for it in items
        ],
    }

def pick_ocr_engine(
    preferred: str | None = None,
    *,
    languages: list[str] | None = None,
    tesseract_lang: str | None = None,
) -> OcrEngine:
    order: list[str] = []
    pref = (preferred or "").strip().lower()
    if pref:
        order.append(pref)
    for name in ("rapidocr", "tesseract", "easyocr"):
        if name not in order:
            order.append(name)

    last_err: Exception | None = None
    for name in order:
        try:
            if name == "rapidocr":
                return RapidOcrEngine()
            if name == "tesseract":
                return TesseractOcrEngine(lang=(tesseract_lang or "eng"))
            if name == "easyocr":
                return EasyOcrEngine(languages=languages or ["en"])
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"No OCR engine available (preferred={preferred!r}): {last_err}")

