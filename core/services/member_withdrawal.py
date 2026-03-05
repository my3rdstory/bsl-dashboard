from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from core.models import Member, MemberWithdrawalHistory


def withdraw_member_with_history(
    member: Member,
    *,
    review_note: str,
    withdrew_at=None,
    actor=None,
) -> None:
    member_user = member.user
    withdraw_time = withdrew_at or timezone.now()
    lifecycle = None
    try:
        lifecycle = member.lifecycle
    except ObjectDoesNotExist:
        lifecycle = None

    MemberWithdrawalHistory.objects.create(
        former_user_username=member_user.get_username(),
        former_display_name=member.display_name,
        former_lightning_identifier=member.lightning_identifier,
        former_status=member.status,
        requested_at=member.requested_at,
        signed_up_at=lifecycle.signed_up_at if lifecycle else None,
        reviewed_at=member.reviewed_at,
        withdrawn_at=withdraw_time,
        withdraw_note=review_note.strip(),
        application_note=member.application_note,
        withdrawn_by=actor,
    )
    member_user.delete()
