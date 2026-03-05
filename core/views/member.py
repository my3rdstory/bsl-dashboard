import json
import re
from pathlib import Path
from html import unescape
from datetime import date
from datetime import datetime
from datetime import timedelta
from secrets import token_hex
from textwrap import shorten
from urllib.parse import quote_plus

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.html import escape
from django.utils.html import strip_tags
from django.urls import reverse

from core.forms import (
    BasicDuesSettingForm,
    DuesSubmitForm,
    MessageShareSettingForm,
    MemberAddressForm,
    MemberNicknameUpdateForm,
    MemberRegistrationForm,
)
from core.models import (
    Dues,
    LightningLoginAttempt,
    Member,
    MemberActivityRequest,
    MemberReviewLog,
    build_monthly_dues_plan,
    calc_monthly_due_sats,
    ensure_member_domain_records,
    get_basic_dues_setting,
    get_message_share_setting,
    get_current_member_address,
    MemberDuesPayment,
    replace_member_address,
)
from core.services import (
    DuesPaymentServiceError,
    check_member_due_payment,
    ensure_member_due_payment,
    get_activity_share_message_examples,
    get_collection_balance_summary,
    get_staff_requested_due_payment,
    send_pending_member_share_messages,
    select_member_due_payment_method,
    submit_member_due_onchain_txid,
    validate_and_resolve_blink_wallet_id,
    withdraw_member_with_history,
)
from .auth import _build_qr_payload
from .common import (
    build_reverse_numbered_rows,
    member_active_required,
    paginate_items,
    resolve_request_member,
)


_README_RENDER_CACHE: dict[str, object] = {
    "path": "",
    "mtime": -1.0,
    "size": -1,
    "html": "",
}


def _get_cached_readme_html(readme_path: Path) -> tuple[str, str]:
    path_key = str(readme_path)
    try:
        stat = readme_path.stat()
        mtime = stat.st_mtime
        size = stat.st_size
        if (
            _README_RENDER_CACHE.get("path") == path_key
            and _README_RENDER_CACHE.get("mtime") == mtime
            and _README_RENDER_CACHE.get("size") == size
        ):
            return "", _README_RENDER_CACHE["html"]  # type: ignore[return-value]
    except OSError as exc:
        return f"README.md 읽기 중 오류가 발생했습니다: {exc}", ""

    try:
        content = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"README.md 읽기 중 오류가 발생했습니다: {exc}", ""

    html = _markdown_to_html(content)
    _README_RENDER_CACHE.update(
        {"path": path_key, "mtime": mtime, "size": size, "html": html}
    )
    return "", html


def _attach_htmx_toast(response: HttpResponse, message: str, level: str = "warning") -> HttpResponse:
    current_trigger = response.headers.get("HX-Trigger", "").strip()
    payload: dict[str, object] = {"app-toast": {"message": str(message), "level": level}}
    if current_trigger:
        try:
            parsed = json.loads(current_trigger)
            if isinstance(parsed, dict):
                parsed.update(payload)
                payload = parsed
        except json.JSONDecodeError:
            payload = {"app-toast": {"message": str(message), "level": level}}
    response["HX-Trigger"] = json.dumps(payload, ensure_ascii=False)
    return response


def _markdown_to_html(markdown_text: str) -> str:
    lines = (
        escape(markdown_text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    )

    def _split_table_row(raw: str) -> list[str]:
        return [cell.strip() for cell in raw.strip().strip("|").split("|")]

    def _is_table_delimiter(text: str) -> bool:
        return bool(
            re.fullmatch(
                r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+(?:\s*:?-{3,}:?\s*)\|?\s*$",
                text.strip(),
            )
        )

    def _is_table_row(text: str) -> bool:
        stripped = text.strip()
        return "|" in stripped and stripped.count("|") >= 1

    def inline(text: str) -> str:
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^\*]+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*([^\*]+?)\*", r"<em>\1</em>", text)
        text = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
            text,
        )
        return text

    html_parts: list[str] = []
    paragraph_buffer: list[str] = []
    list_open = False
    list_tag = "ul"
    in_code_block = False
    code_buffer: list[str] = []
    index = 0
    total = len(lines)

    def flush_paragraph() -> None:
        nonlocal paragraph_buffer
        if not paragraph_buffer:
            return
        paragraph = "<br>".join(paragraph_buffer).strip()
        if paragraph:
            html_parts.append(f"<p>{inline(paragraph)}</p>")
        paragraph_buffer = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            html_parts.append(f"</{list_tag}>")
            list_open = False

    while index < total:
        line = lines[index]
        if line.startswith("```"):
            if in_code_block:
                html_parts.append("<pre><code>" + "\n".join(code_buffer) + "</code></pre>")
                code_buffer = []
                in_code_block = False
            else:
                flush_paragraph()
                close_list()
                in_code_block = True
            continue

        if index + 1 < total and _is_table_row(line) and _is_table_delimiter(lines[index + 1]):
            flush_paragraph()
            close_list()
            header_cells = _split_table_row(line)
            html_parts.append("<table class='readme-table'>")
            html_parts.append("<thead><tr>")
            for header_cell in header_cells:
                html_parts.append(f"<th>{inline(header_cell)}</th>")
            html_parts.append("</tr></thead><tbody>")

            index += 2
            while index < total and _is_table_row(lines[index]):
                if _is_table_delimiter(lines[index]):
                    index += 1
                    continue
                if not lines[index].strip():
                    break
                row_cells = _split_table_row(lines[index])
                html_parts.append("<tr>")
                for row_cell in row_cells:
                    html_parts.append(f"<td>{inline(row_cell)}</td>")
                html_parts.append("</tr>")
                index += 1
            html_parts.append("</tbody></table>")
            continue

        if in_code_block:
            code_buffer.append(line)
            continue

        if not line.strip():
            flush_paragraph()
            close_list()
            continue

        if re.fullmatch(r"\s*---\s*", line):
            flush_paragraph()
            close_list()
            html_parts.append("<hr>")
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading_match:
            flush_paragraph()
            close_list()
            level = len(heading_match.group(1))
            html_parts.append(f"<h{level}>{inline(heading_match.group(2).strip())}</h{level}>")
            continue

        task_match = re.match(r"^([ \t]*)(?:-|\*|\+)\s+\[(x|X| )\]\s+(.*)$", line)
        if task_match:
            checked = task_match.group(2).strip().lower() == "x"
            if not list_open:
                flush_paragraph()
                list_tag = "ul"
                html_parts.append("<ul>")
                list_open = True
            elif list_tag != "ul":
                html_parts.append(f"</{list_tag}>")
                list_tag = "ul"
                html_parts.append("<ul>")

            checked_attr = " checked" if checked else ""
            task_html = (
                "<label class=\"md-task-item\">"
                f"<input type=\"checkbox\" disabled{checked_attr}> "
                f"{inline(task_match.group(3).strip())}"
                "</label>"
            )
            html_parts.append(f"<li>{task_html}</li>")
            index += 1
            continue

        ordered_task_match = re.match(r"^([ \t]*)(\d+)[.)]\s+\[(x|X| )\]\s+(.*)$", line)
        if ordered_task_match:
            checked = ordered_task_match.group(3).strip() in {"x", "X"}
            if not list_open:
                flush_paragraph()
                list_tag = "ol"
                html_parts.append("<ol>")
                list_open = True
            elif list_tag != "ol":
                html_parts.append(f"</{list_tag}>")
                list_tag = "ol"
                html_parts.append("<ol>")

            checked_attr = " checked" if checked else ""
            task_html = (
                "<label class=\"md-task-item\">"
                f"<input type=\"checkbox\" disabled{checked_attr}> "
                f"{inline(ordered_task_match.group(4).strip())}"
                "</label>"
            )
            html_parts.append(f"<li>{task_html}</li>")
            index += 1
            continue

        list_match = re.match(r"^([ \t]*)(?:-|\*|\+)\s+(.*)$", line)
        if list_match:
            if not list_open:
                flush_paragraph()
                list_tag = "ul"
                html_parts.append("<ul>")
                list_open = True
            elif list_tag != "ul":
                html_parts.append(f"</{list_tag}>")
                list_tag = "ul"
                html_parts.append("<ul>")
            html_parts.append(f"<li>{inline(list_match.group(2).strip())}</li>")
            index += 1
            continue

        ordered_match = re.match(r"^([ \t]*)(\d+)[.)]\s+(.*)$", line)
        if ordered_match:
            if not list_open:
                flush_paragraph()
                list_tag = "ol"
                html_parts.append("<ol>")
                list_open = True
            elif list_tag != "ol":
                html_parts.append(f"</{list_tag}>")
                list_tag = "ol"
                html_parts.append("<ol>")
            html_parts.append(f"<li>{inline(ordered_match.group(3).strip())}</li>")
            index += 1
            continue

        close_list()
        paragraph_buffer.append(line)
        index += 1

    flush_paragraph()
    close_list()
    if in_code_block:
        html_parts.append("<pre><code>" + "\n".join(code_buffer) + "</code></pre>")

    return "\n".join(html_parts)


