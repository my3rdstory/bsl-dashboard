from secrets import token_hex

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .member import Member
from core.utils import (
    decrypt_blink_api_key,
    encrypt_blink_api_key,
    mask_blink_api_key,
)


class MemberAuthProfile(models.Model):
    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name="auth_profile",
    )
    internal_temp_id = models.CharField(max_length=64, unique=True)
    internal_temp_password = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "회원 인증 내부정보"
        verbose_name_plural = "회원 인증 내부정보"

    def __str__(self) -> str:
        return f"{self.member_id}"


class MemberLifecycle(models.Model):
    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name="lifecycle",
    )
    signed_up_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "회원 생명주기"
        verbose_name_plural = "회원 생명주기"
        indexes = [
            models.Index(fields=["signed_up_at"]),
            models.Index(fields=["withdrawn_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.member_id}"


class MemberYearMembership(models.Model):
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="year_memberships",
    )
    year = models.PositiveIntegerField()
    joined_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year", "-joined_at"]
        indexes = [
            models.Index(fields=["member"]),
            models.Index(fields=["year"]),
            models.Index(fields=["member", "ended_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "year"],
                name="uniq_member_year_membership",
            )
        ]
        verbose_name = "회원 연도 이력"
        verbose_name_plural = "회원 연도 이력"

    def __str__(self) -> str:
        return f"{self.member_id}:{self.year}"


class MemberReviewLog(models.Model):
    ACTION_APPROVE = "approve"
    ACTION_REQUEST_DUES = "request_dues"
    ACTION_REJECT = "reject"
    ACTION_MARK_PENDING = "mark_pending"
    ACTION_CONFIRM_DUES = "confirm_dues"
    ACTION_WITHDRAW = "withdraw"
    ACTION_CHOICES = [
        (ACTION_APPROVE, "승인"),
        (ACTION_REQUEST_DUES, "회비지급요청"),
        (ACTION_REJECT, "반려"),
        (ACTION_MARK_PENDING, "미승인 전환"),
        (ACTION_CONFIRM_DUES, "회비확인"),
        (ACTION_WITHDRAW, "탈퇴 처리"),
    ]

    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="review_logs",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    note = models.TextField(blank=True)
    acted_at = models.DateTimeField(default=timezone.now)
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_review_logs",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-acted_at", "-id"]
        indexes = [
            models.Index(fields=["member", "acted_at"]),
            models.Index(fields=["action", "acted_at"]),
            models.Index(fields=["member", "action"]),
        ]
        verbose_name = "회원 처리 이력"
        verbose_name_plural = "회원 처리 이력"

    def __str__(self) -> str:
        return f"{self.member_id}:{self.action}@{self.acted_at}"


class MemberWithdrawalHistory(models.Model):
    former_user_username = models.CharField(max_length=150, blank=True, default="")
    former_display_name = models.CharField(max_length=80)
    former_lightning_identifier = models.CharField(max_length=120)
    former_status = models.CharField(max_length=16, blank=True, default="")
    requested_at = models.DateTimeField(null=True, blank=True)
    signed_up_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(default=timezone.now)
    withdraw_note = models.TextField(blank=True, default="")
    application_note = models.TextField(blank=True, default="")
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="member_withdrawal_histories",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-withdrawn_at", "-id"]
        indexes = [
            models.Index(fields=["withdrawn_at"]),
            models.Index(fields=["former_lightning_identifier"]),
        ]
        verbose_name = "회원 탈퇴 이력"
        verbose_name_plural = "회원 탈퇴 이력"

    def __str__(self) -> str:
        return f"{self.former_display_name}:{self.withdrawn_at}"


class MemberSettlement(models.Model):
    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name="settlement",
    )
    dues_paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["dues_paid_at"]),
        ]
        verbose_name = "회원 정산"
        verbose_name_plural = "회원 정산"

    def __str__(self) -> str:
        return f"{self.member_id}"


class MemberActivity(models.Model):
    member = models.OneToOneField(
        Member,
        on_delete=models.CASCADE,
        related_name="activity",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["claimed_at"]),
            models.Index(fields=["processed_at"]),
        ]
        verbose_name = "회원 활동비"
        verbose_name_plural = "회원 활동비"

    def __str__(self) -> str:
        return f"{self.member_id}"


