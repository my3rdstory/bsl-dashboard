from __future__ import annotations

import json
import logging
from email.utils import formataddr
from urllib import error, request

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import strip_tags

from core.models import Member, MemberActivityRequest, get_message_share_setting

logger = logging.getLogger(__name__)

DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_HTTP_USER_AGENT = "bsl-dashboard/1.0"
EVENT_MEMBER_PENDING = "member_pending"
EVENT_MEMBER_APPROVED = "member_approved"
EVENT_MEMBER_DUES_CONFIRMED = "member_dues_confirmed"
EVENT_ACTIVITY_CREATED = "activity_created"
EVENT_SETTLEMENT_REQUESTED = "settlement_requested"
EVENT_SETTLEMENT_COMPLETED = "settlement_completed"

EVENT_LABEL_MAP = {
    EVENT_MEMBER_PENDING: "승인대기 회원 발생",
    EVENT_MEMBER_APPROVED: "승인 처리 완료",
    EVENT_MEMBER_DUES_CONFIRMED: "회비입금 확인 및 회원등록 완료",
    EVENT_ACTIVITY_CREATED: "활동 등록",
    EVENT_SETTLEMENT_REQUESTED: "정산요청",
    EVENT_SETTLEMENT_COMPLETED: "정산완료",
}

EVENT_MEMBER_CHANNEL_FIELD_MAP = {
    EVENT_MEMBER_PENDING: {
        "email": "send_member_pending_email",
        "discord": "send_member_pending_discord",
    },
    EVENT_MEMBER_APPROVED: {
        "email": "send_member_approved_email",
        "discord": "send_member_approved_discord",
    },
    EVENT_MEMBER_DUES_CONFIRMED: {
        "email": "send_member_dues_confirmed_email",
        "discord": "send_member_dues_confirmed_discord",
    },
}

EVENT_CHANNEL_FIELD_MAP = {
    EVENT_ACTIVITY_CREATED: {
        "email": "send_activity_created_email",
        "discord": "send_activity_created_discord",
    },
    EVENT_SETTLEMENT_REQUESTED: {
        "email": "send_settlement_requested_email",
        "discord": "send_settlement_requested_discord",
    },
    EVENT_SETTLEMENT_COMPLETED: {
        "email": "send_settlement_completed_email",
        "discord": "send_settlement_completed_discord",
    },
}


def _split_csv_values(raw_value: str) -> list[str]:
    return [item.strip() for item in str(raw_value).split(",") if item.strip()]


def _resolve_message_base_url(request_obj: HttpRequest | None = None) -> str:
    if request_obj is not None:
        try:
            return request_obj.build_absolute_uri("/").rstrip("/")
        except Exception:  # pragma: no cover
            pass

    configured_base_url = str(
        getattr(settings, "MESSAGE_SHARE_BASE_URL", "")
    ).strip()
    if configured_base_url:
        return configured_base_url.rstrip("/")

    allowed_hosts = [
        str(host).strip()
        for host in getattr(settings, "ALLOWED_HOSTS", [])
        if str(host).strip() and str(host).strip() != "*"
    ]
    if allowed_hosts:
        host = allowed_hosts[0]
        is_local_host = host.startswith("localhost") or host.startswith("127.0.0.1")
        scheme = "http" if is_local_host else "https"
        return f"{scheme}://{host}".rstrip("/")

    if bool(getattr(settings, "DEBUG", False)):
        return "http://localhost:10001"
    return ""


def _build_quick_link(base_url: str, path: str) -> str:
    normalized_base_url = str(base_url).strip().rstrip("/")
    normalized_path = str(path).strip()
    if not normalized_base_url or not normalized_path.startswith("/"):
        return ""
    return f"{normalized_base_url}{normalized_path}"


def _activity_quick_link_path(event_type: str, activity: MemberActivityRequest) -> str:
    try:
        if event_type == EVENT_ACTIVITY_CREATED:
            return reverse("core:staff-activity-detail", args=[activity.id])
        return reverse("core:staff-settlement-detail", kwargs={"activity_id": activity.id})
    except NoReverseMatch:
        return ""


