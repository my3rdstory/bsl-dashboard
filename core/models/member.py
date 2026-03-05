from secrets import token_hex

from django.conf import settings
from django.db import models
from django.utils import timezone


class Member(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_ACTIVE = "active"
    STATUS_REJECTED = "rejected"
    STATUS_SUSPENDED = "suspended"
    STATUS_CHOICES = [
        (STATUS_PENDING, "승인 대기"),
        (STATUS_APPROVED, "승인(회비확인 대기)"),
        (STATUS_ACTIVE, "활성"),
        (STATUS_REJECTED, "반려"),
        (STATUS_SUSPENDED, "중지"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    display_name = models.CharField(max_length=80)
    phone = models.CharField(max_length=32, blank=True)
    lightning_identifier = models.CharField(max_length=120, unique=True)
    application_note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    requested_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_members",
    )
    paid_until_month = models.DateField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    is_backoffice_admin = models.BooleanField(default=False)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["status", "requested_at"]),
            models.Index(fields=["requested_at"]),
            models.Index(fields=["reviewed_at"]),
        ]
        verbose_name = "회원"
        verbose_name_plural = "회원"

    def __str__(self) -> str:
        return str(self.display_name)


def _issue_superadmin_lightning_identifier(user_id: int | None = None) -> str:
    base = f"superadmin_{user_id}" if user_id else f"superadmin_{token_hex(8)}"
    candidate = base[:120]
    suffix_no = 1
    while Member.objects.filter(lightning_identifier=candidate).exists():
        suffix = f"_{suffix_no}"
        trimmed_base = base[: max(1, 120 - len(suffix))]
        candidate = f"{trimmed_base}{suffix}"
        suffix_no += 1
    return candidate


def ensure_superadmin_member(user) -> Member | None:
    if not getattr(user, "is_superuser", False):
        return None

    member = Member.objects.filter(user=user).first()
    if member is None:
        display_name = str(getattr(user, "username", "")).strip() or "수퍼어드민"
        member = Member.objects.create(
            user=user,
            display_name=display_name[:80],
            lightning_identifier=_issue_superadmin_lightning_identifier(
                getattr(user, "id", None)
            ),
            application_note="수퍼어드민 계정 자동 생성",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
    else:
        updated_fields: list[str] = []
        if not member.display_name:
            member.display_name = (
                str(getattr(user, "username", "")).strip() or "수퍼어드민"
            )[:80]
            updated_fields.append("display_name")
        if not member.lightning_identifier:
            member.lightning_identifier = _issue_superadmin_lightning_identifier(
                getattr(user, "id", None)
            )
            updated_fields.append("lightning_identifier")
        if not member.is_backoffice_admin:
            member.is_backoffice_admin = True
            updated_fields.append("is_backoffice_admin")
        if updated_fields:
            member.save(update_fields=updated_fields)

    # Avoid circular import at module load time.
    from .member_domains import ensure_member_domain_records

    ensure_member_domain_records(member)
    return member
