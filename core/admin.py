from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from .models import (
    BasicDuesSetting,
    Dues,
    Member,
    MemberActivity,
    MemberActivityAttachment,
    MemberActivityRequest,
    MemberAuthProfile,
    MemberLifecycle,
    MemberReviewLog,
    MemberYearMembership,
    MemberSettlement,
    MemberWithdrawalHistory,
    MemberDuesPayment,
    MessageShareSetting,
)

User = get_user_model()

admin.site.site_header = "BSL 백오피스 관리자"
admin.site.site_title = "BSL 관리자"
admin.site.index_title = "운영 관리"


class DarkAdminMixin:
    class Media:
        css = {"all": ("css/admin-dark.css",)}


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(DarkAdminMixin, DjangoUserAdmin):
    list_display = ("member_nickname",) + DjangoUserAdmin.list_display + (
        "backoffice_admin_toggle",
    )
    search_fields = DjangoUserAdmin.search_fields + ("member__display_name",)
    readonly_fields = DjangoUserAdmin.readonly_fields + ("member_nickname",)
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("회원 연동 정보", {"fields": ("member_nickname",)}),
    )
    list_select_related = ("member",)

    def get_urls(self):
        model_name = f"{self.model._meta.app_label}_{self.model._meta.model_name}"
        custom_urls = [
            path(
                "<int:user_id>/toggle-backoffice-admin/",
                self.admin_site.admin_view(self.toggle_backoffice_admin_view),
                name=f"{model_name}_toggle_backoffice_admin",
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(self, request: HttpRequest, extra_context=None):
        self._list_request = request
        return super().changelist_view(request, extra_context=extra_context)

    def toggle_backoffice_admin_view(
        self, request: HttpRequest, user_id: int
    ) -> HttpResponseRedirect:
        if not request.user.is_superuser:
            raise PermissionDenied("수퍼어드민만 백오피스 권한을 변경할 수 있습니다.")

        target_user = self.get_object(request, str(user_id))
        changelist_url = reverse(
            f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_changelist"
        )
        if target_user is None:
            self.message_user(
                request,
                "대상 사용자를 찾을 수 없습니다.",
                level=messages.ERROR,
            )
            return HttpResponseRedirect(changelist_url)

        member = getattr(target_user, "member", None)
        if member is None:
            self.message_user(
                request,
                "회원 연동 정보가 없어 백오피스 권한을 변경할 수 없습니다.",
                level=messages.WARNING,
            )
            return HttpResponseRedirect(changelist_url)

        member.is_backoffice_admin = not member.is_backoffice_admin
        member.save(update_fields=["is_backoffice_admin"])
        changed_text = "부여" if member.is_backoffice_admin else "해제"
        self.message_user(
            request,
            f"{member.display_name} 백오피스 어드민 권한을 {changed_text}했습니다.",
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect(request.META.get("HTTP_REFERER") or changelist_url)

    @admin.display(description="닉네임")
    def member_nickname(self, obj: User):
        member = getattr(obj, "member", None)
        if member is None:
            return "-"
        return member.display_name or "-"

    @admin.display(description="백오피스 관리")
    def backoffice_admin_toggle(self, obj: User):
        member = getattr(obj, "member", None)
        if member is None:
            return "-"

        is_enabled = bool(member.is_backoffice_admin)
        request = getattr(self, "_list_request", None)
        if not isinstance(request, HttpRequest) or not request.user.is_superuser:
            label = "ON" if is_enabled else "OFF"
            state_class = "is-on" if is_enabled else "is-off"
            return format_html(
                '<span class="bo-admin-state {}">{}</span>',
                state_class,
                label,
            )

        toggle_url = reverse(
            f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_toggle_backoffice_admin",
            args=[obj.pk],
        )
        label = "ON" if is_enabled else "OFF"
        state_class = "is-on" if is_enabled else "is-off"
        return format_html(
            '<a class="bo-admin-toggle-link {}" href="{}">{}</a>',
            state_class,
            toggle_url,
            label,
        )


@admin.register(Member)
class MemberAdmin(DarkAdminMixin, admin.ModelAdmin):
    status_map = dict(Member.STATUS_CHOICES)
    list_display = (
        "display_name_col",
        "lightning_identifier_col",
        "status_col",
        "requested_at_col",
        "paid_until_month_col",
    )
    list_filter = ("status",)
    search_fields = ("display_name", "lightning_identifier", "user__username")

    @admin.display(description="닉네임", ordering="display_name")
    def display_name_col(self, obj: Member):
        return obj.display_name

    @admin.display(description="라이트닝 ID", ordering="lightning_identifier")
    def lightning_identifier_col(self, obj: Member):
        return obj.lightning_identifier

    @admin.display(description="상태", ordering="status")
    def status_col(self, obj: Member):
        status_value = str(obj.status)
        return self.status_map.get(status_value, status_value)

    @admin.display(description="신청일", ordering="requested_at")
    def requested_at_col(self, obj: Member):
        return obj.requested_at

    @admin.display(description="회비 확인 월", ordering="paid_until_month")
    def paid_until_month_col(self, obj: Member):
        return obj.paid_until_month or "-"


@admin.register(Dues)
class DuesAdmin(DarkAdminMixin, admin.ModelAdmin):
    status_map = dict(Dues.STATUS_CHOICES)
    list_display = (
        "month_col",
        "member_col",
        "amount_col",
        "status_col",
        "payment_reference_col",
        "approved_at_col",
    )
    list_filter = ("status", "month")
    search_fields = ("member__display_name", "payment_reference")

    @admin.display(description="월", ordering="month")
    def month_col(self, obj: Dues):
        return obj.month

    @admin.display(description="회원", ordering="member__display_name")
    def member_col(self, obj: Dues):
        return str(obj.member)

    @admin.display(description="금액", ordering="amount")
    def amount_col(self, obj: Dues):
        return obj.amount

    @admin.display(description="상태", ordering="status")
    def status_col(self, obj: Dues):
        status_value = str(obj.status)
        return self.status_map.get(status_value, status_value)

    @admin.display(description="납부 식별자", ordering="payment_reference")
    def payment_reference_col(self, obj: Dues):
        return obj.payment_reference or "-"

    @admin.display(description="승인 시각", ordering="approved_at")
    def approved_at_col(self, obj: Dues):
        return obj.approved_at or "-"


@admin.register(MemberAuthProfile)
class MemberAuthProfileAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = (
        "member",
        "internal_temp_id",
        "internal_temp_password",
        "updated_at",
    )
    search_fields = (
        "member__display_name",
        "internal_temp_id",
        "internal_temp_password",
    )


@admin.register(MemberLifecycle)
class MemberLifecycleAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = ("member", "signed_up_at", "withdrawn_at", "updated_at")
    search_fields = ("member__display_name",)


@admin.register(MemberSettlement)
class MemberSettlementAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = ("member", "dues_paid_at", "updated_at")
    search_fields = ("member__display_name",)


@admin.register(MemberActivity)
class MemberActivityAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = ("member", "claimed_at", "processed_at", "updated_at")
    search_fields = ("member__display_name",)


@admin.register(MemberActivityRequest)
class MemberActivityRequestAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = ("member", "title", "amount_sats", "lightning_address", "created_at")
    search_fields = ("member__display_name", "title", "lightning_address", "evidence_url")


@admin.register(MemberActivityAttachment)
class MemberActivityAttachmentAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = (
        "activity_request",
        "original_filename",
        "file_size_bytes",
        "content_type",
        "created_at",
    )
    search_fields = ("activity_request__title", "object_key", "original_filename")


@admin.register(MemberYearMembership)
class MemberYearMembershipAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = ("member", "year", "joined_at", "ended_at", "updated_at")
    list_filter = ("year",)
    search_fields = ("member__display_name", "member__lightning_identifier")


@admin.register(MemberReviewLog)
class MemberReviewLogAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = ("member", "action", "acted_by", "acted_at")
    list_filter = ("action",)
    search_fields = ("member__display_name", "member__lightning_identifier", "note")


@admin.register(MemberWithdrawalHistory)
class MemberWithdrawalHistoryAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = (
        "former_display_name",
        "former_lightning_identifier",
        "withdrawn_by",
        "withdrawn_at",
    )
    list_filter = ("withdrawn_at",)
    search_fields = (
        "former_display_name",
        "former_lightning_identifier",
        "former_user_username",
        "withdraw_note",
    )


@admin.register(MemberDuesPayment)
class MemberDuesPaymentAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = (
        "member",
        "request_month",
        "amount_sats",
        "payment_method",
        "status",
        "paid_at",
        "last_checked_at",
    )
    list_filter = ("status", "payment_method", "request_month")
    search_fields = (
        "member__display_name",
        "member__lightning_identifier",
        "onchain_txid",
        "lightning_invoice_hash",
    )


@admin.register(BasicDuesSetting)
class BasicDuesSettingAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = (
        "annual_dues_sats",
        "configured_year",
        "end_year",
        "next_annual_dues_sats",
        "updated_at",
    )


@admin.register(MessageShareSetting)
class MessageShareSettingAdmin(DarkAdminMixin, admin.ModelAdmin):
    list_display = (
        "admin_email",
        "discord_channel_id",
        "updated_at",
    )
