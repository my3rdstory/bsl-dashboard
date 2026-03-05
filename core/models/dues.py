from datetime import date

from django.conf import settings
from django.db import models
from django.utils import timezone

from .member import Member


class Dues(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SUBMITTED = "submitted"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_WAIVED = "waived"
    STATUS_CHOICES = [
        (STATUS_PENDING, "승인 대기"),
        (STATUS_SUBMITTED, "납부 제출"),
        (STATUS_APPROVED, "승인"),
        (STATUS_REJECTED, "반려"),
        (STATUS_WAIVED, "면제"),
    ]

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="dues")
    month = models.DateField(help_text="해당 월의 1일 날짜로 저장")
    amount = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    payment_reference = models.CharField(max_length=160, blank=True)
    admin_note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_dues",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-month", "member__display_name"]
        indexes = [
            models.Index(fields=["status", "month"]),
            models.Index(fields=["month"]),
            models.Index(fields=["member", "status"]),
        ]
        verbose_name = "회비"
        verbose_name_plural = "회비"
        constraints = [
            models.UniqueConstraint(
                fields=["member", "month"], name="uniq_member_month_dues"
            ),
        ]

    def __str__(self) -> str:
        return f"회비 {self.month:%Y-%m}"

    @staticmethod
    def normalize_month(year: int, month: int) -> date:
        return date(year, month, 1)
