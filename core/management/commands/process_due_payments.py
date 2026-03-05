from django.core.management.base import BaseCommand

from core.models import MemberDuesPayment
from core.services import check_member_due_payment


class Command(BaseCommand):
    help = "회비 납입 대기 건(온체인/라이트닝)을 점검해 자동 승인 상태를 반영합니다."

    def handle(self, *args, **options):
        pending_rows = (
            MemberDuesPayment.objects.filter(
                status__in=[
                    MemberDuesPayment.STATUS_WAITING_ONCHAIN,
                    MemberDuesPayment.STATUS_WAITING_LIGHTNING,
                ]
            )
            .select_related("member")
            .order_by("created_at")
        )
        processed = 0
        for row in pending_rows:
            check_member_due_payment(row.member)
            processed += 1
        self.stdout.write(self.style.SUCCESS(f"점검 완료: {processed}건"))

