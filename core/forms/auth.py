from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .nickname import validate_member_nickname


class LightningNicknameForm(forms.Form):
    nickname = forms.CharField(
        max_length=80,
        label="BSL 회원 닉네임",
        widget=forms.TextInput(attrs={"maxlength": 80}),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["nickname"].widget.attrs["class"] = "input is-small nickname-input"

    def clean_nickname(self):
        return validate_member_nickname(self.cleaned_data.get("nickname", ""))


class LightningApplicationForm(forms.Form):
    application_note = forms.CharField(
        max_length=2000,
        label="자기소개 및 검증용 소셜미디어 주소",
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "maxlength": 2000,
                "placeholder": (
                    "예시)\n"
                    "- 자기소개: ...\n"
                    "- 소셜미디어 주소: https://..."
                ),
            }
        ),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["application_note"].widget.attrs["class"] = (
            "textarea is-small application-textarea"
        )


class SuperAdminLoginForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "아이디 또는 비밀번호가 올바르지 않습니다.",
        "inactive": "비활성화된 계정입니다.",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {
                "class": "input is-small",
                "autocomplete": "username",
                "placeholder": "수퍼어드민 아이디",
            }
        )
        self.fields["password"].widget.attrs.update(
            {
                "class": "input is-small",
                "autocomplete": "current-password",
                "placeholder": "비밀번호",
            }
        )

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_superuser:
            raise ValidationError(
                "수퍼어드민 계정만 로그인할 수 있습니다.",
                code="superadmin_required",
            )
