from django import forms


class DuesSubmitForm(forms.Form):
    payment_reference = forms.CharField(
        max_length=160, label="납부 트랜잭션/인보이스 ID"
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["payment_reference"].widget.attrs["class"] = "input is-small"
