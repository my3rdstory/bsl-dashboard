from django.core.exceptions import ValidationError

# XSS/스크립트 인젝션 가능성이 높은 기호는 닉네임에서 제한한다.
FORBIDDEN_NICKNAME_CHARS = frozenset(
    {
        "<",
        ">",
        '"',
        "'",
        "`",
        "\\",
        "/",
        ";",
        "(",
        ")",
        "{",
        "}",
        "[",
        "]",
    }
)


def validate_member_nickname(raw_value: str) -> str:
    nickname = str(raw_value or "").strip()
    if not nickname:
        raise ValidationError("닉네임을 입력해 주세요.")
    if len(nickname) > 80:
        raise ValidationError("닉네임은 80자 이하로 입력해 주세요.")

    for char in nickname:
        code_point = ord(char)
        if code_point < 32 or code_point == 127:
            raise ValidationError("닉네임에 제어 문자는 사용할 수 없습니다.")
        if char in FORBIDDEN_NICKNAME_CHARS:
            raise ValidationError("닉네임에 사용할 수 없는 특수문자가 포함되어 있습니다.")

    return nickname
