from __future__ import annotations

from datetime import date
from math import ceil

from django.db import models
from django.utils import timezone


def _current_year() -> int:
    return int(timezone.localdate().year)


class BasicDuesSetting(models.Model):
    annual_dues_sats = models.PositiveIntegerField(default=100_000)
    configured_year = models.PositiveIntegerField(default=_current_year)
    end_year = models.PositiveIntegerField(null=True, blank=True)
    next_annual_dues_sats = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "기본 회비 설정"
        verbose_name_plural = "기본 회비 설정"

    def __str__(self) -> str:
        end_year_text = str(self.end_year) if self.end_year is not None else "제한없음"
        return f"{self.annual_dues_sats} sats / {self.configured_year} / {end_year_text}"


def normalize_dues_setting_end_year(
    setting: BasicDuesSetting,
    reference_year: int | None = None,
) -> BasicDuesSetting:
    current_year = int(reference_year or _current_year())
    if setting.end_year is not None and current_year > int(setting.end_year):
        next_annual_dues_sats = (
            int(setting.next_annual_dues_sats)
            if setting.next_annual_dues_sats is not None
            else int(setting.annual_dues_sats)
        )
        setting.annual_dues_sats = next_annual_dues_sats
        setting.configured_year = int(setting.end_year) + 1
        setting.end_year = None
        setting.next_annual_dues_sats = None
        setting.save(
            update_fields=[
                "annual_dues_sats",
                "configured_year",
                "end_year",
                "next_annual_dues_sats",
                "updated_at",
            ]
        )
    return setting


def get_basic_dues_setting() -> BasicDuesSetting:
    setting = BasicDuesSetting.objects.order_by("-id").first()
    if setting is None:
        setting = BasicDuesSetting.objects.create()
    return normalize_dues_setting_end_year(setting)


def calc_monthly_due_sats(annual_dues_sats: int) -> int:
    if annual_dues_sats <= 0:
        return 0
    return int(ceil(annual_dues_sats / 12.0))


def resolve_monthly_dues_amount(target_month: date | None = None) -> int:
    setting = get_basic_dues_setting()
    if target_month is None:
        target_month = timezone.localdate().replace(day=1)
    normalize_dues_setting_end_year(setting, reference_year=target_month.year)
    monthly_plan = build_monthly_dues_plan(
        annual_dues_sats=int(setting.annual_dues_sats),
        year=int(target_month.year),
    )
    target_month_number = int(target_month.month)
    for row in monthly_plan:
        if int(row["month"]) == target_month_number:
            return int(row["applied_fee_sats"])
    return int(setting.annual_dues_sats)


def build_monthly_dues_plan(
    annual_dues_sats: int,
    year: int,
) -> list[dict[str, object]]:
    monthly_due_sats = calc_monthly_due_sats(annual_dues_sats)
    running_total_fee = int(annual_dues_sats)
    rows: list[dict[str, object]] = []
    for month in range(1, 13):
        base_fee_sats = running_total_fee
        planned_deduction_sats = 0 if month == 1 else min(monthly_due_sats, base_fee_sats)
        applied_fee_sats = max(base_fee_sats - planned_deduction_sats, 0)
        running_total_fee = applied_fee_sats
        rows.append(
            {
                "month": month,
                "due_date": date(year, month, 1),
                "base_fee_sats": base_fee_sats,
                "planned_deduction_sats": planned_deduction_sats,
                "applied_fee_sats": applied_fee_sats,
                # backward-compatible keys
                "fee_sats": applied_fee_sats,
                "total_fee_sats": applied_fee_sats,
            }
        )
    return rows