def _build_activity_message(
    activity: MemberActivityRequest,
    event_label: str,
    *,
    quick_link: str = "",
) -> str:
    lines = [
        f"[BSL 활동 알림] {event_label}",
        f"- 닉네임: {activity.member.display_name}",
        f"- 제목: {activity.title}",
        f"- 활동비: {int(activity.amount_sats):,} sats",
        f"- 상태: {activity.get_status_display()}",
        f"- 라이트닝 주소: {activity.lightning_address}",
        f"- 근거 링크: {activity.evidence_url}",
    ]
    settlement_text = strip_tags(str(activity.settlement_result_html or "")).strip()
    if settlement_text:
        lines.append(f"- 정산요청 내용: {settlement_text[:300]}")
    lines.append(
        f"- 기록 시각: {timezone.localtime(timezone.now()):%Y-%m-%d %H:%M:%S}"
    )
    if quick_link:
        lines.append(f"- 바로가기: {quick_link}")
    return "\n".join(lines)


def get_activity_share_message_examples() -> list[dict[str, str]]:
    samples = [
        {
            "event_type": EVENT_MEMBER_PENDING,
            "member": "홍길동",
            "title": "",
            "amount": "0",
            "status": "승인 대기",
        },
        {
            "event_type": EVENT_MEMBER_APPROVED,
            "member": "홍길동",
            "title": "",
            "amount": "0",
            "status": "승인(회비확인 대기)",
        },
        {
            "event_type": EVENT_MEMBER_DUES_CONFIRMED,
            "member": "홍길동",
            "title": "",
            "amount": "91,666",
            "status": "활성",
        },
        {
            "event_type": EVENT_ACTIVITY_CREATED,
            "member": "홍길동",
            "title": "2월 밋업 운영 지원",
            "amount": "25,000",
            "status": "등록",
        },
        {
            "event_type": EVENT_SETTLEMENT_REQUESTED,
            "member": "홍길동",
            "title": "2월 밋업 운영 지원",
            "amount": "25,000",
            "status": "정산요청",
        },
        {
            "event_type": EVENT_SETTLEMENT_COMPLETED,
            "member": "홍길동",
            "title": "2월 밋업 운영 지원",
            "amount": "25,000",
            "status": "정산완료",
        },
    ]
    rows: list[dict[str, str]] = []
    for sample in samples:
        event_type = sample["event_type"]
        event_label = EVENT_LABEL_MAP[event_type]
        if event_type == EVENT_MEMBER_PENDING:
            lines = [
                f"[BSL 회원 알림] {event_label}",
                f"- 닉네임: {sample['member']}",
                "- 라이트닝 ID: lnid_example_123456",
                f"- 상태: {sample['status']}",
                "- 신청 내용: 자기소개 및 검증용 소셜미디어 주소...",
                "- 기록 시각: YYYY-MM-DD HH:MM:SS",
            ]
        elif event_type == EVENT_MEMBER_APPROVED:
            lines = [
                f"[BSL 회원 알림] {event_label}",
                f"- 닉네임: {sample['member']}",
                "- 라이트닝 ID: lnid_example_123456",
                "- 상태 변경: 승인 대기 -> 승인(회비확인 대기)",
                "- 상태: 승인(회비확인 대기)",
                "- 처리자: 운영자",
                "- 처리 사유: 가입 승인",
                "- 기록 시각: YYYY-MM-DD HH:MM:SS",
            ]
        elif event_type == EVENT_MEMBER_DUES_CONFIRMED:
            lines = [
                f"[BSL 회원 알림] {event_label}",
                f"- 닉네임: {sample['member']}",
                "- 라이트닝 ID: lnid_example_123456",
                "- 상태 변경: 승인(회비확인 대기) -> 활성",
                f"- 회비 금액: {sample['amount']} sats",
                "- 결제 정보: 온체인: txid_example_123",
                "- 처리자: 운영자",
                "- 처리 사유: 회비 입금 확인",
                "- 기록 시각: YYYY-MM-DD HH:MM:SS",
            ]
        else:
            lines = [
                f"[BSL 활동 알림] {event_label}",
                f"- 닉네임: {sample['member']}",
                f"- 제목: {sample['title']}",
                f"- 활동비: {sample['amount']} sats",
                f"- 상태: {sample['status']}",
                "- 라이트닝 주소: satoshi@blink.sv",
                "- 근거 링크: https://discord.com/channels/123/456/789",
                "- 정산요청 내용: 진행 내용 및 결과 요약...",
                "- 기록 시각: YYYY-MM-DD HH:MM:SS",
            ]
        rows.append(
            {
                "event_type": event_type,
                "event_label": event_label,
                "content": "\n".join(lines),
            }
        )
    return rows