class MemberActivityRequest(models.Model):
    STATUS_REGISTERED = "registered"
    STATUS_SETTLEMENT_REQUESTED = "settlement_requested"
    STATUS_SETTLEMENT_COMPLETED = "settlement_completed"
    STATUS_CHOICES = [
        (STATUS_REGISTERED, "등록"),
        (STATUS_SETTLEMENT_REQUESTED, "정산요청"),
        (STATUS_SETTLEMENT_COMPLETED, "정산완료"),
    ]

    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="activity_requests",
    )
    title = models.CharField(max_length=300)
    content_html = models.TextField()
    amount_sats = models.PositiveBigIntegerField()
    lightning_address = models.CharField(max_length=100)
    evidence_url = models.URLField(max_length=100)
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_REGISTERED,
    )
    settlement_result_html = models.TextField(blank=True, default="")
    settlement_requested_at = models.DateTimeField(null=True, blank=True)
    settlement_completed_at = models.DateTimeField(null=True, blank=True)
    settlement_processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_activity_settlements",
    )
    settlement_processed_at = models.DateTimeField(null=True, blank=True)
    settlement_transfer_status = models.CharField(max_length=24, blank=True, default="")
    settlement_transfer_reference = models.CharField(max_length=180, blank=True, default="")
    settlement_transfer_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["status", "settlement_requested_at"]),
            models.Index(fields=["status", "settlement_requested_at", "updated_at"]),
            models.Index(fields=["status", "settlement_completed_at"]),
            models.Index(fields=["member", "status"]),
            models.Index(fields=["member", "created_at"]),
        ]
        verbose_name = "회원 활동 등록"
        verbose_name_plural = "회원 활동 등록"

    def __str__(self) -> str:
        return f"{self.member_id}:{self.title}"


class MemberActivityAttachment(models.Model):
    TYPE_ACTIVITY = "activity"
    TYPE_SETTLEMENT_EVIDENCE = "settlement_evidence"
    ATTACHMENT_TYPE_CHOICES = [
        (TYPE_ACTIVITY, "활동첨부"),
        (TYPE_SETTLEMENT_EVIDENCE, "정산근거첨부"),
    ]

    activity_request = models.ForeignKey(
        MemberActivityRequest,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    object_key = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    file_size_bytes = models.PositiveBigIntegerField(default=0)
    attachment_type = models.CharField(
        max_length=24,
        choices=ATTACHMENT_TYPE_CHOICES,
        default=TYPE_ACTIVITY,
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["activity_request", "created_at"]),
            models.Index(fields=["attachment_type"]),
            models.Index(fields=["file_size_bytes"]),
        ]
        verbose_name = "회원 활동 첨부파일"
        verbose_name_plural = "회원 활동 첨부파일"

    def __str__(self) -> str:
        return f"{self.activity_request_id}:{self.original_filename}"


class MemberAddressHistory(models.Model):
    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="address_histories",
    )
    bitcoin_onchain_address = models.CharField(max_length=100)
    lightning_address = models.CharField(max_length=100)
    blink_api_key_encrypted = models.TextField(blank=True, default="")
    blink_wallet_id = models.CharField(max_length=120, blank=True, default="")
    is_current = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    replaced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["member", "is_current"]),
            models.Index(fields=["is_current", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member", "is_current"],
                name="uniq_member_current_address",
                condition=Q(is_current=True),
            )
        ]
        verbose_name = "회원 주소 이력"
        verbose_name_plural = "회원 주소 이력"

    def __str__(self) -> str:
        return f"{self.member_id}:{self.created_at}"

    def get_blink_api_key(self) -> str:
        return decrypt_blink_api_key(self.blink_api_key_encrypted)

    @property
    def masked_blink_api_key(self) -> str:
        return mask_blink_api_key(self.get_blink_api_key())


