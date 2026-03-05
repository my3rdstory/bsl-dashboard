from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone


class LightningLoginAttempt(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_EXPIRED = "expired"
    STATUS_CHOICES = [
        (STATUS_PENDING, "대기"),
        (STATUS_SUCCESS, "성공"),
        (STATUS_FAILED, "실패"),
        (STATUS_EXPIRED, "만료"),
    ]

    token = models.UUIDField(default=uuid4, unique=True, editable=False)
    k1 = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    auth_invoice = models.TextField()
    qr_payload = models.TextField()
    wallet_key = models.CharField(max_length=130, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    require_nickname = models.BooleanField(default=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lightning_login_attempts",
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["wallet_key"]),
            models.Index(fields=["expires_at"]),
        ]
        verbose_name = "라이트닝 로그인 시도"
        verbose_name_plural = "라이트닝 로그인 시도"

    def __str__(self) -> str:
        return f"LightningLoginAttempt({self.status})"

    @staticmethod
    def default_expiry() -> timezone.datetime:
        return timezone.now() + timedelta(minutes=2)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class LightningIdentity(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="lightning_identity",
    )
    wallet_key = models.CharField(max_length=130, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_authenticated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["wallet_key"]),
            models.Index(fields=["last_authenticated_at"]),
        ]
        verbose_name = "라이트닝 인증 계정"
        verbose_name_plural = "라이트닝 인증 계정"

    def __str__(self) -> str:
        return str(self.wallet_key)