def _send_discord_channel_message(channel_id: str, content: str) -> None:
    token = str(getattr(settings, "MESSAGE_SHARE_DISCORD_BOT_TOKEN", "")).strip()
    if not token:
        logger.warning("MESSAGE_SHARE_DISCORD_BOT_TOKEN 미설정으로 디스코드 발송을 건너뜁니다.")
        return

    payload = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = request.Request(
        url=f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_HTTP_USER_AGENT,
        },
    )
    try:
        with request.urlopen(req, timeout=7):
            return
    except error.HTTPError as exc:
        logger.warning("디스코드 발송 실패(HTTP %s): %s", exc.code, exc.reason)
    except error.URLError as exc:
        logger.warning("디스코드 발송 실패: %s", exc.reason)


def _send_message_via_gmail(
    *,
    recipient_list: list[str],
    subject: str,
    content: str,
    setting,
) -> None:
    sender_email = str(getattr(setting, "gmail_sender_email", "")).strip()
    sender_name = str(getattr(setting, "gmail_sender_name", "")).strip()
    app_password = str(getattr(setting, "gmail_app_password", "")).replace(" ", "").strip()
    if not sender_email or not app_password:
        logger.warning("지메일 발송 정보 미설정으로 이메일 전송을 건너뜁니다.")
        return

    from_email = (
        formataddr((sender_name, sender_email))
        if sender_name
        else sender_email
    )
    connection = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host="smtp.gmail.com",
        port=587,
        username=sender_email,
        password=app_password,
        use_tls=True,
        fail_silently=False,
        timeout=10,
    )
    mail = EmailMessage(
        subject=subject,
        body=content,
        from_email=from_email,
        to=recipient_list,
        connection=connection,
    )
    mail.send(fail_silently=False)


