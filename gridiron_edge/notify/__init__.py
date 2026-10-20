"""Outbound notifications: message formatting, channels, and the dedupe/quiet-hours chain."""
from dataclasses import dataclass


@dataclass
class NotifyResult:
    ok: bool
    channel: str
    error: str | None = None
