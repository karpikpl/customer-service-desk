"""Feedback telemetry isolated from console, global OTLP exporters, and disk storage."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from pydantic import Field, ValidationError

from service_desk.identity import teams_identity
from service_desk.models import DeskError, Model

EVENT_NAME = "service_desk.feedback"
FeedbackRating = Literal["positive", "negative", "neutral"]


class FeedbackParams(Model):
    rating: FeedbackRating


class NativeFeedback(Model):
    reaction: Literal["like", "dislike"]
    feedback: str = Field(default="{}", max_length=16000)


def parse_native_feedback(activity: dict[str, Any]) -> tuple[FeedbackRating, str, str]:
    try:
        value = activity["value"]
        if activity.get("name") != "message/submitAction" or value["actionName"] != "feedback":
            raise ValueError("Not feedback")
        submission = NativeFeedback.model_validate(value["actionValue"])
        detail = json.loads(submission.feedback or "{}")
        text = detail.get("feedbackText", "")
        response_id = activity.get("replyToId")
        if not isinstance(text, str) or not isinstance(response_id, str) or not response_id:
            raise ValueError("Missing response or invalid text")
        if len(text) > 12000 or len(response_id) > 2048:
            raise ValueError("Feedback exceeds bounds")
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as exc:
        raise DeskError("I could not read this feedback. Please retry the thumbs control.") from exc
    return ("positive" if submission.reaction == "like" else "negative"), text, response_id


_PREFIX = re.compile(r"^(?:feedback|suggestion|feature request)\s*:", re.I)
_ABOUT_REPLY = re.compile(
    r"\b(?:this|that|your|the|last|previous)\s+(?:(?:last|previous)\s+)?"
    r"(?:answer|response|reply|card|agent|bot|assistant|service desk)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(?:not (?:very )?(?:helpful|useful|good)|unhelpful|wrong|confusing|"
    r"incorrect|bad|terrible|frustrating|poor|does(?:n't| not) help|dislike)\b",
    re.I,
)
_POSITIVE = re.compile(r"\b(?:helpful|useful|good|great|excellent|love|like)\b", re.I)


def detect_feedback(text: str) -> FeedbackRating | None:
    explicit = _PREFIX.match(text.strip())
    about_reply = _ABOUT_REPLY.search(text)
    negative = _NEGATIVE.search(text)
    positive = _POSITIVE.search(text)
    if not (explicit or (about_reply and (negative or positive))):
        return None
    if negative:
        return "negative"
    if positive:
        return "positive"
    return "neutral"


def feedback_only(text: str) -> bool:
    # Explicit feedback can be recorded without loading ServiceNow or model state.
    return bool(_PREFIX.match(text.strip())) and not re.search(
        r"\b(?:also|in addition|separately)\b|[.!?]\s*(?:please\s+)?"
        r"(?:show|list|open|create|check|help|troubleshoot|view|comment|resolve|reopen)\b",
        text,
        re.I,
    )


def safe_text(text: str) -> tuple[str, bool, bool]:
    original = text
    text = re.sub(
        r"(?i)\b(?:authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+",
        "Authorization: [redacted]",
        text,
    )
    text = re.sub(
        r"""(?i)\b(password|passwd|pwd|secret|(?:access[_-]?)?token|api[_ -]?key)\s*[:=]\s*"""
        r"""(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b", "[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email redacted]", text)
    redacted = text != original
    encoded = text.encode("utf-8")
    truncated = len(encoded) > 4000
    if truncated:
        text = encoded[:3980].decode("utf-8", errors="ignore") + "...[truncated]"
    return text, redacted, truncated


class FeedbackRecorder:
    def __init__(self, connection_string: str) -> None:
        self.provider: LoggerProvider | None = None
        if connection_string:
            self.provider = LoggerProvider(shutdown_on_exit=False)
            self.provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    AzureMonitorLogExporter(
                        connection_string=connection_string,
                        disable_offline_storage=True,
                        logging_enable=False,
                    ),
                    schedule_delay_millis=1000,
                )
            )

    def record(
        self,
        activity: dict[str, Any],
        tenant_id: str,
        *,
        source: Literal["native", "card", "conversation", "command"],
        rating: FeedbackRating,
        text: str,
        response_id: str = "",
    ) -> None:
        tenant, user, conversation = teams_identity(activity, tenant_id)
        if self.provider is None:
            raise DeskError(
                "Feedback capture is not configured. Please contact service desk support."
            )
        clean, redacted, truncated = safe_text(text)
        scope = hashlib.sha256("\0".join((tenant, user, conversation)).encode()).hexdigest()
        activity_id = activity.get("id") or ""
        event_id = hashlib.sha256(
            "\0".join((scope, source, str(activity_id), response_id, rating, clean)).encode()
        ).hexdigest()
        self.provider.get_logger("service_desk.feedback").emit(
            LogRecord(
                severity_number=SeverityNumber.INFO,
                severity_text="INFO",
                body=EVENT_NAME,
                attributes={
                    "microsoft.custom_event.name": EVENT_NAME,
                    "event_type": EVENT_NAME,
                    "is_feedback": True,
                    "feedback_id": event_id,
                    "feedback_source": source,
                    "feedback_rating": rating,
                    "feedback_text": clean,
                    "feedback_redacted": redacted,
                    "feedback_truncated": truncated,
                    "response_id": response_id[:2048],
                    "conversation_scope": scope,
                },
            )
        )

    def close(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()
