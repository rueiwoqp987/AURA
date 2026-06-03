import re
import datetime
import hashlib
import logging
from collectors.telegram.mixin_base import TelegramCollectorDeps

logger = logging.getLogger(__name__)

FILE_ATTACHMENT_RE = re.compile(
    r"""(?ix)
    \b
    (?P<name>[^,\n]+?\.[a-z0-9]{1,10})
    \s*,\s*
    (?P<size>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|MB|GB|TB)
    \b
    """
)

FILE_NAME_LINE_RE = re.compile(r"(?iu)^(?P<name>[^,\n]+?\.[a-z0-9]{1,10})$")

FILE_SIZE_LINE_RE = re.compile(
    r"""(?ix)
    ^
    (?P<size>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|MB|GB|TB)
    (?:\s+(?P<display_type>[A-Z0-9]{1,12}))?
    $
    """
)

FILE_SIZE_TOKEN_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\b")

PHOTO_ATTACHMENT_RE = re.compile(r"(?i)^Photo(?:\b|,)")

VIDEO_ATTACHMENT_RE = re.compile(
    r"""(?ix)
    ^Video,
    \s*(?P<duration>[^,\n]+?)
    (?:,\s*(?P<size>[\d\.]+\s*(?:KB|MB|GB|TB)))?
    \s*$
    """
)

VIDEO_SIMPLE_ATTACHMENT_RE = re.compile(r"(?i)^Video(?:\b|,)")

TIME_TOKEN_RE = re.compile(r'(\d{1,2}:\d{2}(?:\s*(?:AM|PM))?)', re.I)
ATTACHMENT_TYPES = ("Photo", "Video", "File")

DEFAULT_ATTACHMENT_IDENTITY_POLICY = {
    "photo_no_caption": "observation_first",
    "photo_with_caption": "logical_with_observation",
    "video_no_caption": "observation_first",
    "video_with_caption": "logical_with_observation",
    "file": "logical_candidate_hash_final",
}

