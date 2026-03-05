from datetime import date
from functools import wraps
from typing import Any

from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Page, Paginator
from django.shortcuts import redirect
from django.utils import timezone

from core.models import Member, ensure_superadmin_member


def resolve_request_member(user) -> Member | None:
    if not bool(getattr(user, "is_authenticated", False)):
        return None
    member = Member.objects.filter(user=user).first()
    if member is None and bool(getattr(user, "is_superuser", False)):
        member = ensure_superadmin_member(user)
    return member


def _staff_check(user: object) -> bool:
    return bool(getattr(user, "is_staff", False))


staff_only_required = user_passes_test(_staff_check)


def member_active_required(view_func):
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        member = resolve_request_member(request.user)
        if member is None:
            return redirect("core:my-dashboard")
        if member.is_backoffice_admin:
            return view_func(request, *args, **kwargs)
        if member.status != Member.STATUS_ACTIVE:
            return redirect("core:my-dashboard")
        return view_func(request, *args, **kwargs)

    return _wrapped


# Backward-compatible alias:
# unless explicitly requested as staff-only, "staff" screens are accessible to authenticated 정회원.
staff_required = member_active_required


def parse_month(month_text: str | None) -> date:
    if not month_text:
        now = timezone.localdate()
        return date(now.year, now.month, 1)
    year_text, month_value = month_text.split("-")
    return date(int(year_text), int(month_value), 1)


def paginate_items(
    items,
    page_number: str | None,
    *,
    per_page: int = 10,
) -> Page:
    paginator = Paginator(items, per_page)
    return paginator.get_page(page_number)


def build_reverse_numbered_rows(
    page_obj: Page,
    *,
    item_key: str = "row",
) -> list[dict[str, Any]]:
    rows = list(page_obj.object_list)
    if not rows:
        return []

    start_no = page_obj.paginator.count - (page_obj.start_index() - 1)
    return [
        {item_key: row, "row_no": start_no - index}
        for index, row in enumerate(rows)
    ]
