from __future__ import annotations

import hashlib
import os
from base64 import urlsafe_b64encode

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _resolve_fernet_key() -> bytes:
    configured_key = os.getenv("BLINK_API_KEY_ENCRYPTION_KEY", "").strip()
    if configured_key:
        encoded = configured_key.encode("utf-8")
        try:
            Fernet(encoded)
            return encoded
        except Exception:
            return urlsafe_b64encode(hashlib.sha256(encoded).digest())
    return urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest())


def encrypt_blink_api_key(raw_api_key: str) -> str:
    normalized = raw_api_key.strip()
    if not normalized:
        return ""
    fernet = Fernet(_resolve_fernet_key())
    return fernet.encrypt(normalized.encode("utf-8")).decode("utf-8")


def decrypt_blink_api_key(encrypted_value: str) -> str:
    if not encrypted_value:
        return ""
    fernet = Fernet(_resolve_fernet_key())
    try:
        return fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def mask_blink_api_key(raw_api_key: str) -> str:
    normalized = raw_api_key.strip()
    if not normalized:
        return "-"
    if len(normalized) <= 6:
        return "*" * len(normalized)
    return f"{normalized[:3]}{'*' * (len(normalized) - 6)}{normalized[-3:]}"

