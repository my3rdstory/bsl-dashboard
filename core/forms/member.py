from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from core.models import Member, ensure_member_domain_records, open_member_year_membership
from .nickname import validate_member_nickname


class MemberRegistrationForm(UserCreationForm):
    display_name = forms.CharField(max_length=80, label="표시 닉네임")
    phone = forms.CharField(max_length=32, required=False, label="연락처")
    lightning_identifier = forms.CharField(max_length=120, label="라이트닝 인증 ID")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "display_name",
            "phone",
            "lightning_identifier",
            "password1",
            "password2",
        )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input is-small"

    def clean_display_name(self):
        return validate_member_nickname(self.cleaned_data.get("display_name", ""))

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=commit)
        if not commit:
            return user

        member = Member(
            user=user,
            display_name=self.cleaned_data["display_name"],
            phone=self.cleaned_data.get("phone", ""),
            lightning_identifier=self.cleaned_data["lightning_identifier"],
            status=Member.STATUS_PENDING,
        )
        member.save()
        ensure_member_domain_records(member)
        open_member_year_membership(member, reference_at=member.requested_at)
        return user


class MemberNicknameUpdateForm(forms.Form):
    display_name = forms.CharField(max_length=80, label="닉네임")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["display_name"].widget.attrs.update(
            {
                "class": "input is-small",
                "maxlength": "80",
                "placeholder": "닉네임 입력",
            }
        )

    def clean_display_name(self):
        return validate_member_nickname(self.cleaned_data.get("display_name", ""))