def ensure_member_domain_records(member: Member) -> None:
    def issue_unique_auth_value(field_name: str, prefix: str, size: int) -> str:
        candidate = f"{prefix}_{token_hex(size)}"
        while MemberAuthProfile.objects.filter(**{field_name: candidate}).exists():
            candidate = f"{prefix}_{token_hex(size)}"
        return candidate

    auth_profile, _ = MemberAuthProfile.objects.get_or_create(
        member=member,
        defaults={
            "internal_temp_id": issue_unique_auth_value("internal_temp_id", "mid", 10),
            "internal_temp_password": issue_unique_auth_value(
                "internal_temp_password", "mpw", 16
            ),
        },
    )
    auth_updates = []
    if not auth_profile.internal_temp_id:
        auth_profile.internal_temp_id = issue_unique_auth_value(
            "internal_temp_id", "mid", 10
        )
        auth_updates.append("internal_temp_id")
    if not auth_profile.internal_temp_password:
        auth_profile.internal_temp_password = issue_unique_auth_value(
            "internal_temp_password", "mpw", 16
        )
        auth_updates.append("internal_temp_password")
    if auth_updates:
        auth_profile.save(update_fields=auth_updates)

    lifecycle, _ = MemberLifecycle.objects.get_or_create(
        member=member,
        defaults={
            "signed_up_at": member.requested_at or timezone.now(),
            "withdrawn_at": None,
        },
    )
    lifecycle_updates = []
    if lifecycle.signed_up_at is None:
        lifecycle.signed_up_at = member.requested_at or timezone.now()
        lifecycle_updates.append("signed_up_at")
    if lifecycle_updates:
        lifecycle.save(update_fields=lifecycle_updates)

    settlement, _ = MemberSettlement.objects.get_or_create(
        member=member,
        defaults={"dues_paid_at": None},
    )

    activity, _ = MemberActivity.objects.get_or_create(
        member=member,
        defaults={
            "claimed_at": None,
            "processed_at": None,
        },
    )
    activity_updates = []
    if activity.processed_at and not activity.claimed_at:
        activity.claimed_at = activity.processed_at
        activity_updates.append("claimed_at")
    if activity_updates:
        activity.save(update_fields=activity_updates)


def _year_from_datetime(reference_at):
    return int(timezone.localtime(reference_at).year)


def open_member_year_membership(member: Member, reference_at=None) -> MemberYearMembership:
    joined_at = reference_at or timezone.now()
    year = _year_from_datetime(joined_at)
    history, _ = MemberYearMembership.objects.get_or_create(
        member=member,
        year=year,
        defaults={"joined_at": joined_at, "ended_at": None},
    )

    updates = []
    if history.joined_at is None:
        history.joined_at = joined_at
        updates.append("joined_at")
    if history.ended_at is not None:
        history.ended_at = None
        updates.append("ended_at")
    if updates:
        history.save(update_fields=updates)
    return history


def close_member_year_membership(
    member: Member, reference_at=None
) -> MemberYearMembership:
    ended_at = reference_at or timezone.now()
    year = _year_from_datetime(ended_at)
    history, _ = MemberYearMembership.objects.get_or_create(
        member=member,
        year=year,
        defaults={"joined_at": ended_at, "ended_at": ended_at},
    )
    if history.ended_at != ended_at:
        history.ended_at = ended_at
        history.save(update_fields=["ended_at"])
    return history


def log_member_review(
    member: Member,
    action: str,
    actor=None,
    note: str = "",
    acted_at=None,
) -> MemberReviewLog:
    return MemberReviewLog.objects.create(
        member=member,
        action=action,
        note=note,
        acted_at=acted_at or timezone.now(),
        acted_by=actor,
    )


def get_current_member_address(member: Member) -> MemberAddressHistory | None:
    return (
        member.address_histories.filter(is_current=True)
        .order_by("-created_at", "-id")
        .first()
    )


def replace_member_address(
    member: Member,
    bitcoin_onchain_address: str,
    blink_lightning_address: str,
    blink_api_key: str,
    blink_wallet_id: str,
    reference_at=None,
) -> MemberAddressHistory:
    now = reference_at or timezone.now()
    member.address_histories.filter(is_current=True).update(
        is_current=False,
        replaced_at=now,
    )
    return MemberAddressHistory.objects.create(
        member=member,
        bitcoin_onchain_address=bitcoin_onchain_address,
        lightning_address=blink_lightning_address,
        blink_api_key_encrypted=encrypt_blink_api_key(blink_api_key),
        blink_wallet_id=blink_wallet_id.strip(),
        is_current=True,
    )