def _qr_image_url(payload: str) -> str:
    return (
        f"https://api.qrserver.com/v1/create-qr-code/?size=640x640&data={quote_plus(payload)}"
        if payload
        else ""
    )


def _build_dues_payment_context(
    member: Member,
    payment: MemberDuesPayment | None,
) -> dict[str, object]:
    if payment is None:
        return {
            "member": member,
            "payment": None,
            "payment_qr_image_url": "",
            "lightning_invoice_expires_in_seconds": None,
            "lightning_invoice_expires_mmss": "",
            "poll_seconds": 1,
            "payment_poll_url": reverse("core:my-dues-payment-poll"),
        }

    poll_seconds = 60 if payment.payment_method == MemberDuesPayment.METHOD_ONCHAIN else 1
    lightning_invoice_expires_in_seconds = None
    lightning_invoice_expires_mmss = ""
    if (
        payment.payment_method == MemberDuesPayment.METHOD_LIGHTNING
        and payment.lightning_invoice_created_at is not None
    ):
        expires_at = payment.lightning_invoice_created_at + timedelta(minutes=2)
        remaining_seconds = int((expires_at - timezone.now()).total_seconds())
        if remaining_seconds < 0:
            remaining_seconds = 0
        lightning_invoice_expires_in_seconds = remaining_seconds
        mm = remaining_seconds // 60
        ss = remaining_seconds % 60
        lightning_invoice_expires_mmss = f"{mm:02d}:{ss:02d}"

    return {
        "member": member,
        "payment": payment,
        "payment_qr_image_url": _qr_image_url(
            payment.destination_onchain_address
            if payment.payment_method == MemberDuesPayment.METHOD_ONCHAIN
            else payment.lightning_invoice
        ),
        "lightning_invoice_expires_in_seconds": lightning_invoice_expires_in_seconds,
        "lightning_invoice_expires_mmss": lightning_invoice_expires_mmss,
        "poll_seconds": poll_seconds,
        "payment_poll_url": reverse("core:my-dues-payment-poll"),
    }


def _build_timeline_rows(
    *,
    limit: int = 12,
    member: Member | None = None,
    include_links: bool = True,
) -> list[dict[str, object]]:
    source_limit = max(int(limit), 1)
    review_logs_qs = MemberReviewLog.objects.select_related("member", "acted_by")
    dues_paid_logs_qs = MemberDuesPayment.objects.select_related("member").filter(
        status=MemberDuesPayment.STATUS_PAID,
        paid_at__isnull=False,
    )
    settlement_logs_qs = MemberActivityRequest.objects.select_related(
        "member",
        "settlement_processed_by",
    ).filter(
        status=MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED,
        settlement_completed_at__isnull=False,
    )
    if member is not None:
        review_logs_qs = review_logs_qs.filter(member=member)
        dues_paid_logs_qs = dues_paid_logs_qs.filter(member=member)
        settlement_logs_qs = settlement_logs_qs.filter(member=member)

    review_logs = list(review_logs_qs.order_by("-acted_at", "-id")[:source_limit])
    dues_paid_logs = list(dues_paid_logs_qs.order_by("-paid_at", "-id")[:source_limit])
    settlement_logs = list(
        settlement_logs_qs.order_by("-settlement_completed_at", "-id")[:source_limit]
    )

    actor_user_ids = [int(log.acted_by_id) for log in review_logs if log.acted_by_id]
    actor_user_ids.extend(
        int(activity.settlement_processed_by_id)
        for activity in settlement_logs
        if activity.settlement_processed_by_id
    )
    actor_names: dict[int, str] = {
        int(row.user_id): str(row.display_name)
        for row in Member.objects.filter(user_id__in=actor_user_ids).only(
            "user_id",
            "display_name",
        )
    }

    rows: list[dict[str, object]] = []
    for log in review_logs:
        actor_label = actor_names.get(int(log.acted_by_id or 0), "")
        if not actor_label and log.acted_by is not None:
            actor_label = log.acted_by.get_username()
        if not actor_label:
            actor_label = "시스템"
        review_note = shorten(str(log.note or "").strip(), width=100, placeholder="...")
        rows.append(
            {
                "at": log.acted_at,
                "event_type": "회원 처리",
                "title": f"{log.member.display_name} {log.get_action_display()}",
                "detail": review_note or "-",
                "actor": actor_label or "-",
                "url": (
                    reverse("core:staff-member-detail", kwargs={"member_id": log.member_id})
                    if include_links
                    else ""
                ),
            }
        )

    for payment in dues_paid_logs:
        method_label = payment.get_payment_method_display() or "-"
        rows.append(
            {
                "at": payment.paid_at,
                "event_type": "회비 납부",
                "title": (
                    f"{payment.member.display_name} {int(payment.amount_sats or 0):,} sats 납부"
                ),
                "detail": f"결제수단 {method_label}",
                "actor": "회원 본인",
                "url": (
                    reverse(
                        "core:staff-member-detail",
                        kwargs={"member_id": payment.member_id},
                    )
                    if include_links
                    else ""
                ),
            }
        )

    for activity in settlement_logs:
        transfer_status = str(activity.settlement_transfer_status or "").strip() or "-"
        actor_label = actor_names.get(int(activity.settlement_processed_by_id or 0), "")
        if not actor_label and activity.settlement_processed_by is not None:
            actor_label = activity.settlement_processed_by.get_username()
        rows.append(
            {
                "at": activity.settlement_completed_at,
                "event_type": "정산 완료",
                "title": (
                    f"{activity.member.display_name} 활동 #{activity.id} "
                    f"{int(activity.amount_sats or 0):,} sats"
                ),
                "detail": f"전송상태 {transfer_status}",
                "actor": actor_label or "-",
                "url": (
                    reverse(
                        "core:staff-settlement-detail",
                        kwargs={"activity_id": activity.id},
                    )
                    if include_links
                    else ""
                ),
            }
        )

    rows.sort(key=lambda row: row["at"], reverse=True)
    return rows[:limit]


