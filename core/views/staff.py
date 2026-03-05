from django.contrib import messages
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Max, Q, QuerySet, Value, When
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from core.forms import MemberActivityRequestForm, MemberActivitySettlementRequestForm
from core.models import (
    Dues,
    Member,
    MemberActivityAttachment,
    MemberActivityRequest,
    MemberDuesPayment,
    MemberReviewLog,
    build_monthly_dues_plan,
    ensure_member_domain_records,
    get_basic_dues_setting,
    MemberWithdrawalHistory,
    log_member_review,
    open_member_year_membership,
)
from core.services import (
    EVENT_ACTIVITY_CREATED,
    EVENT_MEMBER_APPROVED,
    EVENT_MEMBER_DUES_CONFIRMED,
    EVENT_SETTLEMENT_COMPLETED,
    EVENT_SETTLEMENT_REQUESTED,
    DuesPaymentServiceError,
    ObjectStorageError,
    build_attachment_download_url,
    download_object_bytes,
    delete_object,
    format_due_payment_review_note,
    is_object_storage_configured,
    mark_due_request_by_staff,
    send_activity_share_messages,
    send_member_status_share_messages,
    send_lightning_payment_to_address,
    upload_activity_attachment,
    withdraw_member_with_history,
)

from .common import (
    build_reverse_numbered_rows,
    paginate_items,
    parse_month,
    resolve_request_member,
    staff_required,
)


MAX_ACTIVITY_ATTACHMENTS = 10
MAX_ACTIVITY_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024


def _wants_json_response(request: HttpRequest) -> bool:
    accepted = str(request.headers.get("Accept", "")).lower()
    if "application/json" in accepted:
        return True
    return str(request.headers.get("X-Requested-With", "")).lower() == "xmlhttprequest"


def _members_with_domains() -> QuerySet[Member]:
    return Member.objects.select_related(
        "user",
        "auth_profile",
        "lifecycle",
        "settlement",
        "activity",
    ).prefetch_related("year_memberships")


def _ensure_domain_rows(members: QuerySet[Member]) -> None:
    for member in members:
        ensure_member_domain_records(member)


def _active_members_ordered() -> QuerySet[Member]:
    return _members_with_domains().filter(status=Member.STATUS_ACTIVE).order_by(
        "-lifecycle__signed_up_at", "-requested_at"
    )


def _approved_members_ordered() -> QuerySet[Member]:
    return _members_with_domains().filter(status=Member.STATUS_APPROVED).order_by(
        "-reviewed_at", "-requested_at"
    )


def _dues_amount_by_member_id(members: list[Member]) -> dict[int, int]:
    setting = get_basic_dues_setting()
    monthly_plan = build_monthly_dues_plan(
        annual_dues_sats=int(setting.annual_dues_sats),
        year=int(setting.configured_year),
    )
    dues_by_month: dict[int, int] = {
        int(row["month"]): int(row["total_fee_sats"])
        for row in monthly_plan
    }
    fallback_amount = int(setting.annual_dues_sats)
    amount_map: dict[int, int] = {}
    for member in members:
        requested_month = int(timezone.localtime(member.requested_at).month)
        amount_map[int(member.id)] = dues_by_month.get(requested_month, fallback_amount)
    return amount_map


