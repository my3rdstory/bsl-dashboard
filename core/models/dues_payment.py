from django.db import models
from django.utils import timezone

from .member import Member


class MemberDuesPayment(models.Model):
    METHOD_ONCHAIN = "onchain"
    METHOD_LIGHTNING = "lightning"
    METHOD_CHOICES = [
        (METHOD_ONCHAIN, "온체인"),
        (METHOD_LIGHTNING, "라이트닝"),
    ]

    STATUS_READY = "ready"
    STATUS_WAITING_ONCHAIN = "waiting_onchain"
    STATUS_WAITING_LIGHTNING = "waiting_lightning"
    STATUS_PAID = "paid"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_READY, "결제수단 선택 대기"),
        (STATUS_WAITING_ONCHAIN, "온체인 확인 대기"),
        (STATUS_WAITING_LIGHTNING, "라이트닝 확인 대기"),
        (STATUS_PAID, "납부 완료"),
        (STATUS_FAILED, "실패"),
    ]

    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="dues_payments",
    )
    request_month = models.DateField(help_text="신청일 기준 월(1일) 저장")
    amount_sats = models.PositiveIntegerField()
    payment_method = models.CharField(
        max_length=16,
        choices=METHOD_CHOICES,
        blank=True,
        default="",
    )
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_READY,
    )

    destination_onchain_address = models.CharField(max_length=120, blank=True, default="")
    destination_lightning_address = models.CharField(max_length=120, blank=True, default="")
    destination_blink_wallet_id = models.CharField(max_length=120, blank=True, default="")

    onchain_txid = models.CharField(max_length=128, blank=True, default="")
    onchain_tx_submitted_at = models.DateTimeField(null=True, blank=True)
    onchain_confirmed_at = models.DateTimeField(null=True, blank=True)

    lightning_invoice = models.TextField(blank=True, default="")
    lightning_invoice_hash = models.CharField(max_length=128, blank=True, default="")
    lightning_invoice_created_at = models.DateTimeField(null=True, blank=True)
    lightning_invoice_paid_at = models.DateTimeField(null=True, blank=True)

    requested_by_staff_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    check_note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["member", "status"]),
            models.Index(fields=["member", "status", "requested_by_staff_at", "created_at"]),
            models.Index(fields=["status", "request_month"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "paid_at"]),
            models.Index(fields=["requested_by_staff_at"]),
            models.Index(fields=["paid_at"]),
            models.Index(fields=["status", "requested_by_staff_at"]),
        ]
        verbose_name = "회원 회비 납입 요청"
        verbose_name_plural = "회원 회비 납입 요청"

    def __str__(self) -> str:
        return f"{self.member_id}:{self.request_month:%Y-%m}:{self.status}"

    @property
    def is_pending(self) -> bool:
        return self.status in {
            self.STATUS_READY,
            self.STATUS_WAITING_ONCHAIN,
            self.STATUS_WAITING_LIGHTNING,
        }