class TelegramMessagesMixin(TelegramCollectorDeps):
    def _size_to_bytes(self, size_str):
        if not size_str:
            return None
        m = re.match(r"([\d\.]+)\s*(b|kb|mb|gb|tb)", size_str.strip(), re.I)
        if not m:
            return None
        value = float(m.group(1))
        unit = m.group(2).lower()
        multiplier = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}.get(unit, 1)
        return int(value * multiplier)

    def _strip_message_meta_lines(self, msg_text):
        normalized = (msg_text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if not normalized:
            return []

        lines = normalized.split("\n")
        time_line_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if TIME_TOKEN_RE.search(lines[i]):
                time_line_idx = i
                break

        if time_line_idx is not None:
            return lines[:time_line_idx]
        return lines

    def _split_attachment_text(self, msg_text):
        normalized = (msg_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return "", ""

        lines = normalized.split("\n")
        header = (lines[0] or "").strip()
        caption = "\n".join(line.strip() for line in lines[1:] if line.strip()).strip()
        return header, caption

    def _peek_message_type(self, raw_msg):
        body_lines = self._strip_message_meta_lines(raw_msg)
        if not body_lines:
            return "Text"

        body_text = "\n".join(line for line in body_lines if line is not None).strip()
        if not body_text:
            return "Text"

        if not self._looks_like_attachment_body(body_text):
            return "Text"

        classified = self._classify_message_type(body_text)
        if isinstance(classified, tuple):
            return classified[0]
        return classified

    def _looks_like_attachment_body(self, text):
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return False

        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        if not lines:
            return False

        if self._is_attachment_header_line(lines[0]):
            return True

        return self._parse_multiline_file_attachment(lines) is not None

    def _is_attachment_header_line(self, text):
        header = (text or "").strip()
        if not header:
            return False
        if PHOTO_ATTACHMENT_RE.search(header):
            return True
        if VIDEO_ATTACHMENT_RE.search(header):
            return True
        if VIDEO_SIMPLE_ATTACHMENT_RE.search(header):
            return True
        if FILE_ATTACHMENT_RE.search(header):
            return True
        if FILE_NAME_LINE_RE.search(header):
            return True
        if FILE_SIZE_LINE_RE.search(header):
            return True
        if header.startswith(","):
            return True
        return False

    def _build_file_attachment_detail(self, attachment_name, size_str):
        filename = (attachment_name or "").strip()
        file_type = "File"




        prefixed_match = re.match(
            r"(?i)^(?P<label>[A-Z0-9+_.-]{2,20})\s*file\s*(?P<filename>.+\.[a-z0-9]{1,10})$",
            filename,
        )
        if prefixed_match:
            label = (prefixed_match.group("label") or "").strip()
            extracted_filename = (prefixed_match.group("filename") or "").strip()
            if extracted_filename:
                filename = extracted_filename
            if label:
                file_type = label

        return {
            "filename": filename,
            "type": file_type,
            "size": self._size_to_bytes(size_str),
        }

    def _coalesce_file_name_lines(self, lines):
        cleaned = [str(line or "").strip() for line in lines if str(line or "").strip()]
        if not cleaned:
            return ""

        candidates = []
        candidates.append("".join(cleaned))
        candidates.append(" ".join(cleaned))
        candidates.append(cleaned[-1])

        for candidate in candidates:
            value = candidate.strip().lstrip(",").strip()
            if FILE_NAME_LINE_RE.search(value):
                return value
            label_match = re.search(
                r"(?i)(?P<label>[A-Z0-9+_.-]{2,20})\s*file\s*(?P<filename>[^,\n]+?\.[a-z0-9]{1,10})",
                value,
            )
            if label_match:
                return f"{label_match.group('label')} file{label_match.group('filename')}"
            filename_match = re.search(r"(?iu)(?P<name>[^,\n ]+?\.[a-z0-9]{1,10})", value)
            if filename_match:
                return filename_match.group("name")
        return ""

    def _extract_file_size_from_line(self, line):
        size_match = FILE_SIZE_LINE_RE.search(line or "")
        if size_match:
            return (
                f'{size_match.group("size")} {size_match.group("unit")}',
                (size_match.group("display_type") or "").strip(),
            )

        tokens = FILE_SIZE_TOKEN_RE.findall(line or "")
        if not tokens:
            return None, ""



        value, unit = tokens[-1]
        return f"{value} {unit}", ""

    def _parse_multiline_file_attachment(self, lines):
        cleaned = [str(line or "").strip() for line in lines if str(line or "").strip()]
        for idx, line in enumerate(cleaned):
            size_str, display_type = self._extract_file_size_from_line(line)
            if not size_str:
                continue

            filename = self._coalesce_file_name_lines(cleaned[:idx])
            if not filename:
                continue

            detail = self._build_file_attachment_detail(filename, size_str)
            if display_type:
                detail["display_type"] = display_type

            caption = "\n".join(cleaned[idx + 1:]).strip()
            if caption:
                detail["caption"] = caption
            return detail

        return None

    def _parse_date_marker(self, text, current_date=None):
        stripped = (text or "").strip()
        for fmt in ("%B %d, %Y", "%B %d %Y", "%B %d"):
            try:
                parsed = datetime.datetime.strptime(stripped, fmt)
                if fmt == "%B %d":
                    parsed = parsed.replace(year=datetime.datetime.now().year)
                return parsed.date()
            except Exception:
                continue
        return current_date

    def _classify_message_type(self, msg_text):
        normalized = (msg_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        body_lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        header_line, caption = self._split_attachment_text(normalized)

        if PHOTO_ATTACHMENT_RE.search(header_line or ""):
            detail = {
                "filename": "Photo",
                "type": "Photo",
            }
            if caption:
                detail["caption"] = caption
            return "Photo", detail

        video_match = VIDEO_ATTACHMENT_RE.search(header_line or "")
        if video_match:
            size_bytes = self._size_to_bytes(video_match.group("size"))
            detail = {
                "filename": "Video",
                "type": "Video",
                "duration": (video_match.group("duration") or "").strip(),
                "size": size_bytes,
            }
            if caption:
                detail["caption"] = caption
            return "Video", detail

        if VIDEO_SIMPLE_ATTACHMENT_RE.search(header_line or ""):
            detail = {
                "filename": "Video",
                "type": "Video",
            }
            if caption:
                detail["caption"] = caption
            return "Video", detail

        if "voice" in (header_line or "").lower() and re.search(r'(\d+)\s*(minutes|seconds)\b', header_line or "", re.I):
            return "Voice Message"

        file_match = FILE_ATTACHMENT_RE.search(header_line or "")
        if file_match:
            attachment_name = (file_match.group("name") or "").strip()
            size_str = f'{file_match.group("size")} {file_match.group("unit")}'
            detail = self._build_file_attachment_detail(attachment_name, size_str)
            if caption:
                detail["caption"] = caption
            return "File", detail

        multiline_file_detail = self._parse_multiline_file_attachment(body_lines)
        if multiline_file_detail:
            return "File", multiline_file_detail

        if ("Incomming Call" in (msg_text or "")) or ("Canceled Call" in (msg_text or "")) or ("Missed Call" in (msg_text or "")) or ("Outgoing Call" in (msg_text or "")):
            return "Call"
        return "Text"

    def _build_message_id(self, msg_type, msg_text, timestamp_str, msg_direction, msg_status, sender=None, salt=None):
        sender_part = sender or ""
        ts_part = timestamp_str or ""
        status_part = msg_status or ""
        salt_part = salt or ""
        return hashlib.sha256(
            f"{msg_type}|{msg_text}|{ts_part}|{msg_direction}|{status_part}|{sender_part}|{salt_part}".encode()
        ).hexdigest()

    def _get_attachment_identity_policy(self):
        policy = dict(DEFAULT_ATTACHMENT_IDENTITY_POLICY)
        profile_policy = (getattr(self, "profile", None) or {}).get("attachment_identity_policy") or {}
        if isinstance(profile_policy, dict):
            policy.update(profile_policy)
        return policy

    def _get_attachment_policy_version(self):
        profile = getattr(self, "profile", None) or {}
        return str(profile.get("attachment_identity_policy_version") or "telegram_attachment_identity_v1")

    def _get_attachment_policy_source(self):
        profile = getattr(self, "profile", None) or {}
        app_name = profile.get("app_name") or "Telegram"
        return f"profile:{app_name}.attachment_identity_policy"

    def _attachment_has_caption(self, msg_type, msg_text):
        if msg_type not in ("Photo", "Video"):
            return False
        if not isinstance(msg_text, dict):
            return False
        return bool((msg_text.get("caption") or "").strip())

    def _classify_attachment_identity_policy(self, msg_type, msg_text):
        policy = self._get_attachment_identity_policy()
        if msg_type == "Photo":
            return policy["photo_with_caption"] if self._attachment_has_caption(msg_type, msg_text) else policy["photo_no_caption"]
        if msg_type == "Video":
            return policy["video_with_caption"] if self._attachment_has_caption(msg_type, msg_text) else policy["video_no_caption"]
        if msg_type == "File":
            return policy["file"]
        return "logical"

    def _identity_status_for_policy(self, msg_type, dedup_policy):
        if dedup_policy == "observation_first":
            return "weak_ui_identity"
        if dedup_policy == "logical_with_observation":
            return "captioned_media_identity"
        if dedup_policy == "logical_candidate_hash_final":
            return "candidate_file_identity"
        return "strong"

    def _build_attachment_action_observation_key(self, raw_text, type_hint, bounds):
        resolved = self._bounds_to_list(bounds)
        if resolved:
            left, top, right, bottom = resolved
            width = max(1, right - left)
            height = max(1, bottom - top)
            width_bucket = min(32, max(1, round(width / 120)))
            height_bucket = min(32, max(1, round(height / 80)))
            center_y = top + max(0, (bottom - top) // 2)
            top_bucket = max(0, top // 160)
            center_y_bucket = max(0, center_y // 160)
            bounds_part = f"w={width_bucket}|h={height_bucket}|t={top_bucket}|cy={center_y_bucket}"
        else:
            bounds_part = str(bounds)

        raw_hash = hashlib.sha1(((raw_text or "").strip()).encode("utf-8")).hexdigest()[:12]
        payload = "|".join(
            [
                f"type={type_hint}",
                f"bounds={bounds_part}",
                f"raw={raw_hash}",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _should_suppress_attachment_redownload(self, message_id, dedup_policy, downloaded_ids):
        if not message_id:
            return False
        if dedup_policy == "observation_first":
            return False
        return bool(downloaded_ids and message_id in downloaded_ids)

    def _build_attachment_download_gate_key(self, logical_message_id, observation_id, dedup_policy, attachment_action_key=None):
        if dedup_policy == "observation_first":
            return observation_id or attachment_action_key or logical_message_id
        if dedup_policy == "logical_with_observation":
            return logical_message_id
        if dedup_policy == "logical_candidate_hash_final":
            return f"{logical_message_id}|action={attachment_action_key}" if attachment_action_key else logical_message_id
        return logical_message_id

    def _build_record_id(self, logical_message_id, observation_id, dedup_policy):
        if dedup_policy in ("observation_first", "logical_candidate_hash_final"):
            return observation_id or logical_message_id
        return logical_message_id

    def _attachment_candidate_artifacts(
        self,
        *,
        msg_type,
        msg_text,
        message_id,
        observation_id,
        record_id,
        bounds,
        page_index,
        page_row_index,
        dedup_policy,
        identity_status,
        download_gate_key,
        policy_source,
        policy_version,
    ):
        artifacts = [
            f"message_id={message_id or ''}",
            f"observation_id={observation_id or ''}",
            f"record_id={record_id or ''}",
            f"msg_type={msg_type or ''}",
            f"page_index={page_index if page_index is not None else ''}",
            f"page_row_index={page_row_index if page_row_index is not None else ''}",
            f"bounds={bounds}",
            f"dedup_policy={dedup_policy or ''}",
            f"identity_status={identity_status or ''}",
            f"download_gate_key={download_gate_key or ''}",
            f"policy_source={policy_source or ''}",
            f"policy_version={policy_version or ''}",
        ]

        if isinstance(msg_text, dict):
            filename = msg_text.get("filename")
            caption = msg_text.get("caption")
            size = msg_text.get("size")
            display_type = msg_text.get("display_type")
            if filename:
                artifacts.append(f"display_filename={filename}")
            if size is not None:
                artifacts.append(f"display_size={size}")
            if display_type:
                artifacts.append(f"display_type={display_type}")
            artifacts.append(f"has_caption={bool((caption or '').strip())}")

        return artifacts

    def _log_attachment_candidate_identified(
        self,
        *,
        msg_type,
        msg_text,
        message_id,
        observation_id,
        record_id,
        bounds,
        page_index,
        page_row_index,
        dedup_policy,
        identity_status,
        download_gate_key,
        policy_source,
        policy_version,
    ):
        self.log_action(
            "attachment_candidate_identified",
            selector=str(msg_type),
            artifacts=self._attachment_candidate_artifacts(
                msg_type=msg_type,
                msg_text=msg_text,
                message_id=message_id,
                observation_id=observation_id,
                record_id=record_id,
                bounds=bounds,
                page_index=page_index,
                page_row_index=page_row_index,
                dedup_policy=dedup_policy,
                identity_status=identity_status,
                download_gate_key=download_gate_key,
                policy_source=policy_source,
                policy_version=policy_version,
            ),
        )

    def _normalize_timestamp(self, current_date, time_str):
        if not time_str:
            return ""
        cleaned = (time_str or "").strip()
        formats = ["%I:%M %p", "%H:%M", "%I:%M:%S %p", "%H:%M:%S"]
        for fmt in formats:
            try:
                t = datetime.datetime.strptime(cleaned, fmt).time()
                if current_date:
                    return datetime.datetime.combine(current_date, t).strftime("%Y-%m-%d %H:%M:%S")
                return t.strftime("%H:%M:%S")
            except Exception:
                continue
        try:
            dt = datetime.datetime.fromisoformat(cleaned)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return cleaned

    def _resolve_dm_sender(self, direction, peer_name=None):
        normalized = (direction or "").strip().lower()
        if normalized in ("sent", "outgoing"):
            return "Me"
        if normalized in ("received", "incoming"):
            return peer_name or "Unknown"
        if normalized == "system":
            return "System"
        return peer_name or "Unknown"

    def process_message_dm(self, msg, bounds=None, current_date=None, sender_ctx=None, seen_ids=None, pending_ids=None, downloaded_ids=None, download_dir=None, message_id_salt=None, observation_id=None, page_index=None, page_row_index=None, downloaded_attachment_keys=None, attachment_action_key=None):
        msg = (msg or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        msg_type = 'Text'
        msg_text = msg_time = msg_direction = msg_status = None
        if downloaded_ids is None:
            downloaded_ids = set()
        if downloaded_attachment_keys is None:
            downloaded_attachment_keys = set()

        if '\n' not in msg:
            msg_type = 'System'
            msg_text = msg
            msg_direction = 'System'
            msg_status = None
            new_date = self._parse_date_marker(msg_text, current_date)

            if new_date != current_date:
                return None, new_date, sender_ctx
            current_date = new_date
        else:
            parts = msg.rsplit('\n', 1)
            if len(parts) == 2:
                msg_text, msg_meta = parts
            else:
                msg_text = msg
                msg_meta = ""

            if ',' in msg_meta:
                msg_time1, msg_status = msg_meta.split(',', 1)
                msg_status = msg_status.strip()
            else:
                msg_time1 = msg_meta

            msg_time1 = (msg_time1 or "").strip()
            m = re.search(r'^(?P<dir>.*?)\s+at\s+(?P<time>\d{1,2}:\d{2}(?:\s*(?:AM|PM))?)\s*$', msg_time1, re.I)
            if m:
                msg_direction = (m.group("dir") or "").strip() or "Unknown"
                msg_time = (m.group("time") or "").strip()
            else:

                time_m = re.search(r'(\d{1,2}:\d{2}(?:\s*(?:AM|PM))?)', msg_time1, re.I)
                if time_m:
                    msg_time = time_m.group(1).strip()
                    msg_direction = msg_time1[:time_m.start()].replace("at", "").strip() or "Unknown"
                else:
                    msg_direction = msg_time1 or "Unknown"
                    msg_time = ""

            classified = self._classify_message_type(msg_text)
            if isinstance(classified, tuple):
                msg_type, msg_text = classified
            else:
                msg_type = classified

        timestamp_str = self._normalize_timestamp(current_date, msg_time)
        message_id = self._build_message_id(msg_type, msg_text, msg_time, msg_direction, msg_status, sender_ctx, salt=message_id_salt)
        dedup_policy = "logical"
        identity_status = "strong"
        download_gate_key = message_id
        record_id = message_id
        policy_source = ""
        policy_version = ""
        seen_gate_key = message_id
        pending_gate_key = message_id

        if msg_type in ATTACHMENT_TYPES:
            dedup_policy = self._classify_attachment_identity_policy(msg_type, msg_text)
            identity_status = self._identity_status_for_policy(msg_type, dedup_policy)
            download_gate_key = self._build_attachment_download_gate_key(message_id, observation_id, dedup_policy, attachment_action_key)
            record_id = self._build_record_id(message_id, observation_id, dedup_policy)
            policy_source = self._get_attachment_policy_source()
            policy_version = self._get_attachment_policy_version()
            seen_gate_key = record_id if dedup_policy in ("observation_first", "logical_candidate_hash_final") else message_id
            pending_gate_key = seen_gate_key

        if seen_ids and seen_gate_key in seen_ids:
            return None, current_date, sender_ctx
        if pending_ids and pending_gate_key in pending_ids:
            return None, current_date, sender_ctx

        parsed_downloaded = False
        artifacts = []
        if msg_type in ATTACHMENT_TYPES:
            self._log_attachment_candidate_identified(
                msg_type=msg_type,
                msg_text=msg_text,
                message_id=message_id,
                observation_id=observation_id,
                record_id=record_id,
                bounds=bounds,
                page_index=page_index,
                page_row_index=page_row_index,
                dedup_policy=dedup_policy,
                identity_status=identity_status,
                download_gate_key=download_gate_key,
                policy_source=policy_source,
                policy_version=policy_version,
            )

        if msg_type in ATTACHMENT_TYPES and bounds:
            if self._should_suppress_attachment_redownload(message_id, dedup_policy, downloaded_ids):
                parsed_downloaded = True
                self.log_action(
                    "attachment_download_suppressed",
                    selector="message_id_seen",
                    result="skipped_downloaded_message_id",
                    artifacts=[
                        f"message_id={message_id or ''}",
                        f"observation_id={observation_id or ''}",
                        f"record_id={record_id or ''}",
                        f"download_gate_key={download_gate_key or ''}",
                        f"dedup_policy={dedup_policy or ''}",
                        f"identity_status={identity_status or ''}",
                        f"page_index={page_index if page_index is not None else ''}",
                        f"page_row_index={page_row_index if page_row_index is not None else ''}",
                    ],
                )
            elif download_gate_key not in downloaded_attachment_keys:
                ret = self._download_attachment(
                    msg_text,
                    msg_type,
                    bounds,
                    download_dir,
                    message_id=message_id,
                    observation_id=observation_id,
                    record_id=record_id,
                    page_index=page_index,
                    page_row_index=page_row_index,
                    identity_status=identity_status,
                    dedup_policy=dedup_policy,
                    download_gate_key=download_gate_key,
                    policy_source=policy_source,
                    policy_version=policy_version,
                )
                downloaded_attachment_keys.add(download_gate_key)
                parsed_downloaded = bool(ret)
                if ret:
                    if isinstance(ret, list):
                        artifacts.extend(ret)
                    else:
                        artifacts.append(ret)
            else:
                parsed_downloaded = True
                self.log_action(
                    "attachment_download_suppressed",
                    selector="download_gate_seen",
                    result="skipped_duplicate_download_gate",
                    artifacts=[
                        f"message_id={message_id or ''}",
                        f"observation_id={observation_id or ''}",
                        f"record_id={record_id or ''}",
                        f"download_gate_key={download_gate_key or ''}",
                        f"dedup_policy={dedup_policy or ''}",
                        f"identity_status={identity_status or ''}",
                        f"page_index={page_index if page_index is not None else ''}",
                        f"page_row_index={page_row_index if page_row_index is not None else ''}",
                    ],
                )

        return {
            "record_id": record_id,
            "message_id": message_id,
            "observation_id": observation_id,
            "type": msg_type,
            "sender": self._resolve_dm_sender(msg_direction, sender_ctx),
            "timestamp": timestamp_str,
            "content": msg_text,
            "status": msg_status,
            "direction": msg_direction,
            "raw": msg,
            "page_index": page_index,
            "page_row_index": page_row_index,
            "dedup_policy": dedup_policy,
            "download_gate_key": download_gate_key,
            "identity_status": identity_status,
            "policy_source": policy_source,
            "policy_version": policy_version,
            "attachment_candidate": msg_type in ATTACHMENT_TYPES,
            "artifacts": artifacts,
            "downloaded": parsed_downloaded,
            "bounds": bounds,
        }, current_date, sender_ctx

    def process_message_group(self, msg, bounds=None, current_date=None, current_sender=None, seen_ids=None, pending_ids=None, downloaded_ids=None, download_dir=None, message_id_salt=None, observation_id=None, page_index=None, page_row_index=None, downloaded_attachment_keys=None, attachment_action_key=None):
        msg = (msg or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        msg_type = 'Text'
        msg_text = msg_time = msg_direction = msg_status = None
        sender = None
        if downloaded_ids is None:
            downloaded_ids = set()
        if downloaded_attachment_keys is None:
            downloaded_attachment_keys = set()

        lines = msg.split('\n')
        lines = [l for l in lines if l is not None]
        if len(lines) == 1:
            msg_type = 'System'
            msg_text = lines[0]
            new_date = self._parse_date_marker(msg_text, current_date)
            if new_date != current_date:
                return None, new_date, None
            system_id = self._build_message_id(msg_type, msg_text, "", "System", None, "System", salt=message_id_salt)
            parsed = {
                "record_id": system_id,
                "message_id": system_id,
                "observation_id": observation_id,
                "type": msg_type,
                "sender": "System",
                "timestamp": "",
                "content": msg_text,
                "status": None,
                "direction": "System",
                "raw": msg,
                "page_index": page_index,
                "page_row_index": page_row_index,
                "dedup_policy": "logical",
                "download_gate_key": system_id,
                "identity_status": "strong",
                "policy_source": "",
                "policy_version": "",
                "attachment_candidate": False,
                "artifacts": [],
            }
            if seen_ids and parsed["message_id"] in seen_ids:
                return None, current_date, None
            if pending_ids and parsed["message_id"] in pending_ids:
                return None, current_date, None
            return parsed, current_date, None

        msg_meta = ""
        extra_meta = []
        body_lines = lines[:]
        time_line_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if TIME_TOKEN_RE.search(lines[i]):
                msg_meta = lines[i]
                time_line_idx = i
                break
        if time_line_idx is not None:
            body_lines = lines[:time_line_idx]
            extra_meta = lines[time_line_idx + 1 :]
        else:
            msg_meta = lines[-1]
            body_lines = lines[:-1]

        if body_lines:
            first_body = body_lines[0].strip()
            attachment_header = self._is_attachment_header_line(first_body)
        else:
            attachment_header = False

        if attachment_header:
            sender = None
            msg_text = "\n".join(body_lines).strip()
        elif len(body_lines) > 1:
            sender = body_lines[0].strip()
            msg_text = "\n".join(body_lines[1:]).strip()
        else:
            msg_text = body_lines[0].strip() if body_lines else ""

        msg_status = msg_meta.strip()
        msg_direction = 'Incoming'

        time_match = TIME_TOKEN_RE.search(msg_status)
        if time_match:
            msg_time = time_match.group(1).strip()

        status_flag = None
        if re.search(r'\bNot\s*Seen\b', msg_status, re.I):
            status_flag = 'Not Seen'
        elif re.search(r'\bSeen\b', msg_status, re.I):
            status_flag = 'Seen'

        view_count = None
        for em in extra_meta:
            m = re.search(r'Viewed\s+(\d+)\s+times', em, re.I)
            if m:
                view_count = m.group(1)
                break
        if view_count:
            msg_status = f"{msg_status}; Viewed {view_count} times"

        if status_flag:
            msg_direction = 'Outgoing'
        elif 'Received at' in msg_status:
            msg_direction = 'Incoming'
        else:
            msg_direction = 'Unknown'

        if sender is None and msg_direction == 'Incoming':
            sender = current_sender
        if sender is None and msg_direction == 'Outgoing':
            sender = 'Me'
        if sender:
            current_sender = sender

        if status_flag:
            status_flag_lower = status_flag.lower()
            if msg_text.lower().startswith(status_flag_lower):
                msg_text = msg_text[len(status_flag):].strip(", ").strip()

        classified = self._classify_message_type(msg_text)
        if isinstance(classified, tuple):
            msg_type, msg_text = classified
        else:
            msg_type = classified

        timestamp_str = self._normalize_timestamp(current_date, msg_time)
        message_id = self._build_message_id(msg_type, msg_text, msg_time, msg_direction, msg_status, sender, salt=message_id_salt)
        dedup_policy = "logical"
        identity_status = "strong"
        download_gate_key = message_id
        record_id = message_id
        policy_source = ""
        policy_version = ""
        seen_gate_key = message_id
        pending_gate_key = message_id

        if msg_type in ATTACHMENT_TYPES:
            dedup_policy = self._classify_attachment_identity_policy(msg_type, msg_text)
            identity_status = self._identity_status_for_policy(msg_type, dedup_policy)
            download_gate_key = self._build_attachment_download_gate_key(message_id, observation_id, dedup_policy, attachment_action_key)
            record_id = self._build_record_id(message_id, observation_id, dedup_policy)
            policy_source = self._get_attachment_policy_source()
            policy_version = self._get_attachment_policy_version()
            seen_gate_key = record_id if dedup_policy in ("observation_first", "logical_candidate_hash_final") else message_id
            pending_gate_key = seen_gate_key

        if seen_ids and seen_gate_key in seen_ids:
            return None, current_date, current_sender
        if pending_ids and pending_gate_key in pending_ids:
            return None, current_date, current_sender

        parsed_downloaded = False
        artifacts = []
        if msg_type in ATTACHMENT_TYPES:
            self._log_attachment_candidate_identified(
                msg_type=msg_type,
                msg_text=msg_text,
                message_id=message_id,
                observation_id=observation_id,
                record_id=record_id,
                bounds=bounds,
                page_index=page_index,
                page_row_index=page_row_index,
                dedup_policy=dedup_policy,
                identity_status=identity_status,
                download_gate_key=download_gate_key,
                policy_source=policy_source,
                policy_version=policy_version,
            )

        if msg_type in ATTACHMENT_TYPES and bounds:
            if self._should_suppress_attachment_redownload(message_id, dedup_policy, downloaded_ids):
                parsed_downloaded = True
                self.log_action(
                    "attachment_download_suppressed",
                    selector="message_id_seen",
                    result="skipped_downloaded_message_id",
                    artifacts=[
                        f"message_id={message_id or ''}",
                        f"observation_id={observation_id or ''}",
                        f"record_id={record_id or ''}",
                        f"download_gate_key={download_gate_key or ''}",
                        f"dedup_policy={dedup_policy or ''}",
                        f"identity_status={identity_status or ''}",
                        f"page_index={page_index if page_index is not None else ''}",
                        f"page_row_index={page_row_index if page_row_index is not None else ''}",
                    ],
                )
            elif download_gate_key not in downloaded_attachment_keys:
                ret = self._download_attachment(
                    msg_text,
                    msg_type,
                    bounds,
                    download_dir,
                    message_id=message_id,
                    observation_id=observation_id,
                    record_id=record_id,
                    page_index=page_index,
                    page_row_index=page_row_index,
                    identity_status=identity_status,
                    dedup_policy=dedup_policy,
                    download_gate_key=download_gate_key,
                    policy_source=policy_source,
                    policy_version=policy_version,
                )
                downloaded_attachment_keys.add(download_gate_key)
                parsed_downloaded = bool(ret)
                if ret:
                    if isinstance(ret, list):
                        artifacts.extend(ret)
                    else:
                        artifacts.append(ret)
            else:
                parsed_downloaded = True
                self.log_action(
                    "attachment_download_suppressed",
                    selector="download_gate_seen",
                    result="skipped_duplicate_download_gate",
                    artifacts=[
                        f"message_id={message_id or ''}",
                        f"observation_id={observation_id or ''}",
                        f"record_id={record_id or ''}",
                        f"download_gate_key={download_gate_key or ''}",
                        f"dedup_policy={dedup_policy or ''}",
                        f"identity_status={identity_status or ''}",
                        f"page_index={page_index if page_index is not None else ''}",
                        f"page_row_index={page_row_index if page_row_index is not None else ''}",
                    ],
                )

        return {
            "record_id": record_id,
            "message_id": message_id,
            "observation_id": observation_id,
            "type": msg_type,
            "sender": sender or "Unknown",
            "timestamp": timestamp_str,
            "content": msg_text,
            "status": msg_status,
            "direction": msg_direction,
            "raw": msg,
            "page_index": page_index,
            "page_row_index": page_row_index,
            "dedup_policy": dedup_policy,
            "download_gate_key": download_gate_key,
            "identity_status": identity_status,
            "policy_source": policy_source,
            "policy_version": policy_version,
            "attachment_candidate": msg_type in ATTACHMENT_TYPES,
            "artifacts": artifacts,
            "downloaded": parsed_downloaded,
            "bounds": bounds,
        }, current_date, current_sender