def _build_home_timeline_rows(limit: int = 12) -> list[dict[str, object]]:
    return _build_timeline_rows(limit=limit, include_links=True)


def _build_share_message_copy_text(message_html: str) -> str:
    html_text = str(message_html or "").strip()
    if not html_text:
        return ""
    normalized_html = re.sub(
        r"</(p|div|li|h[1-6]|br)\s*>",
        "\n",
        html_text,
        flags=re.IGNORECASE,
    )
    plain_text = unescape(strip_tags(normalized_html))
    lines = [line.strip() for line in plain_text.splitlines()]
    return "\n".join(line for line in lines if line)


def _build_monthly_chart_buckets(
    today: date,
    year: int | None = None,
    *, 
    month_count: int = 12,
) -> list[dict[str, object]]:
    bucket_count = max(1, min(int(month_count), 12))
    target_year = int(year or today.year)
    buckets: list[dict[str, object]] = []
    for month in range(1, bucket_count + 1):
        bucket_year = target_year
        bucket_month = month
        buckets.append(
            {
                "key": f"{bucket_year:04d}-{bucket_month:02d}",
                "label": f"{bucket_month}월",
                "full_label": f"{bucket_year}년 {bucket_month}월",
                "date": date(bucket_year, bucket_month, 1),
            }
        )
    return buckets


def _build_chart_payload(
    month_buckets: list[dict[str, object]],
    value_map: dict[str, int],
) -> dict[str, object]:
    point_count = len(month_buckets)
    if point_count == 0:
        return {
            "items": [],
            "path": "",
            "max_value": 0,
            "point_count": 0,
            "labels": [],
            "values": [],
        }

    values = [
        int(value_map.get(bucket["key"], 0))
        for bucket in month_buckets
    ]
    max_value = max(values) if values else 0
    point_span = 100.0 / point_count
    bar_width = max(2.2, point_span * 0.6)

    plot_top = 14.0
    plot_bottom = 84.0
    plot_height = plot_bottom - plot_top

    items: list[dict[str, object]] = []
    path_points: list[str] = []

    for index, bucket in enumerate(month_buckets):
        value = int(value_map.get(bucket["key"], 0))
        ratio = float(value) / float(max_value) if max_value > 0 else 0.0
        bar_height = plot_height * ratio
        bar_top = plot_bottom - bar_height
        left = point_span * index
        center_x = left + point_span / 2
        bar_left = left + (point_span - bar_width) / 2
        y = plot_bottom - bar_height
        items.append(
            {
                "label": str(bucket["label"]),
                "full_label": str(bucket["full_label"]),
                "value": value,
                "bar_left": f"{bar_left:.2f}",
                "bar_width": f"{bar_width:.2f}",
                "bar_top": f"{bar_top:.2f}",
                "bar_height": f"{bar_height:.2f}",
                "line_x": f"{center_x:.2f}",
                "line_y": f"{y:.2f}",
            }
        )
        path_points.append(f"{center_x:.2f} {y:.2f}")

    chart_path = ""
    if path_points:
        chart_path = f"M {path_points[0]}"
        for path_point in path_points[1:]:
            chart_path += f" L {path_point}"

    return {
        "items": items,
        "path": chart_path,
        "max_value": max_value,
        "point_count": point_count,
        "plot_top": plot_top,
        "plot_bottom": plot_bottom,
        "labels": [str(bucket["label"]) for bucket in month_buckets],
        "values": values,
    }