class MemberAddressForm(forms.Form):
    bitcoin_onchain_address = forms.CharField(
        max_length=100,
        required=True,
        label="비트코인 온체인 주소",
    )
    blink_lightning_address = forms.CharField(
        max_length=100,
        required=True,
        label="블링크 주소",
    )
    blink_api_key = forms.CharField(
        max_length=255,
        required=False,
        label="블링크 API Key",
        widget=forms.PasswordInput(render_value=False),
    )
    blink_wallet_id = forms.CharField(
        max_length=120,
        required=False,
        label="블링크 Wallet ID",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "input is-small"
        self.fields["blink_wallet_id"].widget.attrs["placeholder"] = (
            "미입력 시 API Key 기준 BTC 지갑 자동 선택"
        )


class BasicDuesSettingForm(forms.Form):
    annual_dues_sats = forms.IntegerField(
        min_value=1,
        max_value=99_999_999,
        required=True,
        label="회비설정 (sats)",
    )
    configured_year = forms.IntegerField(
        min_value=2000,
        max_value=9999,
        required=True,
        label="연도 설정",
    )
    end_year = forms.ChoiceField(
        required=False,
        label="종료연도",
    )
    next_annual_dues_sats = forms.IntegerField(
        min_value=1,
        max_value=99_999_999,
        required=False,
        label="종료 후 변경 회비 (sats)",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        current_year = int(timezone.localdate().year)
        raw_year = self.initial.get("configured_year")
        if isinstance(raw_year, int):
            current_year = raw_year
        self.fields["end_year"].choices = [("", "제한없음")] + [
            (str(year), str(year)) for year in range(current_year, current_year + 31)
        ]

        for name, field in self.fields.items():
            if name == "end_year":
                field.widget.attrs["class"] = "is-small"
                field.widget.attrs["data-end-year-select"] = "1"
                continue
            if name == "next_annual_dues_sats":
                field.widget.attrs.update(
                    {
                        "class": "input is-small number-spin-input",
                        "step": "1",
                        "inputmode": "numeric",
                        "pattern": "[0-9]*",
                    }
                )
                if field.min_value is not None:
                    field.widget.attrs["min"] = str(field.min_value)
                field.widget.attrs["data-next-dues-input"] = "1"
                continue
            if isinstance(field, forms.IntegerField):
                field.widget.attrs.update(
                    {
                        "class": "input is-small number-spin-input",
                        "step": "1",
                        "inputmode": "numeric",
                        "pattern": "[0-9]*",
                    }
                )
                if field.min_value is not None:
                    field.widget.attrs["min"] = str(field.min_value)
            else:
                field.widget.attrs["class"] = "input is-small"

    def clean_end_year(self):
        raw_value = self.cleaned_data.get("end_year", "")
        if raw_value in ("", None):
            return None
        return int(raw_value)

    def clean(self):
        cleaned_data = super().clean()
        configured_year = cleaned_data.get("configured_year")
        end_year = cleaned_data.get("end_year")
        next_annual_dues_sats = cleaned_data.get("next_annual_dues_sats")
        if (
            configured_year is not None
            and end_year is not None
            and int(end_year) < int(configured_year)
        ):
            self.add_error(
                "end_year",
                "종료연도는 연도 설정 이상이어야 합니다.",
            )
        if end_year is not None and next_annual_dues_sats is None:
            self.add_error(
                "next_annual_dues_sats",
                "종료연도를 설정하면 변경될 회비를 입력해야 합니다.",
            )
        if end_year is None and next_annual_dues_sats is not None:
            self.add_error(
                "next_annual_dues_sats",
                "변경될 회비를 입력하려면 종료연도를 먼저 설정해 주세요.",
            )
        return cleaned_data


class MessageShareSettingForm(forms.Form):
    admin_email = forms.CharField(
        required=False,
        max_length=2000,
        label="어드민 이메일",
    )
    gmail_app_password = forms.CharField(
        required=False,
        max_length=255,
        label="지메일 앱 비밀번호",
        widget=forms.PasswordInput(render_value=False),
    )
    gmail_sender_email = forms.EmailField(
        required=False,
        label="발송 이메일 주소",
    )
    gmail_sender_name = forms.CharField(
        required=False,
        max_length=120,
        label="발송자 이름",
    )
    discord_channel_id = forms.CharField(
        required=False,
        max_length=1000,
        label="디스코드 채널 ID",
    )
    send_member_pending_email = forms.BooleanField(required=False, label="승인대기 회원 발생")
    send_member_pending_discord = forms.BooleanField(required=False, label="승인대기 회원 발생")
    send_member_approved_email = forms.BooleanField(required=False, label="승인 처리")
    send_member_approved_discord = forms.BooleanField(required=False, label="승인 처리")
    send_member_dues_confirmed_email = forms.BooleanField(required=False, label="회비입금 확인 및 회원등록")
    send_member_dues_confirmed_discord = forms.BooleanField(required=False, label="회비입금 확인 및 회원등록")
    send_activity_created_email = forms.BooleanField(required=False, label="활동 등록")
    send_activity_created_discord = forms.BooleanField(required=False, label="활동 등록")
    send_settlement_requested_email = forms.BooleanField(required=False, label="정산요청")
    send_settlement_requested_discord = forms.BooleanField(required=False, label="정산요청")
    send_settlement_completed_email = forms.BooleanField(required=False, label="정산완료")
    send_settlement_completed_discord = forms.BooleanField(required=False, label="정산완료")
    recruiting_message_html = forms.CharField(
        required=False,
        label="공유 메시지",
        widget=forms.HiddenInput(),
    )

    def __init__(self, *args, **kwargs) -> None:
        self.existing_has_gmail_password = bool(
            kwargs.pop("existing_has_gmail_password", False)
        )
        super().__init__(*args, **kwargs)
        self.fields["admin_email"].widget.attrs.update(
            {
                "class": "input is-small",
                "placeholder": "example1@domain.com, example2@domain.com",
            }
        )
        self.fields["gmail_app_password"].widget.attrs.update(
            {
                "class": "input is-small",
                "placeholder": "Google 앱 비밀번호",
                "autocomplete": "new-password",
            }
        )
        self.fields["gmail_sender_email"].widget.attrs.update(
            {
                "class": "input is-small",
                "placeholder": "sender@gmail.com",
            }
        )
        self.fields["gmail_sender_name"].widget.attrs.update(
            {
                "class": "input is-small",
                "placeholder": "BSL 운영팀",
            }
        )
        self.fields["discord_channel_id"].widget.attrs.update(
            {
                "class": "input is-small",
                "placeholder": "1234567890, 9876543210",
            }
        )

    @staticmethod
    def _split_csv_values(raw_value: str) -> list[str]:
        return [item.strip() for item in str(raw_value).split(",") if item.strip()]

    def clean_admin_email(self) -> str:
        raw = str(self.cleaned_data.get("admin_email", "")).strip()
        if not raw:
            return ""
        emails = self._split_csv_values(raw)
        invalid_values: list[str] = []
        for email in emails:
            try:
                validate_email(email)
            except ValidationError:
                invalid_values.append(email)
        if invalid_values:
            raise forms.ValidationError(
                "유효하지 않은 이메일 형식이 있습니다: " + ", ".join(invalid_values)
            )
        return ", ".join(emails)

    def clean_gmail_app_password(self) -> str:
        raw = str(self.cleaned_data.get("gmail_app_password", "")).strip()
        return raw.replace(" ", "")

    def clean_discord_channel_id(self) -> str:
        raw = str(self.cleaned_data.get("discord_channel_id", "")).strip()
        if not raw:
            return ""
        channel_ids = self._split_csv_values(raw)
        invalid_values = [value for value in channel_ids if not value.isdigit()]
        if invalid_values:
            raise forms.ValidationError(
                "디스코드 채널 ID는 숫자만 입력해 주세요: "
                + ", ".join(invalid_values)
            )
        return ", ".join(channel_ids)

    def clean(self):
        cleaned_data = super().clean()
        admin_emails = self._split_csv_values(cleaned_data.get("admin_email", ""))
        wants_email_delivery = any(
            bool(cleaned_data.get(field_name))
            for field_name in [
                "send_member_pending_email",
                "send_member_approved_email",
                "send_member_dues_confirmed_email",
                "send_activity_created_email",
                "send_settlement_requested_email",
                "send_settlement_completed_email",
            ]
        )
        if wants_email_delivery and not admin_emails:
            self.add_error("admin_email", "이메일 전송을 사용하려면 수신 어드민 이메일을 입력해 주세요.")

        if wants_email_delivery and admin_emails:
            sender_email = str(cleaned_data.get("gmail_sender_email", "")).strip()
            sender_name = str(cleaned_data.get("gmail_sender_name", "")).strip()
            app_password = str(cleaned_data.get("gmail_app_password", "")).strip()
            if not sender_email:
                self.add_error("gmail_sender_email", "발송 이메일 주소를 입력해 주세요.")
            if not sender_name:
                self.add_error("gmail_sender_name", "발송자 이름을 입력해 주세요.")
            if not app_password and not self.existing_has_gmail_password:
                self.add_error("gmail_app_password", "지메일 앱 비밀번호를 입력해 주세요.")
        return cleaned_data
