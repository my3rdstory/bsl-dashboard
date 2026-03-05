import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    BasicDuesSetting,
    Dues,
    LightningIdentity,
    LightningLoginAttempt,
    Member,
    MemberActivityRequest,
    MemberAddressHistory,
    MemberDuesPayment,
    MemberReviewLog,
    MemberWithdrawalHistory,
    build_monthly_dues_plan,
    ensure_member_domain_records,
    get_basic_dues_setting,
    get_message_share_setting,
    resolve_monthly_dues_amount,
)
from core.services import (
    DuesPaymentServiceError,
    check_member_due_payment,
    ensure_member_due_payment,
    format_due_payment_review_note,
    select_member_due_payment_method,
    submit_member_due_onchain_txid,
)
from core.utils import encrypt_blink_api_key


class LightningFirstLoginFlowTests(TestCase):
    def _create_pending_attempt(self) -> LightningLoginAttempt:
        attempt = LightningLoginAttempt.objects.create(
            k1="b" * 64,
            status=LightningLoginAttempt.STATUS_PENDING,
            auth_invoice="lnurl-test",
            qr_payload="lnurl-test",
            expires_at=timezone.now() + timedelta(minutes=2),
        )
        session = self.client.session
        session[f"lightning_next_{attempt.token}"] = "/dashboard/"
        session.save()
        return attempt

    def _create_success_attempt(self) -> LightningLoginAttempt:
        attempt = LightningLoginAttempt.objects.create(
            k1="a" * 64,
            status=LightningLoginAttempt.STATUS_SUCCESS,
            auth_invoice="lnurl-test",
            qr_payload="lnurl-test",
            wallet_key="wallet_key_test",
            require_nickname=True,
            expires_at=timezone.now() + timedelta(minutes=2),
        )
        session = self.client.session
        session[f"lightning_next_{attempt.token}"] = "/dashboard/"
        session.save()
        return attempt

    def test_first_login_requires_second_step_to_create_member_application(self):
        attempt = self._create_success_attempt()

        first_response = self.client.post(
            reverse("core:lightning-login-complete", args=[attempt.token]),
            {"nickname": "테스트닉"},
        )

        self.assertEqual(first_response.status_code, 200)
        attempt.refresh_from_db()
        self.assertFalse(attempt.require_nickname)
        self.assertIsNone(attempt.user_id)
        self.assertEqual(Member.objects.count(), 0)
        self.assertContains(first_response, "자기소개 저장 후 신청 완료")

        second_response = self.client.post(
            reverse("core:lightning-login-complete", args=[attempt.token]),
            {
                "application_note": (
                    "안녕하세요. 비트코인 커뮤니티에서 활동 중입니다.\n"
                    "https://x.com/test_account"
                )
            },
        )

        self.assertEqual(second_response.status_code, 204)
        self.assertEqual(second_response["HX-Redirect"], "/dashboard/")
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.user_id)
        member = Member.objects.get(user_id=attempt.user_id)
        self.assertEqual(member.display_name, "테스트닉")
        self.assertIn("https://x.com/test_account", member.application_note)
        self.assertTrue(
            LightningIdentity.objects.filter(user_id=attempt.user_id).exists()
        )

    def test_first_login_rejects_risky_special_chars_in_nickname(self):
        attempt = self._create_success_attempt()

        response = self.client.post(
            reverse("core:lightning-login-complete", args=[attempt.token]),
            {"nickname": "위험<script>닉네임"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "닉네임에 사용할 수 없는 특수문자가 포함되어 있습니다.",
            status_code=400,
        )
        attempt.refresh_from_db()
        self.assertTrue(attempt.require_nickname)
        self.assertIsNone(attempt.user_id)

    @override_settings(DEBUG=True, DEBUG_FAKE_LIGHTNING_LOGIN=True)
    def test_debug_fake_auth_reuses_member_with_same_lightning_identifier(self):
        user = User.objects.create_user(username="legacy_ln_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="기존닉네임",
            lightning_identifier="debug_wallet_legacy_001",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        self.assertFalse(
            LightningIdentity.objects.filter(user=user).exists()
        )

        attempt = self._create_pending_attempt()
        response = self.client.post(
            reverse("core:lightning-login-fake", args=[attempt.token]),
            {"debug_wallet_key": "debug_wallet_legacy_001"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], "/dashboard/")
        attempt.refresh_from_db()
        self.assertEqual(attempt.user_id, user.id)
        self.assertFalse(attempt.require_nickname)
        identity = LightningIdentity.objects.get(user=user)
        self.assertEqual(identity.wallet_key, "debug_wallet_legacy_001")

    def test_callback_treats_wallet_key_case_insensitively(self):
        user = User.objects.create_user(username="ln_case_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="케이스회원",
            lightning_identifier="02ab" + ("cd" * 31),
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        wallet_key_lower = "02ab" + ("cd" * 31)
        LightningIdentity.objects.create(
            user=user,
            wallet_key=wallet_key_lower,
        )
        attempt = self._create_pending_attempt()

        with patch("core.views.auth.verify_signature", return_value=True):
            response = self.client.get(
                reverse("core:lightning-callback"),
                {
                    "tag": "login",
                    "k1": attempt.k1,
                    "key": wallet_key_lower.upper(),
                    "sig": "aa" * 64,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "OK"})
        attempt.refresh_from_db()
        self.assertEqual(attempt.user_id, user.id)
        self.assertFalse(attempt.require_nickname)

    def test_callback_missing_signature_with_nonstandard_wallet_payload(self):
        attempt = self._create_pending_attempt()

        response = self.client.get(
            reverse("core:lightning-callback"),
            {
                "tag": "login",
                "k1": attempt.k1,
                "action": "login",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "status": "ERROR",
                "reason": "Missing key or signature",
                "code": "missing_params",
            },
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, LightningLoginAttempt.STATUS_FAILED)
        self.assertEqual(
            attempt.failure_reason,
            "이 지갑은 LNURL 인증 응답 형식이 달라 key/sig를 받지 못했습니다.",
        )

    @patch("core.views.auth.verify_signature", return_value=True)
    def test_callback_fallback_supports_pubkey_and_signature_field(self, _mock_verify):
        user = User.objects.create_user(username="ln_alias_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="별칭회원",
            lightning_identifier="02ef" + ("89" * 31),
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        LightningIdentity.objects.create(
            user=user,
            wallet_key="02ef" + ("89" * 31),
        )
        attempt = self._create_pending_attempt()

        response = self.client.post(
            reverse("core:lightning-callback"),
            data=json.dumps(
                {
                    "tag": "login",
                    "k1": attempt.k1,
                    "pubKey": "02ef" + ("89" * 31),
                    "signature": "aa" * 64,
                    "action": "login",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "OK"})
        attempt.refresh_from_db()
        self.assertEqual(attempt.user_id, user.id)
        self.assertFalse(attempt.require_nickname)


class SuperAdminLoginRouteTests(TestCase):
    def test_superadmin_login_route_allows_superuser(self):
        superuser = User.objects.create_superuser(
            username="boss_admin",
            email="boss@example.com",
            password="testpass123!@#",
        )

        response = self.client.post(
            reverse("superadmin-login"),
            {
                "username": "boss_admin",
                "password": "testpass123!@#",
                "next": "/back-office/admin/",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/back-office/admin/")
        self.assertEqual(
            self.client.session.get("_auth_user_id"),
            str(superuser.id),
        )
        member = Member.objects.get(user=superuser)
        self.assertTrue(member.is_backoffice_admin)
        self.assertEqual(member.status, Member.STATUS_ACTIVE)
        self.assertTrue(member.lightning_identifier.startswith("superadmin_"))

    def test_superadmin_login_route_blocks_non_superuser(self):
        User.objects.create_user(
            username="normal_user",
            password="testpass123!@#",
        )

        response = self.client.post(
            reverse("superadmin-login"),
            {
                "username": "normal_user",
                "password": "testpass123!@#",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "수퍼어드민 계정만 로그인할 수 있습니다.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_superadmin_without_member_can_open_my_dashboard(self):
        superuser = User.objects.create_superuser(
            username="legacy_admin",
            email="legacy@example.com",
            password="testpass123!@#",
        )
        self.client.force_login(superuser)

        response = self.client.get(reverse("core:my-dashboard"))

        self.assertEqual(response.status_code, 200)
        member = Member.objects.get(user=superuser)
        self.assertTrue(member.is_backoffice_admin)


class MemberWithdrawalFlowTests(TestCase):
    def test_withdraw_deletes_member_and_keeps_history(self):
        actor = User.objects.create_user(username="reviewer", password="testpass123")
        Member.objects.create(
            user=actor,
            display_name="운영자",
            lightning_identifier="wallet_reviewer_actor",
            status=Member.STATUS_ACTIVE,
        )
        target = User.objects.create(username="withdraw_target")
        target.set_unusable_password()
        target.save(update_fields=["password"])

        member = Member.objects.create(
            user=target,
            display_name="탈퇴대상",
            lightning_identifier="withdraw_wallet_key",
            status=Member.STATUS_ACTIVE,
            application_note="탈퇴 테스트용 소개",
        )
        ensure_member_domain_records(member)
        LightningIdentity.objects.create(
            user=target,
            wallet_key="withdraw_wallet_key",
        )

        self.client.force_login(actor)
        response = self.client.post(
            reverse("core:staff-member-withdraw", args=[member.id]),
            {
                "next": "/members/",
                "first_reviewed_at": "2026-02-09T10:11:12",
                "review_note": "개인 사정으로 탈퇴 요청",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/members/")
        self.assertFalse(Member.objects.filter(id=member.id).exists())
        self.assertFalse(User.objects.filter(id=target.id).exists())
        self.assertFalse(
            LightningIdentity.objects.filter(wallet_key="withdraw_wallet_key").exists()
        )

        history = MemberWithdrawalHistory.objects.get()
        self.assertEqual(history.former_display_name, "탈퇴대상")
        self.assertEqual(history.former_lightning_identifier, "withdraw_wallet_key")
        self.assertEqual(history.withdraw_note, "개인 사정으로 탈퇴 요청")
        self.assertEqual(history.withdrawn_by_id, actor.id)

    def test_member_self_withdraw_deletes_member_related_data_and_keeps_history(self):
        user = User.objects.create_user(username="self_withdraw_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="본인탈퇴회원",
            lightning_identifier="self_withdraw_wallet_key",
            status=Member.STATUS_ACTIVE,
            application_note="본인 탈퇴 테스트",
        )
        ensure_member_domain_records(member)
        LightningIdentity.objects.create(
            user=user,
            wallet_key="self_withdraw_wallet_key",
        )
        Dues.objects.create(
            member=member,
            month=date(2026, 2, 1),
            amount=91_666,
            status=Dues.STATUS_PENDING,
        )
        MemberDuesPayment.objects.create(
            member=member,
            request_month=date(2026, 2, 1),
            amount_sats=91_666,
            status=MemberDuesPayment.STATUS_READY,
            requested_by_staff_at=timezone.now(),
        )
        MemberActivityRequest.objects.create(
            member=member,
            title="탈퇴 전 활동",
            content_html="<p>활동 기록</p>",
            amount_sats=12_345,
            lightning_address="self@blink.test",
            evidence_url="https://example.com/evidence",
            status=MemberActivityRequest.STATUS_REGISTERED,
        )
        attempt = LightningLoginAttempt.objects.create(
            k1="c" * 64,
            status=LightningLoginAttempt.STATUS_SUCCESS,
            auth_invoice="lnurl-test",
            qr_payload="lnurl-test",
            wallet_key="self_withdraw_wallet_key",
            expires_at=timezone.now() + timedelta(minutes=2),
            require_nickname=False,
        )
        session = self.client.session
        session["member_withdraw_attempt_token"] = str(attempt.token)
        session.save()

        self.client.force_login(user)
        response = self.client.post(
            reverse("core:my-withdraw-execute"),
            {
                "attempt_token": str(attempt.token),
                "confirm_delete": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "탈퇴가 완료되었습니다.")
        self.assertFalse(Member.objects.filter(id=member.id).exists())
        self.assertFalse(User.objects.filter(id=user.id).exists())
        self.assertFalse(
            LightningIdentity.objects.filter(wallet_key="self_withdraw_wallet_key").exists()
        )
        self.assertFalse(Dues.objects.filter(member_id=member.id).exists())
        self.assertFalse(MemberDuesPayment.objects.filter(member_id=member.id).exists())
        self.assertFalse(MemberActivityRequest.objects.filter(member_id=member.id).exists())

        history = MemberWithdrawalHistory.objects.get()
        self.assertEqual(history.former_display_name, "본인탈퇴회원")
        self.assertEqual(history.former_lightning_identifier, "self_withdraw_wallet_key")
        self.assertIn("회원 본인 탈퇴", history.withdraw_note)

    def test_member_self_withdraw_blocks_when_reauth_wallet_mismatch(self):
        user = User.objects.create_user(username="self_withdraw_mismatch_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="탈퇴불일치회원",
            lightning_identifier="self_withdraw_expected_wallet",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        attempt = LightningLoginAttempt.objects.create(
            k1="d" * 64,
            status=LightningLoginAttempt.STATUS_SUCCESS,
            auth_invoice="lnurl-test",
            qr_payload="lnurl-test",
            wallet_key="other_wallet_key",
            expires_at=timezone.now() + timedelta(minutes=2),
            require_nickname=False,
        )
        session = self.client.session
        session["member_withdraw_attempt_token"] = str(attempt.token)
        session.save()

        self.client.force_login(user)
        response = self.client.post(
            reverse("core:my-withdraw-execute"),
            {
                "attempt_token": str(attempt.token),
                "confirm_delete": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:my-withdraw-request"))
        self.assertTrue(Member.objects.filter(id=member.id).exists())
        self.assertEqual(MemberWithdrawalHistory.objects.count(), 0)

    @override_settings(DEBUG=True, DEBUG_FAKE_LIGHTNING_LOGIN=True)
    def test_member_withdraw_allows_debug_fake_reauth(self):
        user = User.objects.create_user(username="self_withdraw_debug_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="탈퇴디버그회원",
            lightning_identifier="self_withdraw_debug_wallet",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        self.client.force_login(user)

        start_response = self.client.post(reverse("core:my-withdraw-auth-start"))
        self.assertEqual(start_response.status_code, 200)
        attempt_token = str(self.client.session.get("member_withdraw_attempt_token", ""))
        self.assertTrue(attempt_token)

        fake_response = self.client.post(
            reverse("core:my-withdraw-auth-fake", args=[attempt_token]),
            {"debug_wallet_key": "self_withdraw_debug_wallet"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(fake_response.status_code, 200)
        self.assertContains(fake_response, "라이트닝 재인증이 완료되었습니다.")
        self.assertContains(fake_response, "회원 탈퇴")


class MemberStaffListFlowTests(TestCase):
    def _create_active_actor(self, username: str) -> User:
        user = User.objects.create_user(username=username, password="testpass123")
        Member.objects.create(
            user=user,
            display_name=f"{username}_운영자",
            lightning_identifier=f"wallet_{username}_actor",
            status=Member.STATUS_ACTIVE,
        )
        return user

    def _create_member(
        self,
        *,
        username: str,
        display_name: str,
        wallet_key: str,
        status: str = Member.STATUS_ACTIVE,
        dues_paid: bool = False,
    ) -> Member:
        user = User.objects.create(username=username)
        user.set_unusable_password()
        user.save(update_fields=["password"])

        member = Member.objects.create(
            user=user,
            display_name=display_name,
            lightning_identifier=wallet_key,
            status=status,
            application_note="테스트 가입 신청",
        )
        ensure_member_domain_records(member)
        LightningIdentity.objects.create(user=user, wallet_key=wallet_key)

        if dues_paid:
            settlement = member.settlement
            settlement.dues_paid_at = timezone.now()
            settlement.save(update_fields=["dues_paid_at"])

        return member

    def test_member_list_only_shows_paid_active_members(self):
        actor = self._create_active_actor("reviewer")
        self.client.force_login(actor)

        self._create_member(
            username="paid_user",
            display_name="납부완료회원",
            wallet_key="wallet_paid_user",
            dues_paid=True,
        )
        self._create_member(
            username="unpaid_user",
            display_name="미납부회원",
            wallet_key="wallet_unpaid_user",
            status=Member.STATUS_APPROVED,
            dues_paid=False,
        )
        self._create_member(
            username="pending_user",
            display_name="승인대기회원",
            wallet_key="wallet_pending_user",
            status=Member.STATUS_PENDING,
            dues_paid=False,
        )

        paid_response = self.client.get(reverse("core:staff-members"))
        self.assertEqual(paid_response.status_code, 200)
        self.assertContains(paid_response, "납부완료회원")
        self.assertNotContains(paid_response, "미납부회원")
        self.assertNotContains(paid_response, "승인대기회원")

        dues_request_response = self.client.get(reverse("core:staff-members-dues-request"))
        self.assertEqual(dues_request_response.status_code, 200)
        self.assertContains(dues_request_response, "미납부회원")
        self.assertNotContains(dues_request_response, "납부완료회원")
        self.assertNotContains(dues_request_response, "승인대기회원")

    def test_approve_from_pending_stays_on_pending_list(self):
        actor = self._create_active_actor("approver")
        self.client.force_login(actor)
        pending_member = self._create_member(
            username="pending_target",
            display_name="승인대상회원",
            wallet_key="wallet_pending_target",
            status=Member.STATUS_PENDING,
            dues_paid=False,
        )

        with patch("core.views.staff.send_member_status_share_messages") as share_sender:
            response = self.client.post(
                reverse("core:staff-member-approve", args=[pending_member.id]),
                {
                    "next": "/members/pending/",
                    "first_reviewed_at": "2026-02-09T10:11:12",
                    "review_note": "가입 승인",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("core:staff-members-pending"),
        )
        self.assertEqual(share_sender.call_count, 1)
        self.assertEqual(
            share_sender.call_args.kwargs.get("event_type"),
            "member_approved",
        )
        pending_member.refresh_from_db()
        self.assertEqual(pending_member.status, Member.STATUS_APPROVED)
        self.assertEqual(
            timezone.localtime(pending_member.requested_at).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-02-09 10:11:12",
        )
        self.assertEqual(pending_member.year_memberships.count(), 0)

        dues_request_response = self.client.get(reverse("core:staff-members-dues-request"))
        self.assertEqual(dues_request_response.status_code, 200)
        self.assertContains(dues_request_response, "2026-02-09")
        self.assertContains(dues_request_response, "91,666 sats")

    def test_confirm_dues_moves_member_to_paid_member_list(self):
        actor = self._create_active_actor("dues_reviewer")
        self.client.force_login(actor)
        target = self._create_member(
            username="dues_target",
            display_name="회비확인대상",
            wallet_key="wallet_dues_target",
            status=Member.STATUS_APPROVED,
            dues_paid=False,
        )

        with patch("core.views.staff.send_member_status_share_messages") as share_sender:
            response = self.client.post(
                reverse("core:staff-member-confirm-dues", args=[target.id]),
                {
                    "next": "/members/dues-request/",
                    "first_reviewed_at": "2026-02-10T11:12:13",
                    "review_note": "운영자 직권 회비확인",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/members/dues-request/")
        self.assertEqual(share_sender.call_count, 1)
        self.assertEqual(
            share_sender.call_args.kwargs.get("event_type"),
            "member_dues_confirmed",
        )

        target.refresh_from_db()
        self.assertEqual(target.status, Member.STATUS_ACTIVE)
        self.assertEqual(target.reviewed_by_id, actor.id)
        self.assertEqual(target.review_note, "운영자 직권 회비확인")
        self.assertIsNotNone(target.settlement.dues_paid_at)
        self.assertEqual(
            timezone.localtime(target.settlement.dues_paid_at).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-02-10 11:12:13",
        )
        self.assertEqual(target.year_memberships.count(), 1)
        latest_log = target.review_logs.first()
        self.assertIsNotNone(latest_log)
        self.assertEqual(latest_log.action, MemberReviewLog.ACTION_CONFIRM_DUES)
        self.assertEqual(latest_log.acted_by_id, actor.id)
        self.assertIn("운영자 직권 회비확인", latest_log.note)

        paid_response = self.client.get(reverse("core:staff-members"))
        self.assertContains(paid_response, "회비확인대상")

        dues_request_response = self.client.get(reverse("core:staff-members-dues-request"))
        self.assertNotContains(dues_request_response, "회비확인대상")

    def test_confirm_dues_requires_review_note(self):
        actor = self._create_active_actor("dues_reviewer_no_note")
        self.client.force_login(actor)
        target = self._create_member(
            username="dues_target_no_note",
            display_name="회비확인사유필수대상",
            wallet_key="wallet_dues_target_no_note",
            status=Member.STATUS_APPROVED,
            dues_paid=False,
        )

        response = self.client.post(
            reverse("core:staff-member-confirm-dues", args=[target.id]),
            {
                "next": "/members/dues-request/",
                "first_reviewed_at": "2026-02-10T11:12:13",
                "review_note": "   ",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/members/dues-request/")

        target.refresh_from_db()
        self.assertEqual(target.status, Member.STATUS_APPROVED)
        self.assertEqual(target.year_memberships.count(), 0)
        self.assertIsNone(target.settlement.dues_paid_at)
        self.assertFalse(
            target.review_logs.filter(action=MemberReviewLog.ACTION_CONFIRM_DUES).exists()
        )

    def test_dues_request_button_changes_to_completed_label_after_request(self):
        actor = self._create_active_actor("dues_request_actor")
        self.client.force_login(actor)
        target = self._create_member(
            username="dues_request_target",
            display_name="회비요청대상",
            wallet_key="wallet_dues_request_target",
            status=Member.STATUS_APPROVED,
            dues_paid=False,
        )

        before_response = self.client.get(reverse("core:staff-members-dues-request"))
        self.assertEqual(before_response.status_code, 200)
        self.assertContains(before_response, "회비지급요청")
        self.assertNotContains(before_response, "회비요청완료")

        request_response = self.client.post(
            reverse("core:staff-member-request-dues", args=[target.id]),
            {"next": "/members/dues-request/"},
        )
        self.assertEqual(request_response.status_code, 302)
        self.assertEqual(request_response["Location"], "/members/dues-request/")

        target.refresh_from_db()
        requested_payment = target.dues_payments.first()
        self.assertIsNotNone(requested_payment)
        self.assertIsNotNone(requested_payment.requested_by_staff_at)

        after_response = self.client.get(reverse("core:staff-members-dues-request"))
        self.assertEqual(after_response.status_code, 200)
        self.assertContains(after_response, "회비요청완료")
        self.assertContains(after_response, 'type="submit"')

        rerequest_response = self.client.post(
            reverse("core:staff-member-request-dues", args=[target.id]),
            {"next": "/members/dues-request/"},
        )
        self.assertEqual(rerequest_response.status_code, 302)
        self.assertEqual(rerequest_response["Location"], "/members/dues-request/")


class MemberMyDashboardViewTests(TestCase):
    def test_my_dashboard_shows_payment_method_and_identifier_columns(self):
        user = User.objects.create_user(username="dashboard_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="마이페이지회원",
            lightning_identifier="dashboard_wallet_key",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        member.lifecycle.signed_up_at = timezone.make_aware(datetime(2026, 1, 15, 9, 0, 0))
        member.lifecycle.save(update_fields=["signed_up_at"])

        MemberDuesPayment.objects.create(
            member=member,
            request_month=date(2026, 2, 1),
            amount_sats=91_666,
            payment_method=MemberDuesPayment.METHOD_ONCHAIN,
            status=MemberDuesPayment.STATUS_PAID,
            onchain_txid="a" * 64,
            paid_at=timezone.now(),
        )
        MemberDuesPayment.objects.create(
            member=member,
            request_month=date(2026, 3, 1),
            amount_sats=83_332,
            payment_method=MemberDuesPayment.METHOD_LIGHTNING,
            status=MemberDuesPayment.STATUS_PAID,
            lightning_invoice="lnbc1testinvoice",
            paid_at=timezone.now(),
        )
        MemberDuesPayment.objects.create(
            member=member,
            request_month=date(2025, 12, 1),
            amount_sats=100_000,
            payment_method=MemberDuesPayment.METHOD_ONCHAIN,
            status=MemberDuesPayment.STATUS_PAID,
            onchain_txid="b" * 64,
            paid_at=timezone.now(),
        )

        self.client.force_login(user)
        response = self.client.get(reverse("core:my-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "연도별 회비")
        self.assertContains(response, "174,998 sats")
        self.assertContains(response, "온체인")
        self.assertContains(response, "라이트닝")
        self.assertContains(response, "a" * 64)
        self.assertContains(response, "lnbc1testinvoice")
        self.assertNotContains(response, "b" * 64)
        self.assertNotContains(response, '<th class="num-cell">승인</th>')
        self.assertNotContains(response, '<th class="num-cell">제출</th>')
        self.assertNotContains(response, '<th class="num-cell">대기</th>')
        self.assertNotContains(response, '<th class="num-cell">반려</th>')
        self.assertNotContains(response, '<th class="num-cell">면제</th>')

    def test_my_dashboard_can_update_nickname_with_emoji(self):
        user = User.objects.create_user(username="nickname_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="기존닉네임",
            lightning_identifier="nickname_wallet_key",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:my-nickname-update"),
            {"display_name": "새별명 😀"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:my-dashboard"))
        member.refresh_from_db()
        self.assertEqual(member.display_name, "새별명 😀")

    def test_my_dashboard_rejects_risky_special_chars_in_nickname(self):
        user = User.objects.create_user(username="nickname_user_2", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="안전닉네임",
            lightning_identifier="nickname_wallet_key_2",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(member)
        self.client.force_login(user)

        response = self.client.post(
            reverse("core:my-nickname-update"),
            {"display_name": "안전<script>아님"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:my-dashboard"))
        member.refresh_from_db()
        self.assertEqual(member.display_name, "안전닉네임")

    def test_approved_member_waits_for_staff_due_request_before_payment_screen(self):
        user = User.objects.create_user(username="approved_wait_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="요청대기회원",
            lightning_identifier="approved_wait_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(member)
        self.client.force_login(user)

        response = self.client.get(reverse("core:my-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "운영진의 회비지급요청 후에 결제를 진행할 수 있습니다.")
        self.assertNotContains(response, 'name="payment_method" value="onchain"')
        self.assertNotContains(response, 'name="payment_method" value="lightning"')

    def test_approved_member_with_staff_due_request_sees_payment_methods(self):
        user = User.objects.create_user(username="approved_ready_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="요청완료회원",
            lightning_identifier="approved_ready_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(member)
        payment = ensure_member_due_payment(member)
        payment.requested_by_staff_at = timezone.now()
        payment.save(update_fields=["requested_by_staff_at", "updated_at"])
        self.client.force_login(user)

        response = self.client.get(reverse("core:my-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "온체인 결제")
        self.assertContains(response, "라이트닝 결제")


class TimelineActorDisplayTests(TestCase):
    def test_timeline_rows_show_actor_for_system_member_and_settlement(self):
        from core.views.member import _build_timeline_rows

        processor_user = User.objects.create_user(
            username="timeline_processor",
            password="testpass123",
        )
        processor_member = Member.objects.create(
            user=processor_user,
            display_name="정산처리자",
            lightning_identifier="timeline_processor_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(processor_member)

        target_user = User.objects.create_user(
            username="timeline_target",
            password="testpass123",
        )
        target_member = Member.objects.create(
            user=target_user,
            display_name="타임라인대상회원",
            lightning_identifier="timeline_target_wallet",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(target_member)

        now = timezone.now()
        MemberReviewLog.objects.create(
            member=target_member,
            action=MemberReviewLog.ACTION_CONFIRM_DUES,
            note="온체인 자동 확인",
            acted_at=now - timedelta(minutes=3),
            acted_by=None,
        )
        MemberDuesPayment.objects.create(
            member=target_member,
            request_month=date(2026, 2, 1),
            amount_sats=91_666,
            payment_method=MemberDuesPayment.METHOD_ONCHAIN,
            status=MemberDuesPayment.STATUS_PAID,
            onchain_txid="a" * 64,
            paid_at=now - timedelta(minutes=2),
        )
        activity = MemberActivityRequest.objects.create(
            member=target_member,
            title="타임라인 정산 활동",
            content_html="<p>활동 내용</p>",
            amount_sats=12_000,
            lightning_address="target@blink.test",
            evidence_url="https://example.com/evidence",
            status=MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED,
            settlement_completed_at=now - timedelta(minutes=1),
            settlement_processed_by=processor_user,
            settlement_transfer_status="success",
        )

        rows = _build_timeline_rows(limit=20, include_links=False)

        review_row = next(
            row
            for row in rows
            if row["event_type"] == "회원 처리"
            and "회비확인" in str(row["title"])
            and "타임라인대상회원" in str(row["title"])
        )
        self.assertEqual(review_row["actor"], "시스템")

        payment_row = next(
            row
            for row in rows
            if row["event_type"] == "회비 납부"
            and "타임라인대상회원" in str(row["title"])
        )
        self.assertEqual(payment_row["actor"], "회원 본인")

        settlement_row = next(
            row
            for row in rows
            if row["event_type"] == "정산 완료"
            and f"활동 #{activity.id}" in str(row["title"])
        )
        self.assertEqual(settlement_row["actor"], "정산처리자")


class MemberAddressSettingTests(TestCase):
    @staticmethod
    def _wallets_response(wallet_rows: list[dict[str, str]]) -> dict[str, object]:
        return {
            "data": {
                "me": {
                    "defaultAccount": {
                        "wallets": wallet_rows,
                    }
                }
            }
        }

    def test_settings_address_encrypts_and_masks_blink_api_key(self):
        user = User.objects.create_user(username="address_admin", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="주소관리자",
            lightning_identifier="address_admin_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(member)

        self.client.force_login(user)
        with patch(
            "core.services.dues_payment._http_json",
            return_value=self._wallets_response(
                [
                    {"id": "wallet-001", "walletCurrency": "BTC"},
                    {"id": "wallet-002", "walletCurrency": "USD"},
                ]
            ),
        ):
            response = self.client.post(
                reverse("core:settings-address"),
                {
                    "bitcoin_onchain_address": "bc1qexample0000000000000000000000000000",
                    "blink_lightning_address": "blink@example.com",
                    "blink_api_key": "blink_api_key_1234567890",
                    "blink_wallet_id": "wallet-001",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:settings-address"))

        saved = MemberAddressHistory.objects.get(member=member, is_current=True)
        self.assertEqual(saved.lightning_address, "blink@example.com")
        self.assertEqual(saved.blink_wallet_id, "wallet-001")
        self.assertNotEqual(saved.blink_api_key_encrypted, "blink_api_key_1234567890")
        self.assertEqual(saved.get_blink_api_key(), "blink_api_key_1234567890")

        page = self.client.get(reverse("core:settings-address"))
        self.assertContains(page, saved.masked_blink_api_key)
        self.assertNotContains(page, "blink_api_key_1234567890")

    def test_settings_address_keeps_existing_blink_api_key_when_empty_on_replace(self):
        user = User.objects.create_user(username="address_admin_keep", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="주소관리자2",
            lightning_identifier="address_admin_wallet_2",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(member)

        self.client.force_login(user)
        with patch(
            "core.services.dues_payment._http_json",
            return_value=self._wallets_response(
                [
                    {"id": "wallet-old", "walletCurrency": "BTC"},
                    {"id": "wallet-new", "walletCurrency": "BTC"},
                ]
            ),
        ):
            create_response = self.client.post(
                reverse("core:settings-address"),
                {
                    "bitcoin_onchain_address": "bc1qold0000000000000000000000000000000",
                    "blink_lightning_address": "blink-old@example.com",
                    "blink_api_key": "blink_api_key_keep_me",
                    "blink_wallet_id": "wallet-old",
                },
            )
        self.assertEqual(create_response.status_code, 302)

        with patch(
            "core.services.dues_payment._http_json",
            return_value=self._wallets_response(
                [
                    {"id": "wallet-old", "walletCurrency": "BTC"},
                    {"id": "wallet-new", "walletCurrency": "BTC"},
                ]
            ),
        ):
            replace_response = self.client.post(
                reverse("core:settings-address"),
                {
                    "bitcoin_onchain_address": "bc1qnew0000000000000000000000000000000",
                    "blink_lightning_address": "blink-new@example.com",
                    "blink_api_key": "",
                    "blink_wallet_id": "wallet-new",
                },
            )
        self.assertEqual(replace_response.status_code, 302)
        self.assertEqual(replace_response["Location"], reverse("core:settings-address"))

        current = MemberAddressHistory.objects.get(member=member, is_current=True)
        self.assertEqual(current.bitcoin_onchain_address, "bc1qnew0000000000000000000000000000000")
        self.assertEqual(current.lightning_address, "blink-new@example.com")
        self.assertEqual(current.blink_wallet_id, "wallet-new")
        self.assertEqual(current.get_blink_api_key(), "blink_api_key_keep_me")

    def test_settings_address_auto_selects_btc_wallet_when_wallet_id_is_empty(self):
        user = User.objects.create_user(username="address_admin_auto", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="주소자동선택관리자",
            lightning_identifier="address_auto_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(member)

        self.client.force_login(user)
        with patch(
            "core.services.dues_payment._http_json",
            return_value=self._wallets_response(
                [
                    {"id": "usd-wallet", "walletCurrency": "USD"},
                    {"id": "btc-wallet-auto", "walletCurrency": "BTC"},
                ]
            ),
        ):
            response = self.client.post(
                reverse("core:settings-address"),
                {
                    "bitcoin_onchain_address": "bc1qauto0000000000000000000000000000000",
                    "blink_lightning_address": "blink-auto@example.com",
                    "blink_api_key": "blink_api_key_auto",
                    "blink_wallet_id": "",
                },
            )

        self.assertEqual(response.status_code, 302)
        current = MemberAddressHistory.objects.get(member=member, is_current=True)
        self.assertEqual(current.blink_wallet_id, "btc-wallet-auto")

    def test_settings_address_rejects_wallet_id_not_in_account(self):
        user = User.objects.create_user(username="address_admin_invalid", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="주소검증관리자",
            lightning_identifier="address_invalid_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(member)

        self.client.force_login(user)
        with patch(
            "core.services.dues_payment._http_json",
            return_value=self._wallets_response(
                [
                    {"id": "btc-wallet-1", "walletCurrency": "BTC"},
                ]
            ),
        ):
            response = self.client.post(
                reverse("core:settings-address"),
                {
                    "bitcoin_onchain_address": "bc1qinvalid000000000000000000000000000000",
                    "blink_lightning_address": "blink-invalid@example.com",
                    "blink_api_key": "blink_api_key_invalid",
                    "blink_wallet_id": "wallet-not-exist",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "입력한 블링크 Wallet ID가 API Key 계정에 없습니다.")
        self.assertFalse(MemberAddressHistory.objects.filter(member=member).exists())


class OnchainUnconfirmedApprovalTests(TestCase):
    def test_onchain_unconfirmed_tx_is_accepted_as_paid(self):
        admin_user = User.objects.create_user(username="collector_admin", password="testpass123")
        admin_member = Member.objects.create(
            user=admin_user,
            display_name="수납관리자",
            lightning_identifier="collector_admin_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(admin_member)
        MemberAddressHistory.objects.create(
            member=admin_member,
            bitcoin_onchain_address="tb1qcollect0000000000000000000000000000000",
            lightning_address="collector@blink.test",
            blink_api_key_encrypted="",
            blink_wallet_id="collector-wallet",
            is_current=True,
        )

        member_user = User.objects.create_user(username="due_user", password="testpass123")
        due_member = Member.objects.create(
            user=member_user,
            display_name="회비회원",
            lightning_identifier="due_member_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(due_member)

        payment = ensure_member_due_payment(due_member)
        payment.requested_by_staff_at = timezone.now()
        payment.save(update_fields=["requested_by_staff_at", "updated_at"])
        submit_member_due_onchain_txid(due_member, "a" * 64)

        with (
            patch("core.services.dues_payment._http_json") as mocked_http_json,
            patch("core.services.dues_payment.send_member_status_share_messages") as share_sender,
        ):
            mocked_http_json.return_value = {
                "vout": [
                    {
                        "scriptpubkey_address": payment.destination_onchain_address,
                        "value": int(payment.amount_sats),
                    }
                ],
                "status": {"confirmed": False},
            }
            checked = check_member_due_payment(due_member)

        self.assertIsNotNone(checked)
        self.assertEqual(share_sender.call_count, 1)
        self.assertEqual(
            share_sender.call_args.kwargs.get("event_type"),
            "member_dues_confirmed",
        )
        self.assertEqual(
            int(share_sender.call_args.kwargs.get("dues_amount_sats") or 0),
            int(payment.amount_sats),
        )
        checked.refresh_from_db()
        due_member.refresh_from_db()
        self.assertEqual(checked.status, checked.STATUS_PAID)
        self.assertEqual(checked.check_note, "온체인 입금 확인 완료 (미확정)")
        self.assertEqual(due_member.status, Member.STATUS_ACTIVE)
        latest_log = due_member.review_logs.first()
        self.assertIsNotNone(latest_log)
        self.assertEqual(latest_log.action, MemberReviewLog.ACTION_CONFIRM_DUES)
        self.assertEqual(latest_log.note, f"온체인: {'a' * 64}")


class LightningPaymentSelectionTests(TestCase):
    @staticmethod
    def _mark_staff_due_requested(member: Member) -> None:
        payment = ensure_member_due_payment(member)
        payment.requested_by_staff_at = timezone.now()
        payment.save(update_fields=["requested_by_staff_at", "updated_at"])

    def test_lightning_selection_uses_latest_current_address_fallback(self):
        address_owner_user = User.objects.create_user(
            username="address_owner_fallback",
            password="testpass123",
        )
        address_owner_member = Member.objects.create(
            user=address_owner_user,
            display_name="주소보유회원",
            lightning_identifier="address_owner_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=False,
        )
        ensure_member_domain_records(address_owner_member)
        MemberAddressHistory.objects.create(
            member=address_owner_member,
            bitcoin_onchain_address="tb1qfallback0000000000000000000000000000000",
            lightning_address="fallback@blink.test",
            blink_api_key_encrypted="",
            blink_wallet_id="fallback-wallet",
            is_current=True,
        )

        payer_user = User.objects.create_user(username="payer_member", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="납부회원",
            lightning_identifier="payer_member_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self._mark_staff_due_requested(payer_member)

        def _mock_issue_invoice(payment, collection_address):
            self.assertEqual(collection_address.member_id, address_owner_member.id)
            return ("lnbc1fallbackinvoice", "fallback-hash")

        with patch("core.services.dues_payment._issue_blink_invoice", side_effect=_mock_issue_invoice):
            payment = select_member_due_payment_method(
                payer_member,
                MemberDuesPayment.METHOD_LIGHTNING,
            )

        self.assertEqual(payment.payment_method, MemberDuesPayment.METHOD_LIGHTNING)
        self.assertEqual(payment.status, MemberDuesPayment.STATUS_WAITING_LIGHTNING)
        self.assertEqual(payment.lightning_invoice, "lnbc1fallbackinvoice")

    def test_lightning_selection_auto_resolves_wallet_id_when_empty(self):
        admin_user = User.objects.create_user(username="blink_auto_admin", password="testpass123")
        admin_member = Member.objects.create(
            user=admin_user,
            display_name="자동선택관리자",
            lightning_identifier="blink_auto_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(admin_member)
        address = MemberAddressHistory.objects.create(
            member=admin_member,
            bitcoin_onchain_address="tb1qauto000000000000000000000000000000000",
            lightning_address="auto-admin@blink.test",
            blink_api_key_encrypted=encrypt_blink_api_key("blink_auto_api_key"),
            blink_wallet_id="",
            is_current=True,
        )

        payer_user = User.objects.create_user(username="payer_auto_wallet", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="자동선택납부회원",
            lightning_identifier="payer_auto_wallet_id",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self._mark_staff_due_requested(payer_member)

        def _mock_http_json(_url, *, method="GET", headers=None, payload=None, timeout_sec=10):
            del method, headers, timeout_sec
            query = str((payload or {}).get("query", ""))
            if "query MeWallets" in query:
                return {
                    "data": {
                        "me": {
                            "defaultAccount": {
                                "wallets": [
                                    {"id": "usd-wallet", "walletCurrency": "USD"},
                                    {"id": "btc-auto-wallet", "walletCurrency": "BTC"},
                                ]
                            }
                        }
                    }
                }
            if "mutation LnInvoiceCreate" in query:
                variables = (payload or {}).get("variables", {})
                wallet_id = (
                    variables.get("input", {}).get("walletId", "")
                    if isinstance(variables, dict)
                    else ""
                )
                expires_in = (
                    variables.get("input", {}).get("expiresIn")
                    if isinstance(variables, dict)
                    else None
                )
                self.assertEqual(wallet_id, "btc-auto-wallet")
                self.assertEqual(expires_in, 2)
                return {
                    "data": {
                        "lnInvoiceCreate": {
                            "errors": [],
                            "invoice": {
                                "paymentRequest": "lnbc1autowalletinvoice",
                                "paymentHash": "auto-wallet-hash",
                            },
                        }
                    }
                }
            self.fail(f"예상하지 못한 Blink 쿼리: {query}")

        with patch("core.services.dues_payment._http_json", side_effect=_mock_http_json):
            payment = select_member_due_payment_method(
                payer_member,
                MemberDuesPayment.METHOD_LIGHTNING,
            )

        address.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(address.blink_wallet_id, "btc-auto-wallet")
        self.assertEqual(payment.destination_blink_wallet_id, "btc-auto-wallet")
        self.assertEqual(payment.lightning_invoice, "lnbc1autowalletinvoice")

    def test_lightning_check_uses_payment_request_status_query(self):
        admin_user = User.objects.create_user(username="blink_status_admin", password="testpass123")
        admin_member = Member.objects.create(
            user=admin_user,
            display_name="상태확인관리자",
            lightning_identifier="blink_status_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(admin_member)
        MemberAddressHistory.objects.create(
            member=admin_member,
            bitcoin_onchain_address="tb1qstatus0000000000000000000000000000000",
            lightning_address="status-admin@blink.test",
            blink_api_key_encrypted=encrypt_blink_api_key("blink_status_api_key"),
            blink_wallet_id="status-wallet-id",
            is_current=True,
        )

        payer_user = User.objects.create_user(username="payer_status_wallet", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="상태확인회원",
            lightning_identifier="payer_status_wallet_id",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)

        payment = ensure_member_due_payment(payer_member)
        payment.requested_by_staff_at = timezone.now()
        payment.payment_method = MemberDuesPayment.METHOD_LIGHTNING
        payment.status = MemberDuesPayment.STATUS_WAITING_LIGHTNING
        payment.lightning_invoice = "lnbc1statusinvoice"
        payment.save(
            update_fields=[
                "requested_by_staff_at",
                "payment_method",
                "status",
                "lightning_invoice",
                "updated_at",
            ]
        )

        seen_queries: list[str] = []

        def _mock_http_json(_url, *, method="GET", headers=None, payload=None, timeout_sec=10):
            del method, headers, timeout_sec
            query = str((payload or {}).get("query", ""))
            seen_queries.append(query)
            if "lnInvoicePaymentStatusByPaymentRequest" in query:
                return {
                    "data": {
                        "lnInvoicePaymentStatusByPaymentRequest": {
                            "paymentHash": "status-hash",
                            "paymentRequest": "lnbc1statusinvoice",
                            "status": "PAID",
                        }
                    }
                }
            self.fail(f"예상하지 못한 Blink 쿼리: {query}")

        with patch("core.services.dues_payment._http_json", side_effect=_mock_http_json):
            checked = check_member_due_payment(payer_member)

        self.assertIsNotNone(checked)
        self.assertTrue(
            any("lnInvoicePaymentStatusByPaymentRequest" in query for query in seen_queries)
        )
        checked.refresh_from_db()
        payer_member.refresh_from_db()
        self.assertEqual(checked.status, MemberDuesPayment.STATUS_PAID)
        self.assertEqual(payer_member.status, Member.STATUS_ACTIVE)

    def test_lightning_selection_error_is_rendered_in_panel(self):
        payer_user = User.objects.create_user(username="payer_no_address", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="주소없는회원",
            lightning_identifier="payer_no_address_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self._mark_staff_due_requested(payer_member)
        self.client.force_login(payer_user)

        response = self.client.post(
            reverse("core:my-dues-payment-select-method"),
            {"payment_method": "lightning"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "설정/주소등록에 블링크 주소를 먼저 등록해 주세요.")

    def test_lightning_selection_requires_staff_due_request(self):
        payer_user = User.objects.create_user(username="payer_need_staff_request", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="요청전회원",
            lightning_identifier="payer_need_staff_request_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self.client.force_login(payer_user)

        response = self.client.post(
            reverse("core:my-dues-payment-select-method"),
            {"payment_method": "lightning"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "운영진의 회비지급요청 후에 결제를 진행할 수 있습니다.")

    def test_lightning_selection_maps_401_to_actionable_message(self):
        admin_user = User.objects.create_user(username="blink_admin", password="testpass123")
        admin_member = Member.objects.create(
            user=admin_user,
            display_name="블링크관리자",
            lightning_identifier="blink_admin_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(admin_member)
        MemberAddressHistory.objects.create(
            member=admin_member,
            bitcoin_onchain_address="tb1qblink000000000000000000000000000000000",
            lightning_address="blink-admin@blink.test",
            blink_api_key_encrypted=encrypt_blink_api_key("blink_api_key_value"),
            blink_wallet_id="blink-wallet-id",
            is_current=True,
        )

        payer_user = User.objects.create_user(username="payer_401", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="납부401회원",
            lightning_identifier="payer_401_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self._mark_staff_due_requested(payer_member)

        with patch(
            "core.services.dues_payment._http_json",
            side_effect=DuesPaymentServiceError("HTTP Error 401: Unauthorized"),
        ):
            with self.assertRaises(DuesPaymentServiceError) as exc_info:
                select_member_due_payment_method(
                    payer_member,
                    MemberDuesPayment.METHOD_LIGHTNING,
                )
        self.assertIn("블링크 인증 실패(401)", str(exc_info.exception))

    def test_switch_from_onchain_to_lightning_keeps_lightning_panel_on_error(self):
        admin_user = User.objects.create_user(username="blink_switch_admin", password="testpass123")
        admin_member = Member.objects.create(
            user=admin_user,
            display_name="전환관리자",
            lightning_identifier="blink_switch_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(admin_member)
        MemberAddressHistory.objects.create(
            member=admin_member,
            bitcoin_onchain_address="tb1qswitch000000000000000000000000000000000",
            lightning_address="switch-admin@blink.test",
            blink_api_key_encrypted=encrypt_blink_api_key("blink_switch_api_key"),
            blink_wallet_id="switch-wallet-id",
            is_current=True,
        )

        payer_user = User.objects.create_user(username="payer_switch", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="전환납부회원",
            lightning_identifier="payer_switch_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self._mark_staff_due_requested(payer_member)
        self.client.force_login(payer_user)

        onchain_response = self.client.post(
            reverse("core:my-dues-payment-select-method"),
            {"payment_method": "onchain"},
        )
        self.assertEqual(onchain_response.status_code, 200)
        self.assertContains(onchain_response, "온체인 결제")

        with patch(
            "core.services.dues_payment._http_json",
            side_effect=DuesPaymentServiceError("HTTP Error 401: Unauthorized"),
        ):
            lightning_response = self.client.post(
                reverse("core:my-dues-payment-select-method"),
                {"payment_method": "lightning"},
            )

        self.assertEqual(lightning_response.status_code, 200)
        self.assertContains(lightning_response, "라이트닝 결제")
        self.assertContains(lightning_response, "블링크 인증 실패(401)")
        self.assertNotContains(lightning_response, "온체인 결제")

    def test_lightning_panel_shows_2m_countdown_and_1s_polling(self):
        admin_user = User.objects.create_user(username="blink_countdown_admin", password="testpass123")
        admin_member = Member.objects.create(
            user=admin_user,
            display_name="카운트다운관리자",
            lightning_identifier="blink_countdown_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(admin_member)
        MemberAddressHistory.objects.create(
            member=admin_member,
            bitcoin_onchain_address="tb1qcountdown00000000000000000000000000000",
            lightning_address="countdown-admin@blink.test",
            blink_api_key_encrypted=encrypt_blink_api_key("blink_countdown_api_key"),
            blink_wallet_id="countdown-wallet-id",
            is_current=True,
        )

        payer_user = User.objects.create_user(username="payer_countdown", password="testpass123")
        payer_member = Member.objects.create(
            user=payer_user,
            display_name="카운트다운회원",
            lightning_identifier="payer_countdown_wallet",
            status=Member.STATUS_APPROVED,
        )
        ensure_member_domain_records(payer_member)
        self._mark_staff_due_requested(payer_member)
        self.client.force_login(payer_user)

        with patch(
            "core.services.dues_payment._issue_blink_invoice",
            return_value=("lnbc1countdowninvoice", "countdown-hash"),
        ), patch(
            "core.services.dues_payment._check_lightning_invoice_paid",
            return_value=False,
        ):
            response = self.client.post(
                reverse("core:my-dues-payment-select-method"),
                {"payment_method": "lightning"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "인보이스 만료까지")
        self.assertContains(response, "2분 기준")
        self.assertContains(response, 'hx-trigger="every 1s"')


class DuesPaymentReviewNoteFormatTests(TestCase):
    def test_lightning_review_note_uses_invoice(self):
        member_user = User.objects.create_user(username="fmt_light_user", password="testpass123")
        member = Member.objects.create(
            user=member_user,
            display_name="라이트닝회비회원",
            lightning_identifier="fmt_light_wallet",
            status=Member.STATUS_APPROVED,
        )
        payment = MemberDuesPayment.objects.create(
            member=member,
            request_month=date(2026, 2, 1),
            amount_sats=91_666,
            payment_method=MemberDuesPayment.METHOD_LIGHTNING,
            status=MemberDuesPayment.STATUS_PAID,
            lightning_invoice="lnbc1lightninginvoiceexample",
        )

        self.assertEqual(
            format_due_payment_review_note(payment),
            "라이트닝: lnbc1lightninginvoiceexample",
        )


class MessageShareDiscordTests(TestCase):
    def setUp(self):
        self.request_factory = RequestFactory()

    def _configure_discord_setting(self):
        setting = get_message_share_setting()
        setting.discord_channel_id = "123456789012345678"
        setting.send_member_pending_email = False
        setting.send_member_pending_discord = True
        setting.send_member_approved_email = False
        setting.send_member_approved_discord = True
        setting.send_member_dues_confirmed_email = False
        setting.send_member_dues_confirmed_discord = True
        setting.send_activity_created_email = False
        setting.send_activity_created_discord = True
        setting.save(
            update_fields=[
                "discord_channel_id",
                "send_member_pending_email",
                "send_member_pending_discord",
                "send_member_approved_email",
                "send_member_approved_discord",
                "send_member_dues_confirmed_email",
                "send_member_dues_confirmed_discord",
                "send_activity_created_email",
                "send_activity_created_discord",
            ]
        )

    @override_settings(MESSAGE_SHARE_DISCORD_BOT_TOKEN="discord_bot_token_for_test")
    def test_discord_sender_sets_user_agent_header(self):
        import core.services.message_share as message_share_module

        captured_request = {}

        class _DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=0):
            del timeout
            captured_request["req"] = req
            return _DummyResponse()

        with patch(
            "core.services.message_share.request.urlopen",
            side_effect=_fake_urlopen,
        ):
            message_share_module._send_discord_channel_message(
                "123456789012345678",
                "헤더 테스트 메시지",
            )

        req = captured_request.get("req")
        self.assertIsNotNone(req)
        headers = {key.lower(): value for key, value in req.header_items()}
        self.assertEqual(
            headers.get("user-agent"),
            message_share_module.DISCORD_HTTP_USER_AGENT,
        )

    def test_activity_discord_message_includes_quick_link(self):
        import core.services.message_share as message_share_module

        self._configure_discord_setting()
        user = User.objects.create_user(username="quicklink_activity", password="test1234")
        member = Member.objects.create(
            user=user,
            display_name="활동회원",
            lightning_identifier="quicklink_activity_wallet",
            status=Member.STATUS_ACTIVE,
        )
        activity = MemberActivityRequest.objects.create(
            member=member,
            title="활동 테스트",
            content_html="<p>활동</p>",
            amount_sats=12_000,
            lightning_address="member@blink.test",
            evidence_url="https://example.com/evidence",
        )
        request_obj = self.request_factory.get("/", HTTP_HOST="127.0.0.1:10001")
        quick_link = (
            "http://127.0.0.1:10001"
            + reverse("core:staff-activity-detail", args=[activity.id])
        )

        with patch("core.services.message_share._send_discord_channel_message") as sender:
            message_share_module.send_activity_share_messages(
                activity,
                event_type=message_share_module.EVENT_ACTIVITY_CREATED,
                request_obj=request_obj,
            )

        self.assertEqual(sender.call_count, 1)
        self.assertIn(f"바로가기: {quick_link}", sender.call_args.args[1])

    def test_pending_member_discord_message_includes_quick_link(self):
        import core.services.message_share as message_share_module

        self._configure_discord_setting()
        user = User.objects.create_user(username="quicklink_pending", password="test1234")
        member = Member.objects.create(
            user=user,
            display_name="승인대기회원",
            lightning_identifier="quicklink_pending_wallet",
            status=Member.STATUS_PENDING,
        )
        request_obj = self.request_factory.get("/", HTTP_HOST="127.0.0.1:10001")
        quick_link = "http://127.0.0.1:10001" + reverse("core:staff-members-pending")

        with patch("core.services.message_share._send_discord_channel_message") as sender:
            message_share_module.send_pending_member_share_messages(
                member,
                request_obj=request_obj,
            )

        self.assertEqual(sender.call_count, 1)
        self.assertIn(f"바로가기: {quick_link}", sender.call_args.args[1])

    def test_member_approved_discord_message_includes_status_change(self):
        import core.services.message_share as message_share_module

        self._configure_discord_setting()
        user = User.objects.create_user(username="quicklink_approved", password="test1234")
        member = Member.objects.create(
            user=user,
            display_name="승인완료회원",
            lightning_identifier="quicklink_approved_wallet",
            status=Member.STATUS_APPROVED,
        )
        request_obj = self.request_factory.get("/", HTTP_HOST="127.0.0.1:10001")

        with patch("core.services.message_share._send_discord_channel_message") as sender:
            message_share_module.send_member_status_share_messages(
                member,
                event_type=message_share_module.EVENT_MEMBER_APPROVED,
                request_obj=request_obj,
                actor_name="운영자A",
                review_note="가입 승인",
            )

        self.assertEqual(sender.call_count, 1)
        message_text = sender.call_args.args[1]
        self.assertIn("상태 변경: 승인 대기 -> 승인(회비확인 대기)", message_text)
        self.assertIn("처리자: 운영자A", message_text)
        self.assertIn("처리 사유: 가입 승인", message_text)

    def test_member_dues_confirmed_discord_message_includes_dues_amount(self):
        import core.services.message_share as message_share_module

        self._configure_discord_setting()
        user = User.objects.create_user(username="quicklink_dues", password="test1234")
        member = Member.objects.create(
            user=user,
            display_name="회비완료회원",
            lightning_identifier="quicklink_dues_wallet",
            status=Member.STATUS_ACTIVE,
        )
        request_obj = self.request_factory.get("/", HTTP_HOST="127.0.0.1:10001")

        with patch("core.services.message_share._send_discord_channel_message") as sender:
            message_share_module.send_member_status_share_messages(
                member,
                event_type=message_share_module.EVENT_MEMBER_DUES_CONFIRMED,
                request_obj=request_obj,
                actor_name="운영자B",
                review_note="회비 입금 확인",
                dues_amount_sats=91666,
                payment_note="온체인: txid_sample",
            )

        self.assertEqual(sender.call_count, 1)
        message_text = sender.call_args.args[1]
        self.assertIn("상태 변경: 승인(회비확인 대기) -> 활성", message_text)
        self.assertIn("회비 금액: 91,666 sats", message_text)
        self.assertIn("결제 정보: 온체인: txid_sample", message_text)


class BasicDuesCalculationTests(TestCase):
    def test_monthly_dues_plan_uses_running_deduction_from_previous_month(self):
        plan = build_monthly_dues_plan(annual_dues_sats=100_000, year=2026)
        self.assertEqual(plan[0]["applied_fee_sats"], 100_000)
        self.assertEqual(plan[1]["applied_fee_sats"], 91_666)
        self.assertEqual(plan[2]["applied_fee_sats"], 83_332)
        self.assertEqual(plan[1]["base_fee_sats"], 100_000)
        self.assertEqual(plan[2]["base_fee_sats"], 91_666)

    def test_resolve_monthly_dues_amount_follows_running_plan(self):
        BasicDuesSetting.objects.create(annual_dues_sats=100_000, configured_year=2026)
        self.assertEqual(resolve_monthly_dues_amount(date(2026, 1, 1)), 100_000)
        self.assertEqual(resolve_monthly_dues_amount(date(2026, 2, 1)), 91_666)
        self.assertEqual(resolve_monthly_dues_amount(date(2026, 3, 1)), 83_332)

    def test_resolve_monthly_dues_amount_applies_next_dues_after_end_year(self):
        BasicDuesSetting.objects.create(
            annual_dues_sats=120_000,
            configured_year=2026,
            end_year=2026,
            next_annual_dues_sats=240_000,
        )
        self.assertEqual(resolve_monthly_dues_amount(date(2026, 1, 1)), 120_000)
        self.assertEqual(resolve_monthly_dues_amount(date(2027, 1, 1)), 240_000)
        setting = get_basic_dues_setting()
        self.assertEqual(setting.annual_dues_sats, 240_000)
        self.assertEqual(setting.configured_year, 2027)
        self.assertIsNone(setting.end_year)
        self.assertIsNone(setting.next_annual_dues_sats)


class BasicDuesSettingViewTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="basic_admin", password="testpass123")
        self.member = Member.objects.create(
            user=user,
            display_name="회비관리자",
            lightning_identifier="basic_admin_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(self.member)
        self.client.force_login(user)

    def test_settings_basic_requires_next_dues_when_end_year_is_set(self):
        response = self.client.post(
            reverse("core:settings-basic"),
            {
                "annual_dues_sats": 100000,
                "configured_year": 2026,
                "end_year": "2026",
                "next_annual_dues_sats": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "종료연도를 설정하면 변경될 회비를 입력해야 합니다.")

    def test_settings_basic_shows_transition_message(self):
        response = self.client.post(
            reverse("core:settings-basic"),
            {
                "annual_dues_sats": 100000,
                "configured_year": 2026,
                "end_year": "2026",
                "next_annual_dues_sats": 150000,
            },
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(reverse("core:settings-basic"))
        self.assertContains(page, "100,000 sats는 2026년까지 유지")
        self.assertContains(page, "2027년부터 연 150,000 sats로 변경")


class MemberActivityManageTests(TestCase):
    def setUp(self):
        self.owner_user = User.objects.create_user(
            username="activity_owner",
            password="testpass123",
        )
        self.owner_member = Member.objects.create(
            user=self.owner_user,
            display_name="활동등록자",
            lightning_identifier="activity_owner_wallet",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(self.owner_member)

        self.other_user = User.objects.create_user(
            username="activity_other",
            password="testpass123",
        )
        self.other_member = Member.objects.create(
            user=self.other_user,
            display_name="다른회원",
            lightning_identifier="activity_other_wallet",
            status=Member.STATUS_ACTIVE,
        )
        ensure_member_domain_records(self.other_member)

        self.admin_user = User.objects.create_user(
            username="activity_admin",
            password="testpass123",
        )
        self.admin_member = Member.objects.create(
            user=self.admin_user,
            display_name="활동어드민",
            lightning_identifier="activity_admin_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(self.admin_member)

        self.activity = MemberActivityRequest.objects.create(
            member=self.owner_member,
            title="기존 제목",
            content_html="<p>기존 내용</p>",
            amount_sats=10_000,
            lightning_address="owner@blink.test",
            evidence_url="https://discord.com/channels/1/2/3",
        )

    def test_owner_can_edit_and_delete_activity(self):
        self.client.force_login(self.owner_user)

        edit_response = self.client.post(
            reverse("core:staff-activity-edit", args=[self.activity.id]),
            {
                "title": "수정 제목",
                "content_html": "<p>수정 내용</p>",
                "amount_sats": 12_345,
                "lightning_address": "updated@blink.test",
                "evidence_url": "https://discord.com/channels/1/2/4",
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(
            edit_response["Location"],
            reverse("core:staff-activity-detail", args=[self.activity.id]),
        )

        self.activity.refresh_from_db()
        self.assertEqual(self.activity.title, "수정 제목")
        self.assertEqual(self.activity.amount_sats, 12_345)
        self.assertEqual(self.activity.lightning_address, "updated@blink.test")
        self.assertIn("수정 내용", self.activity.content_html)

        delete_response = self.client.post(
            reverse("core:staff-activity-delete", args=[self.activity.id]),
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(
            delete_response["Location"],
            reverse("core:staff-activities"),
        )
        self.assertFalse(
            MemberActivityRequest.objects.filter(id=self.activity.id).exists()
        )

    def test_non_owner_cannot_edit_or_delete_activity(self):
        self.client.force_login(self.other_user)

        edit_response = self.client.post(
            reverse("core:staff-activity-edit", args=[self.activity.id]),
            {
                "title": "권한없는 수정",
                "content_html": "<p>권한없는 수정</p>",
                "amount_sats": 99_999,
                "lightning_address": "unauthorized@blink.test",
                "evidence_url": "https://discord.com/channels/9/9/9",
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(edit_response["Location"], reverse("core:staff-activities"))

        self.activity.refresh_from_db()
        self.assertEqual(self.activity.title, "기존 제목")

        delete_response = self.client.post(
            reverse("core:staff-activity-delete", args=[self.activity.id]),
        )
        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(delete_response["Location"], reverse("core:staff-activities"))
        self.assertTrue(
            MemberActivityRequest.objects.filter(id=self.activity.id).exists()
        )

    def test_owner_can_request_settlement_and_admin_can_complete(self):
        self.client.force_login(self.owner_user)

        request_form_response = self.client.get(
            reverse("core:staff-activity-request-settlement", args=[self.activity.id]),
        )
        self.assertEqual(request_form_response.status_code, 200)
        self.assertContains(request_form_response, "활동 정산요청")

        request_response = self.client.post(
            reverse("core:staff-activity-request-settlement", args=[self.activity.id]),
            {
                "result_content_html": "<p>정산 결과 요약</p>",
            },
        )
        self.assertEqual(request_response.status_code, 302)
        self.assertEqual(
            request_response["Location"],
            reverse("core:staff-settlements"),
        )
        self.activity.refresh_from_db()
        self.assertEqual(
            self.activity.status,
            MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED,
        )
        self.assertIn("정산 결과 요약", self.activity.settlement_result_html)
        self.assertIsNotNone(self.activity.settlement_requested_at)

        activities_response = self.client.get(reverse("core:staff-activities"))
        self.assertEqual(activities_response.status_code, 200)
        self.assertNotContains(activities_response, self.activity.title)

        settlements_response = self.client.get(reverse("core:staff-settlements"))
        self.assertEqual(settlements_response.status_code, 200)
        self.assertContains(settlements_response, self.activity.title)

        self.client.force_login(self.admin_user)
        settlement_process_response = self.client.get(
            reverse("core:staff-settlement-process"),
        )
        self.assertEqual(settlement_process_response.status_code, 200)
        self.assertContains(settlement_process_response, self.activity.title)

        with patch(
            "core.views.staff.send_lightning_payment_to_address",
            return_value=SimpleNamespace(
                status="SUCCESS",
                reference="activity-settlement-tx-001",
                note="상태: SUCCESS, 거래참조: activity-settlement-tx-001",
            ),
        ):
            complete_response = self.client.post(
                reverse(
                    "core:staff-settlement-process-execute",
                    args=[self.activity.id],
                ),
            )
        self.assertEqual(complete_response.status_code, 302)
        self.assertEqual(
            complete_response["Location"],
            reverse("core:staff-settlements"),
        )
        self.activity.refresh_from_db()
        self.assertEqual(
            self.activity.status,
            MemberActivityRequest.STATUS_SETTLEMENT_COMPLETED,
        )
        self.assertIsNotNone(self.activity.settlement_completed_at)
        self.assertEqual(self.activity.settlement_transfer_status, "SUCCESS")
        self.assertEqual(
            self.activity.settlement_transfer_reference,
            "activity-settlement-tx-001",
        )
        self.assertIsNotNone(self.activity.settlement_processed_at)
        self.assertEqual(self.activity.settlement_processed_by_id, self.admin_user.id)

        settlement_detail_response = self.client.get(
            reverse("core:staff-settlement-detail", args=[self.activity.id])
        )
        self.assertEqual(settlement_detail_response.status_code, 200)
        self.assertContains(settlement_detail_response, "정산 상세")
        self.assertContains(settlement_detail_response, 'href="/settlements/"')

    def test_settlement_process_execute_returns_json_logs_for_async_request(self):
        self.activity.status = MemberActivityRequest.STATUS_SETTLEMENT_REQUESTED
        self.activity.settlement_requested_at = timezone.now()
        self.activity.save(update_fields=["status", "settlement_requested_at", "updated_at"])

        self.client.force_login(self.admin_user)
        with patch(
            "core.views.staff.send_lightning_payment_to_address",
            return_value=SimpleNamespace(
                status="SUCCESS",
                reference="async-log-tx-01",
                note="상태: SUCCESS, 거래참조: async-log-tx-01",
            ),
        ):
            response = self.client.post(
                reverse("core:staff-settlement-process-execute", args=[self.activity.id]),
                HTTP_ACCEPT="application/json",
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("redirect_url"), reverse("core:staff-settlements"))
        self.assertIn("logs", payload)
        joined_logs = " ".join(payload.get("logs", []))
        self.assertIn("블링크 라이트닝 자동 전송", joined_logs)


class SidebarMenuVisibilityTests(TestCase):
    def test_non_admin_active_member_cannot_see_management_or_settings_sidebar(self):
        user = User.objects.create_user(username="sidebar_active_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="사이드바회원",
            lightning_identifier="sidebar_active_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=False,
        )
        ensure_member_domain_records(member)
        self.client.force_login(user)

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<p class="menu-label">관리</p>', html=True)
        self.assertNotContains(response, '<p class="menu-label">설정</p>', html=True)
        self.assertNotContains(response, 'href="/members/dues-request/"')
        self.assertNotContains(response, 'href="/settings/address/"')
        self.assertNotContains(response, 'href="/settings/basic/"')
        self.assertNotContains(response, 'href="/settlements/process/"')

    def test_backoffice_admin_can_see_management_and_settings_sidebar(self):
        user = User.objects.create_user(username="sidebar_admin_user", password="testpass123")
        member = Member.objects.create(
            user=user,
            display_name="사이드바관리자",
            lightning_identifier="sidebar_admin_wallet",
            status=Member.STATUS_ACTIVE,
            is_backoffice_admin=True,
        )
        ensure_member_domain_records(member)
        self.client.force_login(user)

        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<p class="menu-label">관리</p>', html=True)
        self.assertContains(response, '<p class="menu-label">설정</p>', html=True)
        self.assertContains(response, 'href="/members/dues-request/"')
        self.assertContains(response, 'href="/settings/address/"')
        self.assertContains(response, 'href="/settings/basic/"')
        self.assertContains(response, 'href="/settlements/process/"')