def send_activity_share_messages(
    activity: MemberActivityRequest,
    *,
    event_type: str,
    request_obj: HttpRequest | None = None,
) -> None:
    if event_type not in EVENT_CHANNEL_FIELD_MAP:
        logger.warning("지원하지 않는 활동 알림 이벤트: %s", event_type)
        return

    setting = get_message_share_setting()
    admin_emails = _split_csv_values(str(setting.admin_email or ""))
    discord_channel_ids = _split_csv_values(str(setting.discord_channel_id or ""))
    channel_fields = EVENT_CHANNEL_FIELD_MAP[event_type]
    send_email = bool(getattr(setting, channel_fields["email"], False))
    send_discord = bool(getattr(setting, channel_fields["discord"], False))
    if not send_email and not send_discord:
        return
    if not admin_emails and not discord_channel_ids:
        return

    event_label = EVENT_LABEL_MAP[event_type]
    base_url = _resolve_message_base_url(request_obj)
    quick_link = _build_quick_link(
        base_url,
        _activity_quick_link_path(event_type, activity),
    )
    content = _build_activity_message(activity, event_label, quick_link=quick_link)
    if send_email and admin_emails:
        try:
            _send_message_via_gmail(
                recipient_list=admin_emails,
                subject=f"[BSL] 활동 내역 알림 - {event_label}",
                content=content,
                setting=setting,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("활동 알림 이메일 발송 실패: %s", exc)

    if send_discord:
        for channel_id in discord_channel_ids:
            _send_discord_channel_message(channel_id, content)


def _build_pending_member_message(member, *, quick_link: str = "") -> str:
    lines = [
        "[BSL 회원 알림] 승인대기 회원 발생",
        f"- 닉네임: {member.display_name}",
        f"- 라이트닝 ID: {member.lightning_identifier}",
        f"- 상태: {member.get_status_display()}",
        f"- 신청 내용: {strip_tags(str(member.application_note or '')).strip()[:300] or '-'}",
        f"- 기록 시각: {timezone.localtime(timezone.now()):%Y-%m-%d %H:%M:%S}",
    ]
    if quick_link:
        lines.append(f"- 바로가기: {quick_link}")
    return "\n".join(lines)


def _build_member_status_message(
    member: Member,
    event_label: str,
    *,
    status_transition: str = "",
    dues_amount_sats: int | None = None,
    payment_note: str = "",
    actor_name: str = "",
    review_note: str = "",
    quick_link: str = "",
) -> str:
    lines = [
        f"[BSL 회원 알림] {event_label}",
        f"- 닉네임: {member.display_name}",
        f"- 라이트닝 ID: {member.lightning_identifier}",
    ]
    if status_transition:
        lines.append(f"- 상태 변경: {status_transition}")
    lines.append(f"- 상태: {member.get_status_display()}")
    if dues_amount_sats is not None:
        lines.append(f"- 회비 금액: {int(dues_amount_sats):,} sats")
    if payment_note:
        lines.append(f"- 결제 정보: {payment_note}")
    if actor_name:
        lines.append(f"- 처리자: {actor_name}")
    if review_note:
        lines.append(f"- 처리 사유: {review_note}")
    lines.append(f"- 기록 시각: {timezone.localtime(timezone.now()):%Y-%m-%d %H:%M:%S}")
    if quick_link:
        lines.append(f"- 바로가기: {quick_link}")
    return "\n".join(lines)


def _member_quick_link_path(member: Member) -> str:
    try:
        return reverse("core:staff-member-detail", kwargs={"member_id": member.id})
    except NoReverseMatch:
        return ""


def _send_member_event_message(*, event_type: str, content: str) -> None:
    if event_type not in EVENT_MEMBER_CHANNEL_FIELD_MAP:
        logger.warning("지원하지 않는 회원 알림 이벤트: %s", event_type)
        return

    setting = get_message_share_setting()
    channel_fields = EVENT_MEMBER_CHANNEL_FIELD_MAP[event_type]
    send_email = bool(getattr(setting, channel_fields["email"], False))
    send_discord = bool(getattr(setting, channel_fields["discord"], False))
    if not send_email and not send_discord:
        return

    admin_emails = _split_csv_values(str(setting.admin_email or ""))
    discord_channel_ids = _split_csv_values(str(setting.discord_channel_id or ""))
    if not admin_emails and not discord_channel_ids:
        return

    event_label = EVENT_LABEL_MAP[event_type]
    if send_email and admin_emails:
        try:
            _send_message_via_gmail(
                recipient_list=admin_emails,
                subject=f"[BSL] 회원 알림 - {event_label}",
                content=content,
                setting=setting,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("회원 알림 이메일 발송 실패: %s", exc)

    if send_discord:
        for channel_id in discord_channel_ids:
            _send_discord_channel_message(channel_id, content)


def send_pending_member_share_messages(
    member,
    *,
    request_obj: HttpRequest | None = None,
) -> None:
    if not isinstance(member, Member):
        return
    base_url = _resolve_message_base_url(request_obj)
    quick_link = _build_quick_link(base_url, reverse("core:staff-members-pending"))
    content = _build_pending_member_message(member, quick_link=quick_link)
    _send_member_event_message(
        event_type=EVENT_MEMBER_PENDING,
        content=content,
    )


def send_member_status_share_messages(
    member: Member,
    *,
    event_type: str,
    request_obj: HttpRequest | None = None,
    actor_name: str = "",
    review_note: str = "",
    dues_amount_sats: int | None = None,
    payment_note: str = "",
) -> None:
    if event_type not in {EVENT_MEMBER_APPROVED, EVENT_MEMBER_DUES_CONFIRMED}:
        logger.warning("지원하지 않는 회원 상태 알림 이벤트: %s", event_type)
        return

    base_url = _resolve_message_base_url(request_obj)
    quick_link = _build_quick_link(base_url, _member_quick_link_path(member))
    if event_type == EVENT_MEMBER_APPROVED:
        status_transition = "승인 대기 -> 승인(회비확인 대기)"
    else:
        status_transition = "승인(회비확인 대기) -> 활성"
    content = _build_member_status_message(
        member,
        EVENT_LABEL_MAP[event_type],
        status_transition=status_transition,
        dues_amount_sats=dues_amount_sats,
        payment_note=payment_note,
        actor_name=actor_name,
        review_note=review_note,
        quick_link=quick_link,
    )
    _send_member_event_message(
        event_type=event_type,
        content=content,
    )