def _build_public_home_context() -> dict[str, object]:
    today = timezone.localdate()
    target_chart_year = int(today.year)
    now = timezone.now()
    month_buckets = _build_monthly_chart_buckets(
        today,
        year=target_chart_year,
        month_count=12,
    )
    pending_members_qs = Member.objects.filter(status=Member.STATUS_PENDING)
    approved_members_qs = Member.objects.filter(status=Member.STATUS_APPROVED)
    active_members_qs = Member.objects.filter(status=Member.STATUS_ACTIVE)
    requested_settlements_qs = MemberActivityRequest.objects.filter(
        status=MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED
    )
    completed_settlements_qs = MemberActivityRequest.objects.filter(
        status=MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED
    )
    activity_total_qs = MemberActivityRequest.objects.all()

    monthly_bucket_start = datetime.combine(
        month_buckets[0]["date"],
        datetime.min.time(),
    )
    monthly_bucket_end = datetime(
        target_chart_year + 1,
        1,
        1,
        tzinfo=timezone.get_current_timezone(),
    ) - timedelta(microseconds=1)
    monthly_bucket_start_aware = timezone.make_aware(
        monthly_bucket_start,
        timezone.get_current_timezone(),
    )
    monthly_bucket_end_aware = timezone.make_aware(
        monthly_bucket_end.replace(tzinfo=None),
        timezone.get_current_timezone(),
    )

    dues_monthly_totals_qs = (
        MemberDuesPayment.objects.filter(
            status=MemberDuesPayment.STATUS_PAID,
            paid_at__isnull=False,
            paid_at__gte=monthly_bucket_start_aware,
            paid_at__lte=monthly_bucket_end_aware,
        )
        .annotate(month=TruncMonth("paid_at"))
        .values("month")
        .annotate(total=Coalesce(Sum("amount_sats"), 0))
        .values("month", "total")
    )
    dues_monthly_totals: dict[str, int] = {}
    for row in dues_monthly_totals_qs:
        if row["month"] is None:
            continue
        key = row["month"].strftime("%Y-%m")
        dues_monthly_totals[key] = int(row["total"] or 0)

    member_monthly_new_totals_qs = (
        Member.objects.filter(
            requested_at__isnull=False,
            requested_at__gte=monthly_bucket_start_aware,
            requested_at__lte=monthly_bucket_end_aware,
        )
        .annotate(month=TruncMonth("requested_at"))
        .values("month")
        .annotate(total=Coalesce(Count("id"), 0))
        .values("month", "total")
    )
    member_monthly_new: dict[str, int] = {}
    for row in member_monthly_new_totals_qs:
        if row["month"] is None:
            continue
        key = row["month"].strftime("%Y-%m")
        member_monthly_new[key] = int(row["total"] or 0)

    member_monthly_totals: dict[str, int] = {}
    running_total = 0
    for bucket in month_buckets:
        month_key = str(bucket["key"])
        running_total += int(member_monthly_new.get(month_key, 0))
        member_monthly_totals[month_key] = running_total

    dues_chart = _build_chart_payload(month_buckets, dues_monthly_totals)
    member_growth_chart = _build_chart_payload(month_buckets, member_monthly_totals)
    if today.month == 12:
        next_month_start = date(today.year + 1, 1, 1)
    else:
        next_month_start = date(today.year, today.month + 1, 1)
    settlement_completed_month_start = date(today.year, today.month, 1)
    settlement_completed_month_start_aware = timezone.make_aware(
        datetime.combine(
            settlement_completed_month_start,
            datetime.min.time(),
        ),
        timezone.get_current_timezone(),
    )
    settlement_completed_month_end = datetime(
        next_month_start.year,
        next_month_start.month,
        1,
        tzinfo=timezone.get_current_timezone(),
    )
    settlement_completed_month_qs = completed_settlements_qs.filter(
        settlement_completed_at__gte=settlement_completed_month_start_aware,
        settlement_completed_at__lt=settlement_completed_month_end,
    )
    balance_summary = get_collection_balance_summary()
    current_month_start = settlement_completed_month_start_aware
    member_new_month_count = Member.objects.filter(
        requested_at__gte=current_month_start,
        requested_at__lt=settlement_completed_month_end,
    ).count()
    settlement_completed_total_amount = completed_settlements_qs.aggregate(
        total=Coalesce(Sum("amount_sats"), 0)
    )["total"]
    message_share_setting = get_message_share_setting()
    recruiting_message_html = str(
        message_share_setting.recruiting_message_html or ""
    ).strip()

    return {
        "waiting_info_cards": [
            {
                "title": "승인 대기",
                "value": pending_members_qs.count(),
                "unit": "명",
                "caption": "승인대기 회원 화면으로 이동",
                "url": reverse("core:staff-members-pending"),
                "button_label": "승인 처리",
            },
            {
                "title": "회비확인 대기",
                "value": approved_members_qs.count(),
                "unit": "명",
                "caption": "회비요청 회원 화면으로 이동",
                "url": reverse("core:staff-members-dues-request"),
                "button_label": "회비지급요청",
            },
            {
                "title": "정산요청 대기",
                "value": requested_settlements_qs.count(),
                "unit": "건",
                "caption": "정산 처리 화면으로 이동",
                "url": reverse("core:staff-settlement-process"),
                "button_label": "정산 처리",
            },
        ],
        "member_info_cards": [
            {
                "title": "전체 회원",
                "value": Member.objects.count(),
                "unit": "명",
            },
            {
                "title": "활성 회원",
                "value": active_members_qs.count(),
                "unit": "명",
            },
            {
                "title": "이번달 신규 회원",
                "value": member_new_month_count,
                "unit": "명",
            },
        ],
        "activity_info_cards": [
            {
                "title": "활동 현황",
                "value": activity_total_qs.count(),
                "unit": "건",
            },
            {
                "title": "정산요청 대기",
                "value": requested_settlements_qs.count(),
                "unit": "건",
            },
            {
                "title": "정산 완료 건수",
                "value": completed_settlements_qs.count(),
                "unit": "건",
            },
            {
                "title": "정산 완료 총 금액",
                "value": settlement_completed_total_amount,
                "unit": "sats",
            },
        ],
        "activity_info_column_class": "is-12-mobile is-6-tablet is-3-desktop",
        "collection_balance": balance_summary,
        "dues_chart": dues_chart,
        "member_growth_chart": member_growth_chart,
        "now": now,
        "recruiting_message_html": recruiting_message_html,
        "recruiting_message_copy_text": _build_share_message_copy_text(
            recruiting_message_html
        ),
        "recruiting_message_updated_at": message_share_setting.updated_at,
    }


def _get_request_member_or_404(request: HttpRequest) -> Member:
    member = resolve_request_member(request.user)
    if member is None:
        raise Http404("No Member matches the given query.")
    return member


def _build_member_nickname_form(member: Member) -> MemberNicknameUpdateForm:
    return MemberNicknameUpdateForm(initial={"display_name": member.display_name})


def _withdraw_attempt_session_key() -> str:
    return "member_withdraw_attempt_token"


def _withdraw_debug_fake_enabled() -> bool:
    return settings.DEBUG and getattr(settings, "DEBUG_FAKE_LIGHTNING_LOGIN", True)


def _withdraw_debug_wallet_key_for_ui(request: HttpRequest, member: Member) -> str:
    fixed_wallet_key = str(
        getattr(settings, "DEBUG_FAKE_LIGHTNING_FIXED_WALLET_KEY", "")
    ).strip()
    if fixed_wallet_key:
        return fixed_wallet_key
    session_wallet_key = request.session.get("debug_lightning_wallet_key")
    if isinstance(session_wallet_key, str) and session_wallet_key.strip():
        return session_wallet_key.strip()
    return str(member.lightning_identifier or "").strip()


def _create_withdraw_lightning_attempt(
    request: HttpRequest,
) -> LightningLoginAttempt:
    attempt = LightningLoginAttempt.objects.create(
        k1=token_hex(32),
        auth_invoice="",
        qr_payload="",
        expires_at=LightningLoginAttempt.default_expiry(),
    )
    lnurl_code = _build_qr_payload(request, attempt.k1)
    attempt.auth_invoice = lnurl_code
    attempt.qr_payload = lnurl_code
    attempt.save(update_fields=["auth_invoice", "qr_payload"])
    request.session[_withdraw_attempt_session_key()] = str(attempt.token)
    return attempt


def _withdraw_attempt_matches_session(
    request: HttpRequest,
    attempt: LightningLoginAttempt,
) -> bool:
    session_token = request.session.get(_withdraw_attempt_session_key())
    return str(session_token or "") == str(attempt.token)


def _withdraw_wallet_matched(
    attempt: LightningLoginAttempt,
    member: Member,
) -> bool:
    wallet_key = str(attempt.wallet_key or "").strip().lower()
    member_wallet_key = str(member.lightning_identifier or "").strip().lower()
    return bool(wallet_key) and wallet_key == member_wallet_key


def _remaining_lightning_seconds(attempt: LightningLoginAttempt) -> int:
    remaining = int((attempt.expires_at - timezone.now()).total_seconds())
    return max(0, remaining)


def _build_withdraw_auth_context(
    request: HttpRequest,
    member: Member,
    attempt: LightningLoginAttempt,
) -> dict[str, object]:
    return {
        "member": member,
        "attempt": attempt,
        "qr_image_url": _qr_image_url(attempt.qr_payload),
        "remaining_seconds": _remaining_lightning_seconds(attempt),
        "debug_fake_enabled": _withdraw_debug_fake_enabled(),
        "debug_wallet_key": _withdraw_debug_wallet_key_for_ui(request, member),
        "is_wallet_matched": _withdraw_wallet_matched(attempt, member),
    }


@member_active_required
def home(request: HttpRequest) -> HttpResponse:
    current_member = Member.objects.filter(user=request.user).only("status").first()
    if current_member is not None and current_member.status == Member.STATUS_APPROVED:
        return redirect("core:my-dashboard")
    return render(request, "core/home.html", _build_public_home_context())


@member_active_required
def logs_timeline(request: HttpRequest) -> HttpResponse:
    timeline_rows = _build_home_timeline_rows(limit=300)
    page_obj = paginate_items(timeline_rows, request.GET.get("page"), per_page=15)
    return render(
        request,
        "core/logs_timeline.html",
        {
            "timeline_rows": page_obj.object_list,
            "timeline_total_count": len(timeline_rows),
            "page_obj": page_obj,
            "page_param": "page",
        },
    )


