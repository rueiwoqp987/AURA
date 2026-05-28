from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zipfile import ZipFile


_TS_PREFIX_RE = re.compile(
    r"^"
    r"(?P<date>\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4})"
    r",\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)"
    r"(?:\s*(?P<ampm>AM|PM))?"
    r"\s+-\s+"
    r"(?P<body>.*)"
    r"$",
    re.IGNORECASE,
)
_ATTACHED_ANGLE_RE = re.compile(r"^<attached:\s*(?P<name>.+?)>$", re.IGNORECASE)
_ATTACHED_SUFFIX_RE = re.compile(r"^(?P<name>.+?)\s+\((?:file attached|attached file)\)$", re.IGNORECASE)

_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".webm"}

def _parse_timestamp(date_str: str, time_str: str, ampm: str | None) -> str:
    raw_date = (date_str or "").strip()
    raw_time = (time_str or "").strip()
    raw_ampm = (ampm or "").strip().upper()
    if raw_ampm:
        raw_time = f"{raw_time} {raw_ampm}"

    date_fmts = (
        "%m/%d/%y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%d/%m/%Y",
        "%m-%d-%y",
        "%m-%d-%Y",
        "%d-%m-%y",
        "%d-%m-%Y",
        "%m.%d.%y",
        "%m.%d.%Y",
        "%d.%m.%y",
        "%d.%m.%Y",
    )
    time_fmts = ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S")

    for df in date_fmts:
        for tf in time_fmts:
            try:
                dt = datetime.strptime(f"{raw_date} {raw_time}", f"{df} {tf}")
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

    return f"{raw_date} {raw_time}".strip()

def _build_message_id(chat_id: str, raw_block: str) -> str:
    return hashlib.sha256(f"{chat_id}|{raw_block}".encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True)
class ParsedExport:
    messages: list[dict]
    attachments: list[dict]
    source_txt_name: str | None = None


def _normalize_attachment_name(name: str) -> str:
    return Path((name or "").strip().replace("\\", "/")).name.strip().lower()


def _infer_attachment_type(file_name: str) -> str:
    ext = Path(file_name or "").suffix.lower()
    if ext in _PHOTO_EXTS:
        return "Photo"
    if ext in _VIDEO_EXTS:
        return "Video"
    return "File"


def _extract_attachment_name(content: str) -> str | None:
    text = (content or "").strip()
    if not text:
        return None
    candidates = [text]
    first_line = text.splitlines()[0].strip() if text.splitlines() else text
    if first_line and first_line not in candidates:
        candidates.insert(0, first_line)
    for candidate in candidates:
        for pattern in (_ATTACHED_ANGLE_RE, _ATTACHED_SUFFIX_RE):
            m = pattern.match(candidate)
            if m:
                return (m.group("name") or "").strip()
    return None

def parse_whatsapp_export_text(chat_id: str, lines: Iterable[str], *, source_path: str) -> ParsedExport:
    out: list[dict] = []

    current_block_lines: list[str] = []
    current_ts = ""
    current_sender = ""
    current_content = ""
    current_raw = ""
    current_type = "Text"

    def flush_current() -> None:
        nonlocal current_block_lines, current_ts, current_sender, current_content, current_raw, current_type
        if not current_block_lines:
            return
        raw_block = "\n".join(current_block_lines).rstrip("\n")
        out.append(
            {
                "message_id": _build_message_id(chat_id, raw_block),
                "type": current_type,
                "sender": current_sender or "System",
                "timestamp": current_ts,
                "content": current_content,
                "raw": current_raw or raw_block,
                "artifacts": [{"kind": "whatsapp_export", "path": source_path}],
                "screenshot_paths": [],
                "uitree_paths": [],
            }
        )
        current_block_lines = []
        current_ts = ""
        current_sender = ""
        current_content = ""
        current_raw = ""
        current_type = "Text"

    for raw_line in lines:
        line = (raw_line or "").rstrip("\n")
        m = _TS_PREFIX_RE.match(line)
        if m:
            flush_current()
            date_str = m.group("date") or ""
            time_str = m.group("time") or ""
            ampm = m.group("ampm")
            body = (m.group("body") or "").strip()

            ts = _parse_timestamp(date_str, time_str, ampm)
            sender = "System"
            content = body
            msg_type = "System"

            if ": " in body:
                maybe_sender, maybe_content = body.split(": ", 1)
                if maybe_sender and maybe_content:
                    sender = maybe_sender.strip()
                    content = maybe_content
                    msg_type = "Text"

            if "<Media omitted>" in content or content.strip().lower() in {"<media omitted>", "media omitted"}:
                msg_type = "Media"

            current_ts = ts
            current_sender = sender
            current_content = content
            current_raw = line
            current_type = msg_type
            current_block_lines = [line]
            continue

        if not current_block_lines:
            continue

        current_block_lines.append(line)
        current_content = (current_content + "\n" + line).strip("\n")
        current_raw = "\n".join(current_block_lines).rstrip("\n")

    flush_current()
    return ParsedExport(messages=out, attachments=[], source_txt_name=None)