def _actor_label_map(user_ids: list[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    labels: dict[int, str] = {}
    for actor_member in Member.objects.filter(user_id__in=user_ids).only(
        "user_id",
        "display_name",
        "lightning_identifier",
    ):
        labels[int(actor_member.user_id)] = (
            f"{actor_member.display_name} ({actor_member.lightning_identifier})"
        )
    return labels


def _resolve_post_next_url(request: HttpRequest, fallback: str) -> str:
    next_url = request.POST.get("next", "")
    if not isinstance(next_url, str):
        return fallback
    candidate = next_url.strip()
    if not candidate or not candidate.startswith("/"):
        return fallback
    if not url_has_allowed_host_and_scheme(
        candidate, {request.get_host()}, require_https=False
    ):
        return fallback
    return candidate


def _split_actor_label(actor_label: str) -> tuple[str, str]:
    label = str(actor_label).strip()
    if label.endswith(")") and " (" in label:
        nickname, lightning_part = label.rsplit(" (", 1)
        return nickname.strip(), lightning_part[:-1].strip()
    return label, ""


def _parse_reviewed_at_or_now(reviewed_at_text: str) -> timezone.datetime:
    reviewed_at = timezone.now()
    if not reviewed_at_text:
        return reviewed_at

    parsed_reviewed_at = parse_datetime(reviewed_at_text)
    if parsed_reviewed_at is None:
        return reviewed_at
    if timezone.is_naive(parsed_reviewed_at):
        parsed_reviewed_at = timezone.make_aware(
            parsed_reviewed_at,
            timezone.get_current_timezone(),
        )
    return parsed_reviewed_at


def _build_confirm_dues_review_note(review_note: str, payment_note: str) -> str:
    clean_note = review_note.strip()
    clean_payment_note = payment_note.strip()
    if not clean_note:
        return clean_payment_note or "회비확인 처리"
    if not clean_payment_note:
        return clean_note
    return f"{clean_note}\n결제정보: {clean_payment_note}"


def _validate_activity_attachments(uploaded_files: list) -> list[str]:
    errors: list[str] = []
    if len(uploaded_files) > MAX_ACTIVITY_ATTACHMENTS:
        errors.append(
            f"첨부파일은 최대 {MAX_ACTIVITY_ATTACHMENTS}개까지 등록할 수 있습니다."
        )
    for uploaded_file in uploaded_files:
        size = int(getattr(uploaded_file, "size", 0))
        if size > MAX_ACTIVITY_ATTACHMENT_SIZE_BYTES:
            errors.append(
                f"{uploaded_file.name} 파일이 10MB를 초과했습니다."
            )
    return errors


def _get_request_member(request: HttpRequest) -> Member:
    member = resolve_request_member(request.user)
    if member is None:
        raise Http404("No Member matches the given query.")
    return member


def _can_manage_activity_request(
    acting_member: Member, activity: MemberActivityRequest
) -> bool:
    if activity.status == MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED:
        return False
    if bool(acting_member.is_backoffice_admin):
        return True
    if activity.status != MemberActivityRequest.STATUS_REGISTERED:
        return False
    return int(activity.member_id) == int(acting_member.id)


def _can_request_activity_settlement(
    acting_member: Member, activity: MemberActivityRequest
) -> bool:
    if activity.status != MemberActivityRequest.STATUS_REGISTERED:
        return False
    return int(activity.member_id) == int(acting_member.id)


def _can_complete_activity_settlement(
    acting_member: Member, activity: MemberActivityRequest
) -> bool:
    if activity.status != MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED:
        return False
    return bool(acting_member.is_backoffice_admin)


def _activity_permission_denied_response(
    request: HttpRequest, activity: MemberActivityRequest | None = None
) -> HttpResponse:
    if (
        activity is not None
        and activity.status == MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED
    ):
        messages.error(request, "정산완료 상태 활동은 수정/삭제할 수 없습니다.")
        return redirect("core:staff-settlement-detail", activity_id=activity.id)
    messages.error(request, "본인이 등록한 활동만 수정/삭제할 수 있습니다.")
    return redirect("core:staff-activities")


@staff_required
def staff_dashboard(request: HttpRequest) -> HttpResponse:
    month = parse_month(request.GET.get("month"))
    month_dues = Dues.objects.filter(month=month)
    metrics = {
        "pending_members": Member.objects.filter(status=Member.STATUS_PENDING).count(),
        "month_total": month_dues.count(),
        "month_pending": month_dues.filter(
            status__in=[Dues.STATUS_PENDING, Dues.STATUS_SUBMITTED]
        ).count(),
        "month_approved": month_dues.filter(status=Dues.STATUS_APPROVED).count(),
    }
    return render(
        request, "core/staff/dashboard.html", {"metrics": metrics, "month": month}
    )


@staff_required
def staff_members(request: HttpRequest) -> HttpResponse:
    _ensure_domain_rows(_active_members_ordered())
    members_queryset = _active_members_ordered().filter(
        settlement__dues_paid_at__isnull=False
    )
    page_obj = paginate_items(members_queryset, request.GET.get("page"), per_page=10)
    members = list(page_obj.object_list)
    members_with_no = build_reverse_numbered_rows(page_obj, item_key="member")
    return render(
        request,
        "core/staff/member_list.html",
        {
            "members": members,
            "members_with_no": members_with_no,
            "member_total_count": page_obj.paginator.count,
            "member_list_title": "회원 목록",
            "member_list_description": (
                "승인 후 회비 납부가 확인된 회원 목록입니다."
            ),
            "member_list_mode": "paid",
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_members_dues_request(request: HttpRequest) -> HttpResponse:
    _ensure_domain_rows(_approved_members_ordered())
    members_queryset = _approved_members_ordered().filter(
        settlement__dues_paid_at__isnull=True
    )
    page_obj = paginate_items(members_queryset, request.GET.get("page"), per_page=10)
    members = list(page_obj.object_list)
    members_with_no = build_reverse_numbered_rows(page_obj, item_key="member")
    dues_amount_by_member_id = _dues_amount_by_member_id(members)
    member_ids = [int(member.id) for member in members]
    requested_member_ids = set(
        MemberDuesPayment.objects.filter(
            member_id__in=member_ids,
            requested_by_staff_at__isnull=False,
            status__in=[
                MemberDuesPayment.STATUS_READY,
                MemberDuesPayment.STATUS_WAITING_ONCHAIN,
                MemberDuesPayment.STATUS_WAITING_LIGHTNING,
                MemberDuesPayment.STATUS_FAILED,
            ],
        ).values_list("member_id", flat=True)
    )
    for row in members_with_no:
        member_id = int(row["member"].id)
        row["dues_amount_sats"] = int(dues_amount_by_member_id.get(member_id, 0))
        row["is_due_requested"] = member_id in requested_member_ids
    return render(
        request,
        "core/staff/member_list.html",
        {
            "members": members,
            "members_with_no": members_with_no,
            "member_total_count": page_obj.paginator.count,
            "member_list_title": "회비요청 회원",
            "member_list_description": (
                "승인 완료되었지만 회비 납부가 아직 확인되지 않은 회원 목록입니다."
            ),
            "member_list_empty_text": "회비요청 대상 회원이 없습니다.",
            "member_list_mode": "dues-request",
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_members_pending(request: HttpRequest) -> HttpResponse:
    members_queryset = (
        Member.objects.filter(
            status=Member.STATUS_PENDING,
            user__lightning_identity__isnull=False,
        )
        .select_related("user", "user__lightning_identity")
        .order_by("-requested_at")
    )
    page_obj = paginate_items(members_queryset, request.GET.get("page"), per_page=10)
    members = list(page_obj.object_list)
    members_with_no = build_reverse_numbered_rows(page_obj, item_key="member")
    return render(
        request,
        "core/staff/members_pending.html",
        {
            "members": members,
            "members_with_no": members_with_no,
            "pending_total_count": page_obj.paginator.count,
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_members_rejected(request: HttpRequest) -> HttpResponse:
    members_queryset = (
        Member.objects.filter(
            status=Member.STATUS_REJECTED,
            user__lightning_identity__isnull=False,
        )
        .select_related("user", "user__lightning_identity", "reviewed_by")
        .order_by("-reviewed_at", "-requested_at")
    )
    page_obj = paginate_items(members_queryset, request.GET.get("page"), per_page=10)
    members = list(page_obj.object_list)
    members_with_no = build_reverse_numbered_rows(page_obj, item_key="member")
    reviewer_ids = [int(member.reviewed_by_id) for member in members if member.reviewed_by_id]
    reviewer_label_map = _actor_label_map(reviewer_ids)
    rejected_rows = []
    for row in members_with_no:
        member = row["member"]
        reviewer_label = "-"
        if member.reviewed_by_id:
            reviewer_label = reviewer_label_map.get(
                int(member.reviewed_by_id),
                str(member.reviewed_by.get_username()) if member.reviewed_by else "-",
            )
        rejected_rows.append(
            {
                "member": member,
                "row_no": row["row_no"],
                "reviewer_label": reviewer_label,
            }
        )
    return render(
        request,
        "core/staff/members_rejected.html",
        {
            "members": members,
            "members_with_no": rejected_rows,
            "rejected_total_count": page_obj.paginator.count,
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_members_withdrawn(request: HttpRequest) -> HttpResponse:
    rows_queryset = MemberWithdrawalHistory.objects.select_related("withdrawn_by").order_by(
        "-withdrawn_at", "-id"
    )
    page_obj = paginate_items(rows_queryset, request.GET.get("page"), per_page=10)
    rows_with_no = build_reverse_numbered_rows(page_obj)
    rows = [item["row"] for item in rows_with_no]

    actor_user_ids = [int(row.withdrawn_by_id) for row in rows if row.withdrawn_by_id]
    actor_label_map = _actor_label_map(actor_user_ids)
    withdrawn_rows = []
    for item in rows_with_no:
        row = item["row"]
        actor_label = "-"
        if row.withdrawn_by_id:
            actor_label = actor_label_map.get(
                int(row.withdrawn_by_id),
                str(row.withdrawn_by.get_username()) if row.withdrawn_by else "-",
            )
        withdrawn_rows.append(
            {
                "row": row,
                "row_no": item["row_no"],
                "actor_label": actor_label,
            }
        )

    return render(
        request,
        "core/staff/members_withdrawn.html",
        {
            "withdrawn_rows": withdrawn_rows,
            "withdrawn_total_count": page_obj.paginator.count,
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_member_approve(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = get_object_or_404(Member, id=member_id)
    reviewed_at_text = request.POST.get("first_reviewed_at", "").strip()
    reviewed_at = _parse_reviewed_at_or_now(reviewed_at_text)
    review_note = request.POST.get("review_note", "").strip()
    if not review_note:
        messages.error(request, "승인근거를 입력해 주세요.")
        return redirect(
            _resolve_post_next_url(request, reverse("core:staff-members-pending"))
        )
    member.requested_at = reviewed_at
    member.status = Member.STATUS_APPROVED
    member.reviewed_at = reviewed_at
    member.reviewed_by = request.user
    member.review_note = review_note
    member.save()
    ensure_member_domain_records(member)
    lifecycle = member.lifecycle
    if lifecycle.withdrawn_at is not None:
        lifecycle.withdrawn_at = None
        lifecycle.save(update_fields=["withdrawn_at"])
    log_member_review(
        member=member,
        action=MemberReviewLog.ACTION_APPROVE,
        actor=request.user,
        note=review_note,
        acted_at=reviewed_at,
    )
    send_member_status_share_messages(
        member,
        event_type=EVENT_MEMBER_APPROVED,
        request_obj=request,
        actor_name=request.user.get_username(),
        review_note=review_note,
    )
    next_url = _resolve_post_next_url(
        request, reverse("core:staff-members-pending")
    )
    if getattr(request, "htmx", False):
        response = HttpResponse("")
        response["HX-Redirect"] = next_url
        return response
    return redirect(next_url)


@staff_required
def staff_member_request_dues(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = get_object_or_404(Member, id=member_id)
    fallback_url = reverse("core:staff-members-dues-request")
    if member.status != Member.STATUS_APPROVED:
        messages.warning(request, "회비확인 대기 상태 회원만 요청할 수 있습니다.")
        return redirect(_resolve_post_next_url(request, fallback_url))

    try:
        mark_due_request_by_staff(member, actor=request.user)
        messages.success(request, "회비지급요청을 기록했습니다.")
    except DuesPaymentServiceError as exc:
        messages.error(request, f"회비지급요청 처리 중 오류가 발생했습니다: {exc}")
    return redirect(_resolve_post_next_url(request, fallback_url))


@staff_required
def staff_member_reject(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = get_object_or_404(Member, id=member_id)
    reviewed_at_text = request.POST.get("first_reviewed_at", "").strip()
    reviewed_at = _parse_reviewed_at_or_now(reviewed_at_text)
    review_note = request.POST.get("review_note", "").strip()
    if not review_note:
        messages.error(request, "반려근거를 입력해 주세요.")
        return redirect(
            _resolve_post_next_url(request, reverse("core:staff-members-pending"))
        )
    member.status = Member.STATUS_REJECTED
    member.reviewed_at = reviewed_at
    member.reviewed_by = request.user
    member.review_note = review_note
    member.save(update_fields=["status", "reviewed_at", "reviewed_by", "review_note"])
    ensure_member_domain_records(member)
    log_member_review(
        member=member,
        action=MemberReviewLog.ACTION_REJECT,
        actor=request.user,
        note=review_note,
        acted_at=reviewed_at,
    )
    if getattr(request, "htmx", False):
        response = HttpResponse("")
        response["HX-Trigger"] = "searchable-table-refresh"
        return response
    return redirect(
        _resolve_post_next_url(request, reverse("core:staff-members-pending"))
    )


@staff_required
def staff_member_mark_pending(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = get_object_or_404(Member, id=member_id)
    reviewed_at = timezone.now()
    review_note = request.POST.get("review_note", "").strip()
    member.status = Member.STATUS_PENDING
    member.reviewed_at = reviewed_at
    member.reviewed_by = request.user
    member.review_note = review_note
    member.save(update_fields=["status", "reviewed_at", "reviewed_by", "review_note"])
    ensure_member_domain_records(member)
    lifecycle = member.lifecycle
    if lifecycle.withdrawn_at is not None:
        lifecycle.withdrawn_at = None
        lifecycle.save(update_fields=["withdrawn_at"])
    log_member_review(
        member=member,
        action=MemberReviewLog.ACTION_MARK_PENDING,
        actor=request.user,
        note=review_note,
        acted_at=reviewed_at,
    )
    return redirect("core:staff-members-pending")


@staff_required
def staff_member_withdraw(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = get_object_or_404(Member, id=member_id)
    reviewed_at_text = request.POST.get("first_reviewed_at", "").strip()
    withdrew_at = _parse_reviewed_at_or_now(reviewed_at_text)
    review_note = request.POST.get("review_note", "").strip()
    if not review_note:
        messages.error(request, "탈퇴사유를 입력해 주세요.")
        return redirect(_resolve_post_next_url(request, reverse("core:staff-members")))

    withdraw_member_with_history(
        member,
        review_note=review_note,
        withdrew_at=withdrew_at,
        actor=request.user,
    )
    return redirect(_resolve_post_next_url(request, reverse("core:staff-members")))


@staff_required
def staff_member_confirm_dues(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    fallback_url = reverse("core:staff-members-dues-request")
    review_note = request.POST.get("review_note", "").strip()
    if not review_note:
        messages.error(request, "회비확인 사유를 입력해 주세요.")
        return redirect(_resolve_post_next_url(request, fallback_url))

    reviewed_at_text = request.POST.get("first_reviewed_at", "").strip()
    confirmed_at = _parse_reviewed_at_or_now(reviewed_at_text)

    member = get_object_or_404(Member, id=member_id)
    ensure_member_domain_records(member)
    member.status = Member.STATUS_ACTIVE
    member.reviewed_at = confirmed_at
    member.reviewed_by = request.user
    member.review_note = review_note
    member.save(update_fields=["status", "reviewed_at", "reviewed_by", "review_note"])
    settlement = member.settlement
    settlement.dues_paid_at = confirmed_at
    settlement.save(update_fields=["dues_paid_at"])
    lifecycle = member.lifecycle
    lifecycle.signed_up_at = confirmed_at
    if lifecycle.withdrawn_at is not None:
        lifecycle.withdrawn_at = None
        lifecycle.save(update_fields=["signed_up_at", "withdrawn_at"])
    else:
        lifecycle.save(update_fields=["signed_up_at"])
    open_member_year_membership(member, reference_at=confirmed_at)
    latest_payment = (
        member.dues_payments.order_by("-paid_at", "-updated_at", "-id").first()
    )
    payment_note = format_due_payment_review_note(latest_payment)
    log_member_review(
        member=member,
        action=MemberReviewLog.ACTION_CONFIRM_DUES,
        actor=request.user,
        note=_build_confirm_dues_review_note(review_note, payment_note),
        acted_at=confirmed_at,
    )
    dues_amount_sats = None
    if latest_payment is not None:
        dues_amount_sats = int(latest_payment.amount_sats or 0)
    send_member_status_share_messages(
        member,
        event_type=EVENT_MEMBER_DUES_CONFIRMED,
        request_obj=request,
        actor_name=request.user.get_username(),
        review_note=review_note,
        dues_amount_sats=dues_amount_sats,
        payment_note=payment_note,
    )

    redirect_url = _resolve_post_next_url(request, fallback_url)
    if getattr(request, "htmx", False):
        response = HttpResponse("")
        response["HX-Redirect"] = redirect_url
        return response
    return redirect(redirect_url)


@staff_required
def staff_member_detail(request: HttpRequest, member_id: int) -> HttpResponse:
    member = get_object_or_404(_members_with_domains(), id=member_id)
    ensure_member_domain_records(member)

    year_memberships = member.year_memberships.all()
    review_logs = list(
        member.review_logs.select_related("acted_by").all()[:100]
    )
    actor_ids = [int(log.acted_by_id) for log in review_logs if log.acted_by_id]
    actor_label_by_user_id = _actor_label_map(actor_ids)

    latest_payment = (
        member.dues_payments.order_by("-paid_at", "-updated_at", "-id").first()
    )
    fallback_payment_note = format_due_payment_review_note(latest_payment)
    generic_confirm_notes = {"회비확인 처리", "온체인 자동 확인", "라이트닝 자동 확인"}

    review_log_rows = []
    for log in review_logs:
        actor_label = "-"
        if log.acted_by_id:
            actor_label = actor_label_by_user_id.get(
                int(log.acted_by_id),
                str(log.acted_by.get_username()) if log.acted_by else "-",
            )
        actor_nickname, actor_lightning_identifier = _split_actor_label(actor_label)
        note_text = log.note or "-"
        if (
            log.action == MemberReviewLog.ACTION_CONFIRM_DUES
            and note_text in generic_confirm_notes
            and latest_payment is not None
        ):
            note_text = fallback_payment_note
        review_log_rows.append(
            {
                "log": log,
                "actor_label": actor_label,
                "actor_nickname": actor_nickname,
                "actor_lightning_identifier": actor_lightning_identifier,
                "note_text": note_text,
            }
        )

    latest_approval = next(
        (
            row
            for row in review_log_rows
            if row["log"].action == MemberReviewLog.ACTION_APPROVE
        ),
        None,
    )

    return render(
        request,
        "core/staff/member_detail.html",
        {
            "member": member,
            "year_memberships": year_memberships,
            "review_log_rows": review_log_rows,
            "latest_approval": latest_approval,
        },
    )


@staff_required
def staff_dues_month(request: HttpRequest) -> HttpResponse:
    month = parse_month(request.GET.get("month"))
    dues_list = (
        Dues.objects.select_related("member", "member__user")
        .filter(month=month)
        .order_by("member__display_name")
    )
    context = {
        "month": month,
        "dues_list": dues_list,
        "status_counts": dues_list.aggregate(
            pending=Count("id", filter=Q(status=Dues.STATUS_PENDING)),
            submitted=Count("id", filter=Q(status=Dues.STATUS_SUBMITTED)),
            approved=Count("id", filter=Q(status=Dues.STATUS_APPROVED)),
            rejected=Count("id", filter=Q(status=Dues.STATUS_REJECTED)),
            waived=Count("id", filter=Q(status=Dues.STATUS_WAIVED)),
        ),
    }
    return render(request, "core/staff/dues_month.html", context)


def _update_dues_status(
    request: HttpRequest, dues_id: int, status: str
) -> HttpResponse:
    dues = get_object_or_404(Dues, id=dues_id)
    dues.status = status
    dues.reviewed_by = request.user
    dues.admin_note = request.POST.get("admin_note", "")
    if status == Dues.STATUS_APPROVED:
        approved_at = timezone.now()
        dues.approved_at = approved_at
        ensure_member_domain_records(dues.member)
        settlement = dues.member.settlement
        settlement.dues_paid_at = approved_at
        settlement.save(update_fields=["dues_paid_at"])
    dues.save()
    return render(
        request, "core/partials/dues_row.html", {"dues": dues, "staff_mode": True}
    )


@staff_required
def staff_due_approve(request: HttpRequest, dues_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    return _update_dues_status(request, dues_id, Dues.STATUS_APPROVED)


@staff_required
def staff_due_reject(request: HttpRequest, dues_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    return _update_dues_status(request, dues_id, Dues.STATUS_REJECTED)


@staff_required
def staff_due_waive(request: HttpRequest, dues_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    return _update_dues_status(request, dues_id, Dues.STATUS_WAIVED)


@staff_required
def staff_settlements(request: HttpRequest) -> HttpResponse:
    page_obj = paginate_items(
        _activity_settlement_queryset(),
        request.GET.get("page"),
        per_page=10,
    )
    activity_settlement_rows = build_reverse_numbered_rows(page_obj, item_key="activity")
    settlements_rows_url = reverse("core:staff-settlements-rows")
    settlements_rows_url = f"{settlements_rows_url}?page={page_obj.number}"
    return render(
        request,
        "core/staff/settlements.html",
        {
            "activity_settlement_total_count": page_obj.paginator.count,
            "activity_settlement_rows": activity_settlement_rows,
            "activity_settlement_revision": _activity_settlement_revision(),
            "activity_settlement_rows_url": settlements_rows_url,
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


def _activity_settlement_queryset():
    return (
        MemberActivityRequest.objects.select_related("member")
        .filter(
            status__in=[
                MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED,
                MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED,
            ]
        )
        .annotate(
            settlement_status_order=Case(
                When(
                    status=MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED,
                    then=Value(0),
                ),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("settlement_status_order", "-settlement_requested_at", "-id")
    )


def _activity_settlement_revision() -> str:
    summary = _activity_settlement_queryset().aggregate(
        latest_updated_at=Max("updated_at"),
        total_count=Count("id"),
    )
    latest_updated_at = summary.get("latest_updated_at")
    latest_epoch_ms = 0
    if latest_updated_at is not None:
        latest_epoch_ms = int(latest_updated_at.timestamp() * 1000)
    total_count = int(summary.get("total_count") or 0)
    return f"{total_count}:{latest_epoch_ms}"


@staff_required
def staff_settlements_rows(request: HttpRequest) -> HttpResponse:
    page_obj = paginate_items(
        _activity_settlement_queryset(),
        request.GET.get("page"),
        per_page=10,
    )
    activity_settlement_rows = build_reverse_numbered_rows(page_obj, item_key="activity")
    return render(
        request,
        "core/staff/tables/activity_settlements_body.html",
        {
            "activity_settlement_rows": activity_settlement_rows,
        },
    )


@staff_required
def staff_settlements_state(request: HttpRequest) -> HttpResponse:
    return JsonResponse({"revision": _activity_settlement_revision()})


def _build_activity_detail_context(
    request: HttpRequest,
    activity: MemberActivityRequest,
) -> dict[str, object]:
    member = _get_request_member(request)
    all_attachments = list(activity.attachments.all())
    activity_attachments = [
        attachment
        for attachment in all_attachments
        if attachment.attachment_type == MemberActivityAttachment.TYPE_ACTIVITY
    ]
    settlement_evidence_attachments = [
        attachment
        for attachment in all_attachments
        if attachment.attachment_type
        == MemberActivityAttachment.TYPE_SETTLEMENT_EVIDENCE
    ]
    settlement_processor_member = None
    if activity.settlement_processed_by_id:
        settlement_processor_member = Member.objects.filter(
            user_id=activity.settlement_processed_by_id
        ).only("display_name", "lightning_identifier").first()
    return {
        "activity": activity,
        "activity_attachments": activity_attachments,
        "settlement_evidence_attachments": settlement_evidence_attachments,
        "settlement_processor_member": settlement_processor_member,
        "can_manage_activity": _can_manage_activity_request(member, activity),
        "can_request_activity_settlement": _can_request_activity_settlement(
            member, activity
        ),
    }


@staff_required
def staff_settlement_detail(request: HttpRequest, activity_id: int) -> HttpResponse:
    activity = get_object_or_404(
        MemberActivityRequest.objects.select_related("member").prefetch_related(
            "attachments"
        ),
        id=activity_id,
    )
    context = _build_activity_detail_context(request, activity)
    context.update(
        {
            "detail_page_title": "정산 상세",
            "detail_page_description": "정산 요청/처리 내역을 확인합니다.",
            "detail_back_url": reverse("core:staff-settlements"),
        }
    )
    return render(
        request,
        "core/staff/activity_detail.html",
        context,
    )


@staff_required
def staff_settlement_process(request: HttpRequest) -> HttpResponse:
    member = _get_request_member(request)
    if not member.is_backoffice_admin:
        messages.warning(request, "어드민만 사용할 수 있습니다.")
        return redirect("core:staff-settlements")

    settlements_queryset = (
        MemberActivityRequest.objects.select_related("member")
        .prefetch_related("attachments")
        .filter(status=MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED)
        .order_by("-settlement_requested_at", "-updated_at", "-id")
    )
    page_obj = paginate_items(settlements_queryset, request.GET.get("page"), per_page=10)
    rows = list(page_obj.object_list)
    settlement_process_rows = [
        {
            "activity": activity,
            "evidence_attachments": [
                attachment
                for attachment in activity.attachments.all()
                if attachment.attachment_type
                == MemberActivityAttachment.TYPE_SETTLEMENT_EVIDENCE
            ],
        }
        for activity in rows
    ]

    return render(
        request,
        "core/staff/settlement_process.html",
        {
            "settlement_process_rows": settlement_process_rows,
            "settlement_process_total_count": page_obj.paginator.count,
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_settlement_process_execute(
    request: HttpRequest, activity_id: int
) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    is_json = _wants_json_response(request)
    logs: list[str] = []
    logs.append("정산 처리 요청을 시작합니다.")

    member = _get_request_member(request)
    if not member.is_backoffice_admin:
        error_message = "어드민만 사용할 수 있습니다."
        if is_json:
            return JsonResponse(
                {
                    "ok": False,
                    "logs": logs + [error_message],
                    "error": error_message,
                },
                status=403,
            )
        messages.warning(request, error_message)
        return redirect("core:staff-settlement-process")

    logs.append("어드민 권한을 확인했습니다.")
    activity = get_object_or_404(
        MemberActivityRequest.objects.select_related("member"),
        id=activity_id,
    )
    if activity.status != MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED:
        error_message = "정산요청 상태의 활동만 처리할 수 있습니다."
        if is_json:
            return JsonResponse(
                {
                    "ok": False,
                    "logs": logs + [error_message],
                    "error": error_message,
                },
                status=400,
            )
        messages.error(request, error_message)
        return redirect("core:staff-settlement-process")

    logs.append(
        f"대상 활동 확인: #{activity.id}, 금액 {int(activity.amount_sats):,} sats, 주소 {activity.lightning_address}"
    )
    logs.append("블링크 라이트닝 자동 전송을 요청합니다.")
    try:
        transfer_result = send_lightning_payment_to_address(
            amount_sats=int(activity.amount_sats),
            lightning_address=activity.lightning_address,
            memo=f"BSL 활동비 정산 #{activity.id}",
        )
    except DuesPaymentServiceError as exc:
        error_message = str(exc)
        if is_json:
            return JsonResponse(
                {
                    "ok": False,
                    "logs": logs + [f"블링크 전송 실패: {error_message}"],
                    "error": error_message,
                },
                status=400,
            )
        messages.error(request, error_message)
        return redirect("core:staff-settlement-process")

    logs.append(f"블링크 응답 상태: {transfer_result.status}")
    if transfer_result.reference:
        logs.append(f"전송 참조값: {transfer_result.reference}")
    if transfer_result.note:
        logs.append(f"전송 메모: {transfer_result.note}")

    processed_at = timezone.now()
    activity.status = MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED
    activity.settlement_completed_at = processed_at
    activity.settlement_processed_by = request.user
    activity.settlement_processed_at = processed_at
    activity.settlement_transfer_status = transfer_result.status
    activity.settlement_transfer_reference = transfer_result.reference
    activity.settlement_transfer_note = transfer_result.note
    activity.save(
        update_fields=[
            "status",
            "settlement_completed_at",
            "settlement_processed_by",
            "settlement_processed_at",
            "settlement_transfer_status",
            "settlement_transfer_reference",
            "settlement_transfer_note",
            "updated_at",
        ]
    )
    send_activity_share_messages(
        activity,
        event_type=EVENT_SETTLEMENT_COMPLETED,
        request_obj=request,
    )
    logs.append(
        f"정산 완료 저장: 처리자 {request.user.get_username()}, 처리일시 {timezone.localtime(processed_at):%Y-%m-%d %H:%M:%S}"
    )
    success_message = "정산 처리가 완료되었습니다."
    if is_json:
        return JsonResponse(
            {
                "ok": True,
                "logs": logs,
                "message": success_message,
                "redirect_url": reverse("core:staff-settlements"),
            }
        )
    messages.success(request, success_message)
    return redirect("core:staff-settlements")


@staff_required
def staff_settlement_mark_paid(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    member = get_object_or_404(Member, id=member_id)
    ensure_member_domain_records(member)
    settlement = member.settlement
    settlement.dues_paid_at = timezone.now()
    settlement.save(update_fields=["dues_paid_at"])
    return render(request, "core/partials/settlement_row.html", {"member": member})


@staff_required
def staff_settlement_reset(request: HttpRequest, member_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    member = get_object_or_404(Member, id=member_id)
    ensure_member_domain_records(member)
    settlement = member.settlement
    settlement.dues_paid_at = None
    settlement.save(update_fields=["dues_paid_at"])
    return render(request, "core/partials/settlement_row.html", {"member": member})


@staff_required
def staff_activities(request: HttpRequest) -> HttpResponse:
    activities_queryset = (
        MemberActivityRequest.objects.select_related("member")
        .prefetch_related("attachments")
        .filter(status=MemberActivityRequest.STATUS_REGISTERED)
    )
    page_obj = paginate_items(activities_queryset, request.GET.get("page"), per_page=10)
    rows_with_no = build_reverse_numbered_rows(page_obj)
    return render(
        request,
        "core/staff/activities.html",
        {
            "activities_with_no": rows_with_no,
            "activity_total_count": page_obj.paginator.count,
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


@staff_required
def staff_activity_create(request: HttpRequest) -> HttpResponse:
    member = _get_request_member(request)
    form = MemberActivityRequestForm(request.POST or None)
    uploaded_files = request.FILES.getlist("attachments")

    if request.method == "POST":
        attachment_errors = _validate_activity_attachments(uploaded_files)
        if uploaded_files and not is_object_storage_configured():
            attachment_errors.append(
                "첨부파일 업로드를 위해 iwinv 오브젝트 스토리지 설정이 필요합니다."
            )
        if form.is_valid() and not attachment_errors:
            uploaded_object_keys: list[str] = []
            try:
                with transaction.atomic():
                    activity = MemberActivityRequest.objects.create(
                        member=member,
                        title=form.cleaned_data["title"],
                        content_html=form.cleaned_data["content_html"],
                        amount_sats=int(form.cleaned_data["amount_sats"]),
                        lightning_address=form.cleaned_data["lightning_address"],
                        evidence_url=form.cleaned_data["evidence_url"],
                        status=MemberActivityRequest.STATUS_REGISTERED,
                    )
                    for uploaded_file in uploaded_files:
                        upload_result = upload_activity_attachment(uploaded_file, member.id)
                        uploaded_object_keys.append(upload_result.object_key)
                        MemberActivityAttachment.objects.create(
                            activity_request=activity,
                            object_key=upload_result.object_key,
                            original_filename=upload_result.stored_filename,
                            content_type=upload_result.content_type,
                            file_size_bytes=upload_result.file_size_bytes,
                            attachment_type=MemberActivityAttachment.TYPE_ACTIVITY,
                        )
            except ObjectStorageError as exc:
                for object_key in uploaded_object_keys:
                    delete_object(object_key)
                messages.error(request, str(exc))
            else:
                send_activity_share_messages(
                    activity,
                    event_type=EVENT_ACTIVITY_CREATED,
                    request_obj=request,
                )
                messages.success(request, "활동이 등록되었습니다.")
                return redirect("core:staff-activities")
        else:
            for message in attachment_errors:
                form.add_error(None, message)

    return render(
        request,
        "core/staff/activity_form.html",
        {
            "form": form,
            "max_attachments": MAX_ACTIVITY_ATTACHMENTS,
            "max_attachment_size_mb": 10,
            "max_attachment_size_bytes": MAX_ACTIVITY_ATTACHMENT_SIZE_BYTES,
            "max_content_chars": MemberActivityRequestForm.MAX_CONTENT_CHARS,
            "is_edit_mode": False,
        },
    )


@staff_required
def staff_activity_detail(request: HttpRequest, activity_id: int) -> HttpResponse:
    activity = get_object_or_404(
        MemberActivityRequest.objects.select_related("member").prefetch_related(
            "attachments"
        ),
        id=activity_id,
    )
    context = _build_activity_detail_context(request, activity)
    context.update(
        {
            "detail_page_title": "활동 상세",
            "detail_page_description": "등록된 활동의 상세 내용과 첨부를 확인합니다.",
            "detail_back_url": reverse("core:staff-activities"),
        }
    )
    return render(
        request,
        "core/staff/activity_detail.html",
        context,
    )


@staff_required
def staff_activity_edit(request: HttpRequest, activity_id: int) -> HttpResponse:
    activity = get_object_or_404(
        MemberActivityRequest.objects.select_related("member").prefetch_related(
            "attachments"
        ),
        id=activity_id,
    )
    member = _get_request_member(request)
    if not _can_manage_activity_request(member, activity):
        return _activity_permission_denied_response(request, activity)

    form_initial = {
        "title": activity.title,
        "content_html": activity.content_html,
        "amount_sats": activity.amount_sats,
        "lightning_address": activity.lightning_address,
        "evidence_url": activity.evidence_url,
    }
    form = MemberActivityRequestForm(request.POST or None, initial=form_initial)
    uploaded_files = request.FILES.getlist("attachments")

    if request.method == "POST":
        attachment_errors = _validate_activity_attachments(uploaded_files)
        if uploaded_files and not is_object_storage_configured():
            attachment_errors.append(
                "첨부파일 업로드를 위해 iwinv 오브젝트 스토리지 설정이 필요합니다."
            )
        if form.is_valid() and not attachment_errors:
            uploaded_object_keys: list[str] = []
            try:
                with transaction.atomic():
                    activity.title = form.cleaned_data["title"]
                    activity.content_html = form.cleaned_data["content_html"]
                    activity.amount_sats = int(form.cleaned_data["amount_sats"])
                    activity.lightning_address = form.cleaned_data["lightning_address"]
                    activity.evidence_url = form.cleaned_data["evidence_url"]
                    activity.save(
                        update_fields=[
                            "title",
                            "content_html",
                            "amount_sats",
                            "lightning_address",
                            "evidence_url",
                            "updated_at",
                        ]
                    )

                    for uploaded_file in uploaded_files:
                        upload_result = upload_activity_attachment(uploaded_file, activity.member_id)
                        uploaded_object_keys.append(upload_result.object_key)
                        MemberActivityAttachment.objects.create(
                            activity_request=activity,
                            object_key=upload_result.object_key,
                            original_filename=upload_result.stored_filename,
                            content_type=upload_result.content_type,
                            file_size_bytes=upload_result.file_size_bytes,
                            attachment_type=MemberActivityAttachment.TYPE_ACTIVITY,
                        )
            except ObjectStorageError as exc:
                for object_key in uploaded_object_keys:
                    delete_object(object_key)
                messages.error(request, str(exc))
            else:
                messages.success(request, "활동이 수정되었습니다.")
                return redirect("core:staff-activity-detail", activity_id=activity.id)
        else:
            for message in attachment_errors:
                form.add_error(None, message)

    return render(
        request,
        "core/staff/activity_form.html",
        {
            "form": form,
            "activity": activity,
            "max_attachments": MAX_ACTIVITY_ATTACHMENTS,
            "max_attachment_size_mb": 10,
            "max_attachment_size_bytes": MAX_ACTIVITY_ATTACHMENT_SIZE_BYTES,
            "max_content_chars": MemberActivityRequestForm.MAX_CONTENT_CHARS,
            "is_edit_mode": True,
        },
    )


@staff_required
def staff_activity_delete(request: HttpRequest, activity_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    activity = get_object_or_404(
        MemberActivityRequest.objects.prefetch_related("attachments"),
        id=activity_id,
    )
    member = _get_request_member(request)
    if not _can_manage_activity_request(member, activity):
        return _activity_permission_denied_response(request, activity)

    object_keys = list(
        activity.attachments.values_list("object_key", flat=True)
    )
    with transaction.atomic():
        activity.delete()
    for object_key in object_keys:
        delete_object(object_key)

    messages.success(request, "활동이 삭제되었습니다.")
    return redirect("core:staff-activities")


@staff_required
def staff_activity_request_settlement(
    request: HttpRequest, activity_id: int
) -> HttpResponse:
    activity = get_object_or_404(
        MemberActivityRequest.objects.select_related("member").prefetch_related(
            "attachments"
        ),
        id=activity_id,
    )
    member = _get_request_member(request)
    if not _can_request_activity_settlement(member, activity):
        messages.error(request, "작성자만 등록 상태의 활동을 정산요청할 수 있습니다.")
        return redirect("core:staff-activity-detail", activity_id=activity.id)

    form = MemberActivitySettlementRequestForm(request.POST or None)
    uploaded_files = request.FILES.getlist("attachments")

    if request.method == "POST":
        attachment_errors = _validate_activity_attachments(uploaded_files)
        if uploaded_files and not is_object_storage_configured():
            attachment_errors.append(
                "첨부파일 업로드를 위해 iwinv 오브젝트 스토리지 설정이 필요합니다."
            )
        if form.is_valid() and not attachment_errors:
            uploaded_object_keys: list[str] = []
            try:
                with transaction.atomic():
                    activity.status = MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED
                    activity.settlement_result_html = form.cleaned_data[
                        "result_content_html"
                    ]
                    activity.settlement_requested_at = timezone.now()
                    activity.save(
                        update_fields=[
                            "status",
                            "settlement_result_html",
                            "settlement_requested_at",
                            "updated_at",
                        ]
                    )

                    for uploaded_file in uploaded_files:
                        upload_result = upload_activity_attachment(
                            uploaded_file, activity.member_id
                        )
                        uploaded_object_keys.append(upload_result.object_key)
                        MemberActivityAttachment.objects.create(
                            activity_request=activity,
                            object_key=upload_result.object_key,
                            original_filename=upload_result.stored_filename,
                            content_type=upload_result.content_type,
                            file_size_bytes=upload_result.file_size_bytes,
                            attachment_type=MemberActivityAttachment.TYPE_SETTLEMENT_EVIDENCE,
                        )
            except ObjectStorageError as exc:
                for object_key in uploaded_object_keys:
                    delete_object(object_key)
                messages.error(request, str(exc))
            else:
                send_activity_share_messages(
                    activity,
                    event_type=EVENT_SETTLEMENT_REQUESTED,
                    request_obj=request,
                )
                messages.success(request, "정산요청이 등록되었습니다.")
                return redirect("core:staff-settlements")
        else:
            for message in attachment_errors:
                form.add_error(None, message)

    activity_attachments = [
        attachment
        for attachment in activity.attachments.all()
        if attachment.attachment_type == MemberActivityAttachment.TYPE_ACTIVITY
    ]
    return render(
        request,
        "core/staff/activity_settlement_request_form.html",
        {
            "activity": activity,
            "activity_attachments": activity_attachments,
            "form": form,
            "max_attachments": MAX_ACTIVITY_ATTACHMENTS,
            "max_attachment_size_mb": 10,
            "max_attachment_size_bytes": MAX_ACTIVITY_ATTACHMENT_SIZE_BYTES,
            "max_content_chars": MemberActivitySettlementRequestForm.MAX_CONTENT_CHARS,
        },
    )


@staff_required
def staff_activity_complete_settlement(
    request: HttpRequest, activity_id: int
) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    activity = get_object_or_404(MemberActivityRequest, id=activity_id)
    member = _get_request_member(request)
    if not _can_complete_activity_settlement(member, activity):
        messages.error(request, "어드민만 정산요청 상태를 정산완료로 처리할 수 있습니다.")
        return redirect("core:staff-activity-detail", activity_id=activity.id)

    activity.status = MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED
    processed_at = timezone.now()
    activity.settlement_completed_at = processed_at
    if activity.settlement_processed_by_id is None:
        activity.settlement_processed_by = request.user
    if activity.settlement_processed_at is None:
        activity.settlement_processed_at = processed_at
    if not activity.settlement_transfer_note:
        activity.settlement_transfer_note = "수동 정산완료 처리"
    activity.save(
        update_fields=[
            "status",
            "settlement_completed_at",
            "settlement_processed_by",
            "settlement_processed_at",
            "settlement_transfer_note",
            "updated_at",
        ]
    )
    send_activity_share_messages(
        activity,
        event_type=EVENT_SETTLEMENT_COMPLETED,
        request_obj=request,
    )
    messages.success(request, "정산완료 처리되었습니다.")
    return redirect("core:staff-activity-detail", activity_id=activity.id)


@staff_required
def staff_activity_attachment_download(
    request: HttpRequest, attachment_id: int
) -> HttpResponse:
    attachment = get_object_or_404(
        MemberActivityAttachment.objects.select_related("activity_request", "activity_request__member"),
        id=attachment_id,
    )
    try:
        download_url = build_attachment_download_url(attachment.object_key)
    except ObjectStorageError as exc:
        messages.error(request, str(exc))
        return redirect("core:staff-activities")
    return HttpResponseRedirect(download_url)


@staff_required
def staff_activity_attachment_preview(
    request: HttpRequest, attachment_id: int
) -> HttpResponse:
    attachment = get_object_or_404(
        MemberActivityAttachment.objects.select_related(
            "activity_request",
            "activity_request__member",
        ),
        id=attachment_id,
    )
    try:
        object_data = download_object_bytes(attachment.object_key)
    except ObjectStorageError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest("첨부파일 미리보기를 불러오지 못했습니다.")

    response = HttpResponse(
        object_data.body,
        content_type=object_data.content_type or "application/octet-stream",
    )
    response["Content-Disposition"] = f'inline; filename="{attachment.original_filename}"'
    return response