def register_member(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("core:my-dashboard")

    if request.method == "POST":
        form = MemberRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            created_member = Member.objects.filter(user=user).first()
            if created_member is not None:
                send_pending_member_share_messages(
                    created_member,
                    request_obj=request,
                )
            login(request, user)
            messages.success(
                request,
                "회원 등록 신청이 접수되었습니다. 관리자의 승인 후 자동 등록이 진행됩니다.",
            )
            return redirect("core:my-dashboard")
    else:
        form = MemberRegistrationForm()

    return render(request, "core/register.html", {"form": form})


@login_required
def my_dashboard(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    ensure_member_domain_records(member)
    if member.status == Member.STATUS_APPROVED:
        requested_payment = get_staff_requested_due_payment(member)
        if requested_payment is None:
            return render(
                request,
                "core/dues_payment_required.html",
                {
                    **_build_dues_payment_context(member, None),
                    "payment_waiting_request_message": (
                        "운영진의 회비지급요청 후에 결제를 진행할 수 있습니다."
                    ),
                    "nickname_form": _build_member_nickname_form(member),
                },
            )
        try:
            payment = ensure_member_due_payment(member)
            checked_payment = check_member_due_payment(member)
            if checked_payment is not None:
                payment = checked_payment
            member.refresh_from_db()
            if member.status == Member.STATUS_ACTIVE:
                return redirect("core:my-dashboard")
            return render(
                request,
                "core/dues_payment_required.html",
                {
                    **_build_dues_payment_context(member, payment),
                    "nickname_form": _build_member_nickname_form(member),
                },
            )
        except DuesPaymentServiceError as exc:
            messages.warning(request, f"회비 납입 준비 중 오류가 발생했습니다: {exc}")
            return render(
                request,
                "core/dues_payment_required.html",
                {
                    **_build_dues_payment_context(member, None),
                    "payment_error_message": str(exc),
                    "nickname_form": _build_member_nickname_form(member),
                },
            )

    active_from_year = None
    yearly_dues_rows: list[dict[str, object]] = []
    member_timeline_rows: list[dict[str, object]] = []
    member_timeline_page_obj = None
    member_timeline_page_param = "timeline_page"
    my_activities_with_no: list[dict[str, object]] = []
    my_activities_page_obj = None
    my_activities_page_param = "my_activity_page"
    my_settlements_with_no: list[dict[str, object]] = []
    my_settlements_page_obj = None
    my_settlements_page_param = "my_settlement_page"
    if member.status == Member.STATUS_ACTIVE:
        signed_up_at = member.lifecycle.signed_up_at
        reference_at = signed_up_at if signed_up_at is not None else member.requested_at
        active_from_year = int(timezone.localtime(reference_at).year)
        paid_payments = (
            member.dues_payments.filter(
                request_month__year__gte=active_from_year,
                status=MemberDuesPayment.STATUS_PAID,
            )
            .order_by("-request_month", "-id")
        )
        yearly_map: dict[int, dict[str, object]] = {}
        for payment in paid_payments:
            payment_year = int(payment.request_month.year)
            row = yearly_map.setdefault(
                payment_year,
                {
                    "year": payment_year,
                    "total_amount_sats": 0,
                    "payment_count": 0,
                    "method_labels": [],
                    "payment_proofs": [],
                },
            )

            if payment.payment_method == MemberDuesPayment.METHOD_ONCHAIN:
                method_label = "온체인"
                proof_value = payment.onchain_txid.strip() or "-"
            elif payment.payment_method == MemberDuesPayment.METHOD_LIGHTNING:
                method_label = "라이트닝"
                proof_value = payment.lightning_invoice.strip() or "-"
            else:
                method_label = "-"
                proof_value = "-"

            row["total_amount_sats"] = int(row["total_amount_sats"]) + int(payment.amount_sats or 0)
            row["payment_count"] = int(row["payment_count"]) + 1
            if method_label not in row["method_labels"]:
                row["method_labels"].append(method_label)
            row["payment_proofs"].append(
                {"method_label": method_label, "proof_value": proof_value}
            )

        yearly_dues_rows = sorted(yearly_map.values(), key=lambda value: int(value["year"]), reverse=True)
        member_timeline_rows = _build_timeline_rows(
            limit=300,
            member=member,
            include_links=False,
        )
        member_timeline_page_obj = paginate_items(
            member_timeline_rows,
            request.GET.get(member_timeline_page_param),
            per_page=5,
        )
        my_activities_queryset = (
            MemberActivityRequest.objects.filter(member=member)
            .order_by("-created_at", "-id")
        )
        my_activities_page_obj = paginate_items(
            my_activities_queryset,
            request.GET.get(my_activities_page_param),
            per_page=5,
        )
        my_activities_with_no = build_reverse_numbered_rows(my_activities_page_obj)

        my_settlements_queryset = (
            MemberActivityRequest.objects.filter(
                member=member,
                status__in=[
                    MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED,
                    MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED,
                ],
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
        my_settlements_page_obj = paginate_items(
            my_settlements_queryset,
            request.GET.get(my_settlements_page_param),
            per_page=5,
        )
        my_settlements_with_no = build_reverse_numbered_rows(
            my_settlements_page_obj,
            item_key="activity",
        )

    return render(
        request,
        "core/my_dashboard.html",
        {
            "member": member,
            "nickname_form": _build_member_nickname_form(member),
            "active_from_year": active_from_year,
            "yearly_dues_rows": yearly_dues_rows,
            "member_timeline_rows": (
                member_timeline_page_obj.object_list if member_timeline_page_obj else []
            ),
            "member_timeline_total_count": len(member_timeline_rows),
            "member_timeline_page_obj": member_timeline_page_obj,
            "member_timeline_page_param": member_timeline_page_param,
            "my_activities_with_no": my_activities_with_no,
            "my_activities_total_count": (
                my_activities_page_obj.paginator.count if my_activities_page_obj else 0
            ),
            "my_activities_page_obj": my_activities_page_obj,
            "my_activities_page_param": my_activities_page_param,
            "my_settlements_with_no": my_settlements_with_no,
            "my_settlements_total_count": (
                my_settlements_page_obj.paginator.count if my_settlements_page_obj else 0
            ),
            "my_settlements_page_obj": my_settlements_page_obj,
            "my_settlements_page_param": my_settlements_page_param,
            "submit_form": DuesSubmitForm(),
        },
    )


@login_required
def my_nickname_update(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = _get_request_member_or_404(request)
    form = MemberNicknameUpdateForm(request.POST)
    if not form.is_valid():
        error_text = (
            form.errors.get("display_name", [None])[0]
            or "닉네임 형식을 확인해 주세요."
        )
        messages.warning(request, error_text)
        return redirect("core:my-dashboard")

    nickname = form.cleaned_data["display_name"]
    if member.display_name == nickname:
        messages.info(request, "동일한 닉네임입니다.")
        return redirect("core:my-dashboard")

    member.display_name = nickname
    member.save(update_fields=["display_name"])
    messages.success(request, "닉네임을 변경했습니다.")
    return redirect("core:my-dashboard")


@login_required
def my_withdraw_request(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    return render(
        request,
        "core/member_withdraw_request.html",
        {
            "member": member,
            "nickname_form": _build_member_nickname_form(member),
        },
    )


@login_required
def my_withdraw_auth_start(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = _get_request_member_or_404(request)
    attempt = _create_withdraw_lightning_attempt(request)
    return render(
        request,
        "core/partials/member_withdraw_auth_panel.html",
        _build_withdraw_auth_context(request, member, attempt),
    )


@login_required
def my_withdraw_auth_poll(request: HttpRequest, token) -> HttpResponse:
    member = _get_request_member_or_404(request)
    attempt = get_object_or_404(LightningLoginAttempt, token=token)
    if not _withdraw_attempt_matches_session(request, attempt):
        return HttpResponseBadRequest("현재 세션의 인증 시도가 아닙니다.")

    if attempt.status == LightningLoginAttempt.STATUS_PENDING and attempt.is_expired():
        attempt.status = LightningLoginAttempt.STATUS_EXPIRED
        attempt.failure_reason = "인증 시간이 만료되었습니다."
        attempt.save(update_fields=["status", "failure_reason"])

    return render(
        request,
        "core/partials/member_withdraw_auth_panel.html",
        _build_withdraw_auth_context(request, member, attempt),
    )


@login_required
def my_withdraw_auth_fake(request: HttpRequest, token) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    if not _withdraw_debug_fake_enabled():
        return HttpResponse(status=404)

    member = _get_request_member_or_404(request)
    attempt = get_object_or_404(LightningLoginAttempt, token=token)
    if not _withdraw_attempt_matches_session(request, attempt):
        return HttpResponseBadRequest("현재 세션의 인증 시도가 아닙니다.")

    if attempt.is_expired():
        attempt.status = LightningLoginAttempt.STATUS_EXPIRED
        attempt.failure_reason = "인증 시간이 만료되었습니다."
        attempt.save(update_fields=["status", "failure_reason"])
        return render(
            request,
            "core/partials/member_withdraw_auth_panel.html",
            _build_withdraw_auth_context(request, member, attempt),
        )

    manual_wallet_key = str(request.POST.get("debug_wallet_key", "")).strip()
    wallet_key = (
        manual_wallet_key[:130]
        if manual_wallet_key
        else _withdraw_debug_wallet_key_for_ui(request, member)[:130]
    )
    if manual_wallet_key:
        request.session["debug_lightning_wallet_key"] = wallet_key

    attempt.wallet_key = wallet_key
    attempt.status = LightningLoginAttempt.STATUS_SUCCESS
    attempt.verified_at = timezone.now()
    attempt.failure_reason = ""
    attempt.user = None
    attempt.require_nickname = False
    attempt.save(
        update_fields=[
            "wallet_key",
            "status",
            "verified_at",
            "failure_reason",
            "user",
            "require_nickname",
        ]
    )
    return render(
        request,
        "core/partials/member_withdraw_auth_panel.html",
        _build_withdraw_auth_context(request, member, attempt),
    )


@login_required
def my_withdraw_execute(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = _get_request_member_or_404(request)
    attempt_token = str(request.POST.get("attempt_token", "")).strip()
    if not attempt_token:
        messages.warning(request, "재인증 정보가 없습니다. 다시 시도해 주세요.")
        return redirect("core:my-withdraw-request")

    attempt = get_object_or_404(LightningLoginAttempt, token=attempt_token)
    if not _withdraw_attempt_matches_session(request, attempt):
        messages.warning(request, "현재 세션의 인증 시도가 아닙니다. 다시 시도해 주세요.")
        return redirect("core:my-withdraw-request")
    if attempt.status != LightningLoginAttempt.STATUS_SUCCESS:
        messages.warning(request, "라이트닝 재인증이 완료되지 않았습니다.")
        return redirect("core:my-withdraw-request")
    if not _withdraw_wallet_matched(attempt, member):
        messages.warning(request, "라이트닝 ID가 일치하지 않아 탈퇴를 진행할 수 없습니다.")
        return redirect("core:my-withdraw-request")

    confirmed = str(request.POST.get("confirm_delete", "")).strip().lower()
    if confirmed not in {"on", "true", "1", "yes"}:
        messages.warning(request, "탈퇴 데이터 삭제 안내를 확인해 주세요.")
        return redirect("core:my-withdraw-request")

    display_name = str(member.display_name)
    lightning_identifier = str(member.lightning_identifier)
    withdraw_member_with_history(
        member,
        review_note="회원 본인 탈퇴(라이트닝 재인증 확인)",
        withdrew_at=timezone.now(),
        actor=request.user,
    )
    request.session.pop(_withdraw_attempt_session_key(), None)
    logout(request)
    return render(
        request,
        "core/member_withdraw_done.html",
        {
            "former_display_name": display_name,
            "former_lightning_identifier": lightning_identifier,
        },
    )


@login_required
def my_dues_payment_select_method(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    member = _get_request_member_or_404(request)
    if member.status != Member.STATUS_APPROVED:
        return HttpResponseBadRequest("회비확인 대기 상태가 아닙니다.")

    if get_staff_requested_due_payment(member) is None:
        action_error_message = "운영진의 회비지급요청 후에 결제를 진행할 수 있습니다."
        if not request.htmx:
            messages.warning(request, action_error_message)
            return redirect("core:my-dashboard")
        response = render(
            request,
            "core/partials/dues_payment_method_panel.html",
            {
                **_build_dues_payment_context(member, None),
                "payment_action_error_message": action_error_message,
            },
        )
        return _attach_htmx_toast(response, action_error_message, level="warning")

    method = request.POST.get("payment_method", "")
    action_error_message = ""
    try:
        payment = select_member_due_payment_method(member, method)
    except DuesPaymentServiceError as exc:
        action_error_message = str(exc)
        if not request.htmx:
            messages.warning(request, action_error_message)
        payment = ensure_member_due_payment(member)

    checked_payment = check_member_due_payment(member)
    if checked_payment is not None:
        payment = checked_payment
    member.refresh_from_db()
    if member.status == Member.STATUS_ACTIVE:
        response = HttpResponse("", status=204)
        response["HX-Redirect"] = reverse("core:my-dashboard")
        return response
    context = _build_dues_payment_context(member, payment)
    if action_error_message:
        context["payment_action_error_message"] = action_error_message
    response = render(
        request,
        "core/partials/dues_payment_method_panel.html",
        context,
    )
    if action_error_message and request.htmx:
        _attach_htmx_toast(response, action_error_message, level="warning")
    return response


@login_required
def my_dues_payment_submit_onchain_txid(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")
    member = _get_request_member_or_404(request)
    if member.status != Member.STATUS_APPROVED:
        return HttpResponseBadRequest("회비확인 대기 상태가 아닙니다.")

    if get_staff_requested_due_payment(member) is None:
        action_error_message = "운영진의 회비지급요청 후에 결제를 진행할 수 있습니다."
        if not request.htmx:
            messages.warning(request, action_error_message)
            return redirect("core:my-dashboard")
        response = render(
            request,
            "core/partials/dues_payment_method_panel.html",
            {
                **_build_dues_payment_context(member, None),
                "payment_action_error_message": action_error_message,
            },
        )
        return _attach_htmx_toast(response, action_error_message, level="warning")

    txid = request.POST.get("onchain_txid", "")
    action_error_message = ""
    try:
        payment = submit_member_due_onchain_txid(member, txid)
    except DuesPaymentServiceError as exc:
        action_error_message = str(exc)
        if not request.htmx:
            messages.warning(request, action_error_message)
        payment = ensure_member_due_payment(member)

    checked_payment = check_member_due_payment(member)
    if checked_payment is not None:
        payment = checked_payment
    member.refresh_from_db()
    if member.status == Member.STATUS_ACTIVE:
        response = HttpResponse("", status=204)
        response["HX-Redirect"] = reverse("core:my-dashboard")
        return response
    context = _build_dues_payment_context(member, payment)
    if action_error_message:
        context["payment_action_error_message"] = action_error_message
    response = render(
        request,
        "core/partials/dues_payment_method_panel.html",
        context,
    )
    if action_error_message and request.htmx:
        _attach_htmx_toast(response, action_error_message, level="warning")
    return response


@login_required
def my_dues_payment_poll(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    if member.status != Member.STATUS_APPROVED:
        response = HttpResponse("", status=204)
        response["HX-Redirect"] = reverse("core:my-dashboard")
        return response

    if get_staff_requested_due_payment(member) is None:
        response = HttpResponse("", status=204)
        response["HX-Redirect"] = reverse("core:my-dashboard")
        return response

    payment = ensure_member_due_payment(member)
    checked_payment = check_member_due_payment(member)
    if checked_payment is not None:
        payment = checked_payment
    member.refresh_from_db()
    if member.status == Member.STATUS_ACTIVE:
        response = HttpResponse("", status=204)
        response["HX-Redirect"] = reverse("core:my-dashboard")
        return response
    return render(
        request,
        "core/partials/dues_payment_method_panel.html",
        _build_dues_payment_context(member, payment),
    )


@login_required
def submit_due(request: HttpRequest, dues_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST 요청만 허용됩니다.")

    member = _get_request_member_or_404(request)
    dues = get_object_or_404(Dues, id=dues_id, member=member)
    form = DuesSubmitForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("납부 식별자를 입력해 주세요.")

    dues.payment_reference = form.cleaned_data["payment_reference"]
    dues.status = Dues.STATUS_SUBMITTED
    dues.submitted_at = timezone.now()
    dues.save(update_fields=["payment_reference", "status", "submitted_at"])

    if request.headers.get("HX-Request"):
        return render(
            request, "core/partials/dues_row.html", {"dues": dues, "staff_mode": False}
        )
    messages.success(request, "납부 제출이 완료되었습니다.")
    return redirect("core:my-dashboard")


@member_active_required
def settings_address(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    if not member.is_backoffice_admin:
        messages.warning(request, "어드민만 사용할 수 있습니다.")
        return redirect("core:home")

    current_address = get_current_member_address(member)
    current_masked_blink_api_key = (
        current_address.masked_blink_api_key if current_address else ""
    )

    if request.method == "POST":
        form = MemberAddressForm(request.POST)
        if form.is_valid():
            bitcoin_onchain_address = form.cleaned_data["bitcoin_onchain_address"]
            blink_lightning_address = form.cleaned_data["blink_lightning_address"]
            blink_api_key_input = form.cleaned_data["blink_api_key"].strip()
            blink_wallet_id_input = form.cleaned_data["blink_wallet_id"].strip()
            current_blink_api_key = (
                current_address.get_blink_api_key() if current_address else ""
            )
            blink_api_key = (
                blink_api_key_input
                if blink_api_key_input
                else current_blink_api_key
            )
            blink_wallet_id = blink_wallet_id_input
            if not blink_api_key:
                form.add_error("blink_api_key", "블링크 API Key를 입력해 주세요.")
            else:
                try:
                    blink_wallet_id = validate_and_resolve_blink_wallet_id(
                        blink_api_key,
                        blink_wallet_id_input,
                    )
                except DuesPaymentServiceError as exc:
                    form.add_error("blink_wallet_id", str(exc))
            if form.errors:
                form.fields["blink_api_key"].widget.attrs["placeholder"] = (
                    "변경 시에만 입력 (미입력 시 기존 키 유지)"
                )
                history_rows = member.address_histories.all()[:100]
                return render(
                    request,
                    "core/settings_address.html",
                    {
                        "form": form,
                        "current_address": current_address,
                        "current_masked_blink_api_key": current_masked_blink_api_key,
                        "history_rows": history_rows,
                    },
                )
            if (
                current_address is not None
                and current_address.bitcoin_onchain_address == bitcoin_onchain_address
                and current_address.lightning_address == blink_lightning_address
                and current_blink_api_key == blink_api_key
                and current_address.blink_wallet_id == blink_wallet_id
            ):
                messages.info(request, "현재 사용 주소와 동일합니다.")
                return redirect("core:settings-address")
            is_replace_action = current_address is not None
            replace_member_address(
                member=member,
                bitcoin_onchain_address=bitcoin_onchain_address,
                blink_lightning_address=blink_lightning_address,
                blink_api_key=blink_api_key,
                blink_wallet_id=blink_wallet_id,
            )
            if is_replace_action:
                messages.success(request, "주소를 교체했습니다.")
            else:
                messages.success(request, "주소를 저장했습니다.")
            return redirect("core:settings-address")
    else:
        form = MemberAddressForm(
            initial={
                "bitcoin_onchain_address": (
                    current_address.bitcoin_onchain_address if current_address else ""
                ),
                "blink_lightning_address": (
                    current_address.lightning_address if current_address else ""
                ),
                "blink_api_key": "",
                "blink_wallet_id": (
                    current_address.blink_wallet_id if current_address else ""
                ),
            }
        )
    if current_address is not None:
        form.fields["blink_api_key"].widget.attrs["placeholder"] = (
            "변경 시에만 입력 (미입력 시 기존 키 유지)"
        )

    history_rows = member.address_histories.all()[:100]
    return render(
        request,
        "core/settings_address.html",
        {
            "form": form,
            "current_address": current_address,
            "current_masked_blink_api_key": current_masked_blink_api_key,
            "history_rows": history_rows,
        },
    )


@member_active_required
def settings_basic(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    if not member.is_backoffice_admin:
        messages.warning(request, "어드민만 사용할 수 있습니다.")
        return redirect("core:home")

    current_setting = get_basic_dues_setting()
    if request.method == "POST":
        form = BasicDuesSettingForm(request.POST)
        if form.is_valid():
            current_setting.annual_dues_sats = form.cleaned_data["annual_dues_sats"]
            current_setting.configured_year = form.cleaned_data["configured_year"]
            current_setting.end_year = form.cleaned_data["end_year"]
            current_setting.next_annual_dues_sats = form.cleaned_data["next_annual_dues_sats"]
            current_setting.save(
                update_fields=[
                    "annual_dues_sats",
                    "configured_year",
                    "end_year",
                    "next_annual_dues_sats",
                ]
            )
            messages.success(request, "회비 기본 설정을 저장했습니다.")
            return redirect("core:settings-basic")
    else:
        form = BasicDuesSettingForm(
            initial={
                "annual_dues_sats": current_setting.annual_dues_sats,
                "configured_year": current_setting.configured_year,
                "end_year": (
                    str(current_setting.end_year)
                    if current_setting.end_year is not None
                    else ""
                ),
                "next_annual_dues_sats": current_setting.next_annual_dues_sats,
            }
        )

    refreshed_setting = get_basic_dues_setting()
    monthly_due_sats = calc_monthly_due_sats(refreshed_setting.annual_dues_sats)
    monthly_plan_rows = build_monthly_dues_plan(
        annual_dues_sats=refreshed_setting.annual_dues_sats,
        year=refreshed_setting.configured_year,
    )
    dues_transition = None
    if (
        refreshed_setting.end_year is not None
        and refreshed_setting.next_annual_dues_sats is not None
    ):
        dues_transition = {
            "current_amount_sats": int(refreshed_setting.annual_dues_sats),
            "current_until_year": int(refreshed_setting.end_year),
            "next_amount_sats": int(refreshed_setting.next_annual_dues_sats),
            "next_from_year": int(refreshed_setting.end_year) + 1,
        }
    return render(
        request,
        "core/settings_basic.html",
        {
            "form": form,
            "current_setting": refreshed_setting,
            "monthly_due_sats": monthly_due_sats,
            "monthly_plan_rows": monthly_plan_rows,
            "dues_transition": dues_transition,
        },
    )


@member_active_required
def settings_message_share(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    if not member.is_backoffice_admin:
        messages.warning(request, "어드민만 사용할 수 있습니다.")
        return redirect("core:home")

    current_setting = get_message_share_setting()
    if request.method == "POST":
        form = MessageShareSettingForm(
            request.POST,
            existing_has_gmail_password=bool(current_setting.gmail_app_password),
        )
        if form.is_valid():
            current_setting.admin_email = form.cleaned_data["admin_email"].strip()
            current_setting.gmail_sender_email = form.cleaned_data[
                "gmail_sender_email"
            ].strip()
            current_setting.gmail_sender_name = form.cleaned_data[
                "gmail_sender_name"
            ].strip()
            submitted_gmail_password = form.cleaned_data["gmail_app_password"].strip()
            if submitted_gmail_password:
                current_setting.gmail_app_password = submitted_gmail_password
            current_setting.discord_channel_id = form.cleaned_data[
                "discord_channel_id"
            ].strip()
            current_setting.send_member_pending_email = bool(
                form.cleaned_data["send_member_pending_email"]
            )
            current_setting.send_member_pending_discord = bool(
                form.cleaned_data["send_member_pending_discord"]
            )
            current_setting.send_member_approved_email = bool(
                form.cleaned_data["send_member_approved_email"]
            )
            current_setting.send_member_approved_discord = bool(
                form.cleaned_data["send_member_approved_discord"]
            )
            current_setting.send_member_dues_confirmed_email = bool(
                form.cleaned_data["send_member_dues_confirmed_email"]
            )
            current_setting.send_member_dues_confirmed_discord = bool(
                form.cleaned_data["send_member_dues_confirmed_discord"]
            )
            current_setting.send_activity_created_email = bool(
                form.cleaned_data["send_activity_created_email"]
            )
            current_setting.send_activity_created_discord = bool(
                form.cleaned_data["send_activity_created_discord"]
            )
            current_setting.send_settlement_requested_email = bool(
                form.cleaned_data["send_settlement_requested_email"]
            )
            current_setting.send_settlement_requested_discord = bool(
                form.cleaned_data["send_settlement_requested_discord"]
            )
            current_setting.send_settlement_completed_email = bool(
                form.cleaned_data["send_settlement_completed_email"]
            )
            current_setting.send_settlement_completed_discord = bool(
                form.cleaned_data["send_settlement_completed_discord"]
            )
            current_setting.recruiting_message_html = form.cleaned_data[
                "recruiting_message_html"
            ].strip()
            current_setting.save(
                update_fields=[
                    "admin_email",
                    "gmail_sender_email",
                    "gmail_sender_name",
                    "gmail_app_password",
                    "discord_channel_id",
                    "send_member_pending_email",
                    "send_member_pending_discord",
                    "send_member_approved_email",
                    "send_member_approved_discord",
                    "send_member_dues_confirmed_email",
                    "send_member_dues_confirmed_discord",
                    "send_activity_created_email",
                    "send_activity_created_discord",
                    "send_settlement_requested_email",
                    "send_settlement_requested_discord",
                    "send_settlement_completed_email",
                    "send_settlement_completed_discord",
                    "recruiting_message_html",
                    "updated_at",
                ]
            )
            messages.success(request, "메시지 공유 설정을 저장했습니다.")
            return redirect("core:settings-message-share")
    else:
        form = MessageShareSettingForm(
            initial={
                "admin_email": current_setting.admin_email,
                "gmail_sender_email": current_setting.gmail_sender_email,
                "gmail_sender_name": current_setting.gmail_sender_name,
                "gmail_app_password": "",
                "discord_channel_id": current_setting.discord_channel_id,
                "send_member_pending_email": current_setting.send_member_pending_email,
                "send_member_pending_discord": current_setting.send_member_pending_discord,
                "send_member_approved_email": current_setting.send_member_approved_email,
                "send_member_approved_discord": current_setting.send_member_approved_discord,
                "send_member_dues_confirmed_email": current_setting.send_member_dues_confirmed_email,
                "send_member_dues_confirmed_discord": current_setting.send_member_dues_confirmed_discord,
                "send_activity_created_email": current_setting.send_activity_created_email,
                "send_activity_created_discord": current_setting.send_activity_created_discord,
                "send_settlement_requested_email": current_setting.send_settlement_requested_email,
                "send_settlement_requested_discord": current_setting.send_settlement_requested_discord,
                "send_settlement_completed_email": current_setting.send_settlement_completed_email,
                "send_settlement_completed_discord": current_setting.send_settlement_completed_discord,
                "recruiting_message_html": current_setting.recruiting_message_html,
            },
            existing_has_gmail_password=bool(current_setting.gmail_app_password),
        )

    message_examples = get_activity_share_message_examples()
    message_examples_map = {
        str(example.get("event_type", "")).strip(): example
        for example in message_examples
    }

    return render(
        request,
        "core/settings_message_share.html",
        {
            "form": form,
            "current_setting": current_setting,
            "message_examples": message_examples,
            "message_examples_map": message_examples_map,
        },
    )


@member_active_required
def settings_readme(request: HttpRequest) -> HttpResponse:
    member = _get_request_member_or_404(request)
    if not member.is_backoffice_admin:
        messages.warning(request, "어드민만 사용할 수 있습니다.")
        return redirect("core:home")

    readme_path = Path(__file__).resolve().parents[2] / "README.md"
    readme_html = ""
    readme_error = ""
    if not readme_path.exists():
        readme_error = "README.md 파일을 찾을 수 없습니다."
    else:
        readme_error, readme_html = _get_cached_readme_html(readme_path)

    return render(
        request,
        "core/settings_readme.html",
        {
            "readme_error": readme_error,
            "readme_html": readme_html,
        },
    )