def parse_whatsapp_export_zip(chat_id: str, zip_path: Path) -> ParsedExport:
    zpath = Path(zip_path)
    with ZipFile(str(zpath), "r") as zf:
        all_names = [n for n in zf.namelist() if not n.endswith("/")]
        txt_names = [n for n in all_names if n.lower().endswith(".txt")]
        if not txt_names:
            return ParsedExport(messages=[], attachments=[], source_txt_name=None)
        txt_name = sorted(txt_names)[0]
        raw = zf.read(txt_name)
        try:
            text = raw.decode("utf-8-sig")
        except Exception:
            try:
                text = raw.decode("cp1252")
            except Exception:
                text = raw.decode("latin-1", errors="replace")
        lines = text.splitlines()
        attachment_members = [
            {
                "zip_member": name,
                "file_name": Path(name).name,
                "normalized_name": _normalize_attachment_name(name),
                "message_type": _infer_attachment_type(name),
            }
            for name in all_names
            if name != txt_name
        ]

    parsed = parse_whatsapp_export_text(chat_id, lines, source_path=str(zpath.resolve()))
    messages = [dict(msg) for msg in (parsed.messages or [])]
    attachments: list[dict] = []

    attachments_by_name: dict[str, list[dict]] = {}
    unmatched_queue: list[dict] = []
    for item in attachment_members:
        unmatched_queue.append(item)
        attachments_by_name.setdefault(item["normalized_name"], []).append(item)

    matched_members: set[str] = set()
    for msg in messages:
        content = msg.get("content") or ""
        attached_name = _extract_attachment_name(content)
        matched = None
        if attached_name:
            key = _normalize_attachment_name(attached_name)
            bucket = attachments_by_name.get(key) or []
            if bucket:
                matched = bucket.pop(0)
        elif msg.get("type") == "Media":
            for candidate in unmatched_queue:
                if candidate["zip_member"] not in matched_members:
                    matched = candidate
                    break

        if not matched:
            continue

        matched_members.add(matched["zip_member"])
        msg["type"] = matched["message_type"]
        msg["attachment_file_name"] = matched["file_name"]
        msg["attachment_zip_member"] = matched["zip_member"]
        attachments.append(
            {
                "zip_member": matched["zip_member"],
                "file_name": matched["file_name"],
                "message_id": msg.get("message_id"),
                "record_id": msg.get("record_id", msg.get("message_id")),
                "observation_id": msg.get("observation_id") or f"{msg.get('record_id', msg.get('message_id'))}:primary",
                "message_type": matched["message_type"],
            }
        )

    for item in attachment_members:
        if item["zip_member"] in matched_members:
            continue
        synthetic_message_id = _build_message_id(chat_id, f"attachment|{item['zip_member']}")
        messages.append(
            {
                "message_id": synthetic_message_id,
                "record_id": synthetic_message_id,
                "type": item["message_type"],
                "sender": "System",
                "timestamp": "",
                "content": item["file_name"],
                "raw": item["file_name"],
                "artifacts": [{"kind": "whatsapp_export", "path": str(zpath.resolve())}],
                "screenshot_paths": [],
                "uitree_paths": [],
                "raw_source": "whatsapp_export_zip_member",
            }
        )
        attachments.append(
            {
                "zip_member": item["zip_member"],
                "file_name": item["file_name"],
                "message_id": synthetic_message_id,
                "record_id": synthetic_message_id,
                "observation_id": f"{synthetic_message_id}:primary",
                "message_type": item["message_type"],
            }
        )

    return ParsedExport(messages=messages, attachments=attachments, source_txt_name=txt_name)
