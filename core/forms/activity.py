from django import forms
from django.utils.html import escape, strip_tags


class MemberActivityRequestForm(forms.Form):
    MAX_CONTENT_CHARS = 10_000

    title = forms.CharField(
        max_length=300,
        required=True,
        label="제목",
    )
    content_html = forms.CharField(
        required=True,
        label="내용",
        widget=forms.HiddenInput(),
    )
    amount_sats = forms.IntegerField(
        min_value=1,
        max_value=99_999_999,
        required=True,
        label="활동비 (sats)",
    )
    lightning_address = forms.CharField(
        max_length=100,
        required=True,
        label="라이트닝 주소",
    )
    evidence_url = forms.URLField(
        max_length=100,
        required=True,
        label="근거 링크",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs["class"] = "input is-small"
        self.fields["amount_sats"].widget.attrs.update(
            {
                "class": "input is-small number-spin-input",
                "min": str(self.fields["amount_sats"].min_value or 1),
                "step": "1",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
            }
        )
        self.fields["lightning_address"].widget.attrs["class"] = "input is-small"
        self.fields["evidence_url"].widget.attrs["class"] = "input is-small"
        self.fields["evidence_url"].widget.attrs["placeholder"] = (
            "https://discord.com/channels/..."
        )

    def clean_content_html(self) -> str:
        raw_html = str(self.cleaned_data.get("content_html", "")).strip()
        text = strip_tags(raw_html).strip()
        if not text:
            raise forms.ValidationError("내용을 입력해 주세요.")
        if len(text) > self.MAX_CONTENT_CHARS:
            truncated_text = text[: self.MAX_CONTENT_CHARS]
            return f"<p>{escape(truncated_text)}</p>"
        return raw_html


class MemberActivitySettlementRequestForm(forms.Form):
    MAX_CONTENT_CHARS = 10_000

    result_content_html = forms.CharField(
        required=True,
        label="결과 내용",
        widget=forms.HiddenInput(),
    )

    def clean_result_content_html(self) -> str:
        raw_html = str(self.cleaned_data.get("result_content_html", "")).strip()
        text = strip_tags(raw_html).strip()
        if not text:
            raise forms.ValidationError("결과 내용을 입력해 주세요.")
        if len(text) > self.MAX_CONTENT_CHARS:
            truncated_text = text[: self.MAX_CONTENT_CHARS]
            return f"<p>{escape(truncated_text)}</p>"
        return raw_html
