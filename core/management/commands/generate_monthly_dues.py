from argparse import ArgumentParser
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core.models import Dues, Member, resolve_monthly_dues_amount


class Command(BaseCommand):
    help = "활성 회원의 월 회비 레코드를 생성합니다. 여러 번 실행해도 중복 생성되지 않습니다."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--month", type=str, default="", help="대상 월(YYYY-MM)")
        parser.add_argument("--amount", type=int, default=None)

    def handle(self, *args: object, **options: object) -> None:
        month_text = str(options.get("month", ""))
        target_month = self._parse_month(month_text)
        option_amount = options.get("amount")
        if option_amount is None:
            amount = resolve_monthly_dues_amount(target_month)
        else:
            amount = int(option_amount)

        created_count = 0
        skipped_count = 0
        active_members = Member.objects.filter(status=Member.STATUS_ACTIVE)

        for member in active_members:
            if member.paid_until_month and member.paid_until_month >= target_month:
                skipped_count += 1
                continue

            _, created = Dues.objects.get_or_create(
                member=member,
                month=target_month,
                defaults={"amount": amount, "status": Dues.STATUS_PENDING},
            )
            if created:
                created_count += 1
            else:
                skipped_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"완료: {target_month:%Y-%m} 회비 생성 {created_count}건, 스킵 {skipped_count}건"
            )
        )

    def _parse_month(self, month_text: str) -> date:
        if not month_text:
            today = date.today()
            return date(today.year, today.month, 1)
        try:
            year_text, month_value = month_text.split("-")
            return date(int(year_text), int(month_value), 1)
        except ValueError as exc:
            raise CommandError("--month 형식은 YYYY-MM 이어야 합니다.") from exc
