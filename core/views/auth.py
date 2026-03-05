import json
from secrets import token_hex
from urllib.parse import quote_plus
from hashlib import sha256
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    encode_dss_signature,
    decode_dss_signature,
)
from bech32 import bech32_encode, convertbits
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt

from core.forms import (
    LightningApplicationForm,
    LightningNicknameForm,
    SuperAdminLoginForm,
)
from core.models import (
    LightningIdentity,
    LightningLoginAttempt,
    Member,
    ensure_superadmin_member,
    ensure_member_domain_records,
    open_member_year_membership,
)
from core.services import send_pending_member_share_messages


logger = logging.getLogger(__name__)


def verify_signature(
    signature_bytes: bytes,
    k1_bytes: bytes,
    key_bytes: bytes,
    hasher=None,
) -> bool:
    if hasher is not None:
        try:
            prepared = hasher(k1_bytes)
        except TypeError:
            return False
        if hasattr(prepared, "digest"):
            k1_bytes = prepared.digest()
        if len(k1_bytes) != 32:
            return False
        message_candidates = [k1_bytes]
    else:
        message_candidates = [sha256(k1_bytes).digest(), k1_bytes]

    if len(k1_bytes) != 32:
        return False

    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), key_bytes
        )
    except ValueError:
        return False

    signature_der_candidates: list[bytes] = []
    if len(signature_bytes) == 64:
        r = int.from_bytes(signature_bytes[:32], "big")
        s = int.from_bytes(signature_bytes[32:], "big")
        try:
            signature_der_candidates.append(encode_dss_signature(r, s))
        except (ValueError, TypeError):
            return False
    else:
        signature_der_candidates.append(signature_bytes)

    if not signature_der_candidates:
        return False

    verified_candidate_signature: bytes | None = None
    for candidate in signature_der_candidates:
        try:
            decode_dss_signature(candidate)
            verified_candidate_signature = candidate
            break
        except Exception:
            continue
    if verified_candidate_signature is None:
        return False

    verify_algorithms = [
        ec.ECDSA(Prehashed(hashes.SHA256())),
        ec.ECDSA(hashes.SHA256()),
    ]

    for message in message_candidates:
        for algorithm in verify_algorithms:
            try:
                public_key.verify(
                    verified_candidate_signature,
                    message,
                    algorithm,
                )
                return True
            except (InvalidSignature, ValueError, TypeError):
                continue

    return False


def _lightning_callback_url(request: HttpRequest) -> str:
    callback_url = request.build_absolute_uri(reverse("core:lightning-callback"))
    if not settings.DEBUG and callback_url.startswith("http://"):
        return "https://" + callback_url[len("http://") :]
    return callback_url


def _random_username() -> str:
    return f"ln_{token_hex(12)}"


def _resolve_next_url(request: HttpRequest, fallback: str = "/dashboard/") -> str:
    next_url = request.GET.get("next") or request.POST.get("next") or fallback
    if not isinstance(next_url, str):
        return fallback
    if not next_url.startswith("/"):
        return fallback
    if not url_has_allowed_host_and_scheme(
        next_url, {request.get_host()}, require_https=False
    ):
        return fallback
    return next_url


def _debug_fake_enabled() -> bool:
    return settings.DEBUG and getattr(settings, "DEBUG_FAKE_LIGHTNING_LOGIN", True)


def _lnurl_enabled() -> bool:
    return getattr(settings, "LNURL_AUTH_ENABLED", True)


def _random_wallet_key() -> str:
    return f"debug_wallet_{token_hex(16)}"


def _get_debug_wallet_key(request: HttpRequest) -> str:
    fixed_wallet_key = str(
        getattr(settings, "DEBUG_FAKE_LIGHTNING_FIXED_WALLET_KEY", "")
    ).strip()
    if fixed_wallet_key:
        return fixed_wallet_key

    session_key = "debug_lightning_wallet_key"
    wallet_key = request.session.get(session_key)
    if isinstance(wallet_key, str) and wallet_key:
        return wallet_key

    wallet_key = _random_wallet_key()
    while LightningIdentity.objects.filter(wallet_key=wallet_key).exists():
        wallet_key = _random_wallet_key()
    request.session[session_key] = wallet_key
    return wallet_key


def _debug_wallet_key_for_ui(request: HttpRequest) -> str:
    fixed_wallet_key = str(
        getattr(settings, "DEBUG_FAKE_LIGHTNING_FIXED_WALLET_KEY", "")
    ).strip()
    if fixed_wallet_key:
        return fixed_wallet_key
    session_wallet_key = request.session.get("debug_lightning_wallet_key")
    if isinstance(session_wallet_key, str):
        return session_wallet_key.strip()
    return ""


def _random_lightning_identifier() -> str:
    return f"lnid_{token_hex(18)}"


def _first_login_nickname_session_key(token: str) -> str:
    return f"lightning_first_nickname_{token}"


def _issue_unique_lightning_identifier(preferred_value: str = "") -> str:
    candidate = preferred_value.strip()
    if candidate and not Member.objects.filter(lightning_identifier=candidate).exists():
        return candidate

    candidate = _random_lightning_identifier()
    while Member.objects.filter(lightning_identifier=candidate).exists():
        candidate = _random_lightning_identifier()
    return candidate


def _ensure_member_enrollment(
    user: User,
    wallet_key: str,
    nickname: str = "",
    application_note: str = "",
    request_obj: HttpRequest | None = None,
) -> Member:
    member = Member.objects.filter(user=user).first()
    if member is not None:
        updated_fields: list[str] = []
        same_lightning_key = bool(wallet_key) and member.lightning_identifier == wallet_key
        cleaned_nickname = nickname.strip()
        cleaned_application_note = application_note.strip()
        if cleaned_nickname and member.display_name != cleaned_nickname:
            member.display_name = cleaned_nickname
            updated_fields.append("display_name")
        if (
            cleaned_application_note
            and member.application_note != cleaned_application_note
        ):
            member.application_note = cleaned_application_note
            updated_fields.append("application_note")
        if wallet_key and member.lightning_identifier != wallet_key:
            key_conflict = Member.objects.filter(
                lightning_identifier=wallet_key
            ).exclude(id=member.id)
            if not key_conflict.exists():
                member.lightning_identifier = wallet_key
                updated_fields.append("lightning_identifier")
        if updated_fields:
            member.save(update_fields=updated_fields)
        ensure_member_domain_records(member)
        if member.status == Member.STATUS_SUSPENDED and same_lightning_key:
            member.status = Member.STATUS_ACTIVE
            member.save(update_fields=["status"])
            lifecycle = member.lifecycle
            if lifecycle.withdrawn_at is not None:
                lifecycle.withdrawn_at = None
                lifecycle.save(update_fields=["withdrawn_at"])
            open_member_year_membership(member)
        elif member.status == Member.STATUS_ACTIVE:
            open_member_year_membership(member)
        return member

    display_name = nickname.strip() or user.get_username() or "신규 회원"
    cleaned_application_note = application_note.strip()
    member = Member.objects.create(
        user=user,
        display_name=display_name,
        lightning_identifier=_issue_unique_lightning_identifier(wallet_key),
        application_note=cleaned_application_note,
        status=Member.STATUS_PENDING,
    )
    ensure_member_domain_records(member)
    send_pending_member_share_messages(member, request_obj=request_obj)
    return member


def _resolve_known_user_by_wallet_key(wallet_key: str) -> User | None:
    normalized_wallet_key = str(wallet_key or "").strip()
    if not normalized_wallet_key:
        return None

    identity = (
        LightningIdentity.objects.filter(wallet_key__iexact=normalized_wallet_key)
        .select_related("user")
        .first()
    )
    if identity is not None:
        update_fields = ["last_authenticated_at"]
        identity.last_authenticated_at = timezone.now()
        if identity.wallet_key != normalized_wallet_key:
            key_conflict = LightningIdentity.objects.filter(
                wallet_key=normalized_wallet_key
            ).exclude(id=identity.id)
            if not key_conflict.exists():
                identity.wallet_key = normalized_wallet_key
                update_fields.append("wallet_key")
        identity.save(update_fields=update_fields)
        return identity.user

    member = (
        Member.objects.filter(lightning_identifier__iexact=normalized_wallet_key)
        .select_related("user")
        .first()
    )
    if member is None or member.user_id is None:
        return None

    LightningIdentity.objects.update_or_create(
        user=member.user,
        defaults={
            "wallet_key": normalized_wallet_key,
            "last_authenticated_at": timezone.now(),
        },
    )
    return member.user


def _remaining_seconds(attempt: LightningLoginAttempt) -> int:
    remaining = int((attempt.expires_at - timezone.now()).total_seconds())
    return max(0, remaining)


def _build_qr_payload(request: HttpRequest, k1: str) -> str:
    callback_url = _lightning_callback_url(request)
    login_url = f"{callback_url}?tag=login&k1={k1}&action=login"
    five_bit_data = convertbits(login_url.encode("utf-8"), 8, 5, True)
    if five_bit_data is None:
        return login_url
    return bech32_encode("lnurl", five_bit_data)


def _qr_image_url(payload: str) -> str:
    return f"https://api.qrserver.com/v1/create-qr-code/?size=280x280&data={quote_plus(payload)}"


def _normalize_callback_payload(request: HttpRequest) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, value in request.GET.items():
        payload[str(key)] = str(value).strip()

    if request.method == "POST":
        for key, value in request.POST.items():
            payload[str(key)] = str(value).strip()

        content_type = (request.META.get("CONTENT_TYPE") or "").lower()
        if "application/json" in content_type:
            try:
                request_body = request.body
            except Exception:
                request_body = b""
            if request_body:
                try:
                    body_json = json.loads(request_body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                    logger.warning("LNURL callback JSON 파싱 실패: k1=%s", request.GET.get("k1", "")[:12])
                else:
                    if isinstance(body_json, dict):
                        for key, value in body_json.items():
                            payload[str(key)] = str(value).strip()

    return payload


def _get_param(payload: dict[str, str], *candidates: str) -> str:
    lowered_payload = {str(key).lower(): str(value).strip() for key, value in payload.items()}
    for key in candidates:
        value = lowered_payload.get(str(key).lower())
        if value:
            return value
    return ""


def entry(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("core:home")
    return redirect("core:lightning-login")


def admin_login_redirect(request: HttpRequest) -> HttpResponse:
    next_url = _resolve_next_url(request, "/back-office/admin/")
    return redirect(f"{reverse('core:lightning-login')}?next={quote_plus(next_url)}")


def superadmin_login(request: HttpRequest) -> HttpResponse:
    next_url = _resolve_next_url(request, "/back-office/admin/")
    if request.user.is_authenticated:
        if request.user.is_superuser:
            ensure_superadmin_member(request.user)
            return redirect(next_url)
        return redirect("core:home")

    if request.method == "POST":
        form = SuperAdminLoginForm(request=request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            ensure_superadmin_member(user)
            login(request, user)
            return redirect(next_url)
    else:
        form = SuperAdminLoginForm(request=request)

    return render(
        request,
        "registration/superadmin_login.html",
        {"form": form, "next_url": next_url},
    )


def lightning_login(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        next_url = _resolve_next_url(request)
        return redirect(next_url)
    next_url = _resolve_next_url(request)
    return render(request, "registration/login.html", {"next_url": next_url})


def lightning_login_start(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest(b"POST only")

    next_url = _resolve_next_url(request)

    if not _lnurl_enabled():
        return HttpResponseBadRequest(b"LNURL disabled")

    attempt = LightningLoginAttempt.objects.create(
        k1=token_hex(32),
        auth_invoice="",
        qr_payload="",
        expires_at=LightningLoginAttempt.default_expiry(),
    )
    lnurl_code = _build_qr_payload(request, attempt.k1)
    attempt.auth_invoice = lnurl_code
    attempt.qr_payload = lnurl_code
    attempt.save(update_fields=["auth_invoice", "qr_payload"])
    request.session[f"lightning_next_{attempt.token}"] = next_url

    context = {
        "attempt": attempt,
        "qr_image_url": _qr_image_url(attempt.qr_payload),
        "next_url": next_url,
        "debug_fake_enabled": _debug_fake_enabled(),
        "debug_wallet_key": _debug_wallet_key_for_ui(request),
        "remaining_seconds": _remaining_seconds(attempt),
    }
    return render(request, "core/auth/lightning_session_panel.html", context)


def lightning_debug_fake_auth(request: HttpRequest, token: str) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest(b"POST only")
    if not _debug_fake_enabled():
        return HttpResponse(status=404)

    attempt = get_object_or_404(LightningLoginAttempt, token=token)
    if attempt.is_expired():
        attempt.status = LightningLoginAttempt.STATUS_EXPIRED
        attempt.failure_reason = "인증 시간이 만료되었습니다."
        attempt.save(update_fields=["status", "failure_reason"])
        return lightning_poll(request, token)

    manual_wallet_key = str(request.POST.get("debug_wallet_key", "")).strip()
    wallet_key = (
        manual_wallet_key[:130]
        if manual_wallet_key
        else (attempt.wallet_key or _get_debug_wallet_key(request))
    )
    if manual_wallet_key:
        request.session["debug_lightning_wallet_key"] = wallet_key
    known_user = _resolve_known_user_by_wallet_key(wallet_key)

    attempt.wallet_key = wallet_key
    attempt.status = LightningLoginAttempt.STATUS_SUCCESS
    attempt.verified_at = timezone.now()
    attempt.failure_reason = ""
    if known_user is not None:
        attempt.user = known_user
        attempt.require_nickname = False
    else:
        attempt.user = None
        attempt.require_nickname = True
    attempt.save(
        update_fields=[
            "wallet_key",
            "status",
            "verified_at",
            "failure_reason",
            "user",
            "require_nickname",
        ]
    )
    return lightning_poll(request, token)


@csrf_exempt
def lightning_callback(request: HttpRequest) -> HttpResponse:
    request_data = _normalize_callback_payload(request)
    user_agent = str(request.META.get("HTTP_USER_AGENT", "")).strip()
    remote_addr = str(
        request.META.get("HTTP_X_FORWARDED_FOR")
        or request.META.get("REMOTE_ADDR")
        or ""
    ).split(",")[0].strip()
    logger.warning(
        "LNURL callback 수신: path=%s query=%s",
        request.path,
        request.META.get("QUERY_STRING", ""),
    )
    if not _lnurl_enabled():
        logger.warning("LNURL callback 요청 - LNURL 비활성화")
        return JsonResponse({"status": "ERROR", "reason": "LNURL disabled"}, status=400)

    tag = _get_param(request_data, "tag")
    k1 = _get_param(request_data, "k1").lower()
    wallet_key = _get_param(request_data, "key", "pubkey", "wallet_key", "public_key")
    signature = _get_param(request_data, "sig", "signature", "sig_hex")

    if tag and tag != "login":
        logger.warning("LNURL callback 태그 오류: tag=%s", tag)
        return JsonResponse(
            {"status": "ERROR", "reason": "Unsupported tag", "code": "unsupported_tag"}
        )

    if len(k1) != 64:
        logger.warning("LNURL callback k1 길이 오류: len=%s", len(k1))
        return JsonResponse(
            {"status": "ERROR", "reason": "Invalid k1", "code": "invalid_k1"}
        )

    attempt = LightningLoginAttempt.objects.filter(k1=k1).first()
    if attempt is None:
        logger.warning("LNURL callback 미확인 k1 수신: k1=%s", k1[:12])
        return JsonResponse(
            {"status": "ERROR", "reason": "Unknown k1", "code": "unknown_k1"}
        )

    if attempt.is_expired():
        attempt.status = LightningLoginAttempt.STATUS_EXPIRED
        attempt.failure_reason = "인증 시간이 만료되었습니다."
        attempt.save(update_fields=["status", "failure_reason"])
        return JsonResponse(
            {"status": "ERROR", "reason": "Expired login request", "code": "expired"}
        )

    if attempt.status == LightningLoginAttempt.STATUS_SUCCESS:
        previous_wallet_key = str(attempt.wallet_key or "").strip().lower()
        if previous_wallet_key and wallet_key and previous_wallet_key != wallet_key:
            logger.warning("LNURL callback 중복/다른 키 시도: token=%s", str(attempt.token))
            return JsonResponse(
                {
                    "status": "ERROR",
                    "reason": "Already verified with different key",
                    "code": "already_used",
                }
            )
        return JsonResponse({"status": "OK"})

    if not wallet_key or not signature:
        present_keys = sorted(request_data.keys())
        missing_key_error = False
        if any(k in present_keys for k in ["action", "tag"]) and "k1" in present_keys:
            if "key" not in present_keys and "pubkey" not in present_keys and "wallet_key" not in present_keys and "public_key" not in present_keys:
                missing_key_error = True
            if "sig" not in present_keys and "signature" not in present_keys and "sig_hex" not in present_keys:
                missing_key_error = True

        attempt.status = LightningLoginAttempt.STATUS_FAILED
        attempt.failure_reason = (
            "이 지갑은 LNURL 인증 응답 형식이 달라 key/sig를 받지 못했습니다."
            if missing_key_error
            else "지갑 인증 데이터가 누락되었습니다."
        )
        attempt.save(update_fields=["status", "failure_reason"])
        logger.warning(
            "LNURL callback 파라미터 누락: k1=%s present=%s ua=%s ip=%s",
            k1[:12],
            ",".join(sorted(request_data.keys())),
            user_agent,
            remote_addr,
        )
        return JsonResponse(
            {
                "status": "ERROR",
                "reason": "Missing key or signature",
                "code": "missing_params",
            }
        )

    try:
        k1_bytes = bytes.fromhex(k1)
        signature_bytes = bytes.fromhex(signature)
        key_bytes = bytes.fromhex(wallet_key)
    except ValueError:
        attempt.status = LightningLoginAttempt.STATUS_FAILED
        attempt.failure_reason = "HEX 형식이 잘못되었습니다."
        attempt.save(update_fields=["status", "failure_reason"])
        logger.warning("LNURL callback HEX 형식 오류: k1_prefix=%s, key_prefix=%s", k1[:12], wallet_key[:12])
        return JsonResponse(
            {
                "status": "ERROR",
                "reason": "Invalid hex format in key/sig/k1",
                "code": "invalid_hex",
            }
        )

    if not verify_signature(signature_bytes, k1_bytes, key_bytes, hasher=None):
        attempt.status = LightningLoginAttempt.STATUS_FAILED
        attempt.failure_reason = "서명 검증에 실패했습니다."
        attempt.save(update_fields=["status", "failure_reason"])
        logger.warning(
            "LNURL callback 서명 검증 실패: k1=%s, key_prefix=%s",
            k1[:12],
            wallet_key[:12],
        )
        return JsonResponse(
            {
                "status": "ERROR",
                "reason": "Invalid signature",
                "code": "invalid_signature",
            }
        )

    known_user = _resolve_known_user_by_wallet_key(wallet_key)
    attempt.wallet_key = wallet_key
    attempt.status = LightningLoginAttempt.STATUS_SUCCESS
    attempt.verified_at = timezone.now()
    if known_user is not None:
        attempt.user = known_user
        attempt.require_nickname = False
    else:
        attempt.require_nickname = True
    attempt.failure_reason = ""
    attempt.save(
        update_fields=[
            "wallet_key",
            "status",
            "verified_at",
            "user",
            "require_nickname",
            "failure_reason",
        ]
    )
    logger.info("LNURL callback 인증 성공: k1=%s", k1[:12])
    return JsonResponse({"status": "OK"})


def lightning_poll(request: HttpRequest, token: str) -> HttpResponse:
    attempt = get_object_or_404(LightningLoginAttempt, token=token)
    next_url = str(
        request.session.get(f"lightning_next_{attempt.token}", "/dashboard/")
    )
    if attempt.status == LightningLoginAttempt.STATUS_PENDING and attempt.is_expired():
        attempt.status = LightningLoginAttempt.STATUS_EXPIRED
        attempt.failure_reason = "인증 시간이 만료되었습니다."
        attempt.save(update_fields=["status", "failure_reason"])

    if attempt.status == LightningLoginAttempt.STATUS_SUCCESS and attempt.user_id:
        attempt_user = attempt.user
        if attempt_user is None:
            return HttpResponseBadRequest(b"invalid user")
        _ensure_member_enrollment(
            attempt_user,
            attempt.wallet_key,
            request_obj=request,
        )
        login(request, attempt_user)
        response = HttpResponse("", status=204)
        response["HX-Redirect"] = next_url
        return response

    first_login_nickname = str(
        request.session.get(_first_login_nickname_session_key(str(attempt.token)), "")
    ).strip()
    context = {
        "attempt": attempt,
        "qr_image_url": _qr_image_url(attempt.qr_payload),
        "nickname_form": LightningNicknameForm(
            initial={"nickname": first_login_nickname}
        ),
        "application_form": LightningApplicationForm(),
        "first_login_nickname": first_login_nickname,
        "next_url": next_url,
        "debug_fake_enabled": _debug_fake_enabled(),
        "debug_wallet_key": _debug_wallet_key_for_ui(request),
        "remaining_seconds": _remaining_seconds(attempt),
    }
    return render(request, "core/auth/lightning_session_panel.html", context)


def lightning_complete_first_login(request: HttpRequest, token: str) -> HttpResponse:
    attempt = get_object_or_404(LightningLoginAttempt, token=token)
    next_url = str(
        request.session.get(f"lightning_next_{attempt.token}", "/dashboard/")
    )
    if request.method != "POST":
        return HttpResponseBadRequest(b"POST only")

    if attempt.status != LightningLoginAttempt.STATUS_SUCCESS or attempt.user_id:
        return HttpResponseBadRequest(b"invalid state")

    nickname_session_key = _first_login_nickname_session_key(str(attempt.token))

    if attempt.require_nickname:
        nickname_form = LightningNicknameForm(request.POST)
        if not nickname_form.is_valid():
            context = {
                "attempt": attempt,
                "qr_image_url": _qr_image_url(attempt.qr_payload),
                "nickname_form": nickname_form,
                "application_form": LightningApplicationForm(),
                "first_login_nickname": "",
                "next_url": next_url,
                "debug_fake_enabled": _debug_fake_enabled(),
                "remaining_seconds": _remaining_seconds(attempt),
            }
            return render(
                request, "core/auth/lightning_session_panel.html", context, status=400
            )

        nickname = nickname_form.cleaned_data["nickname"].strip()
        request.session[nickname_session_key] = nickname
        attempt.require_nickname = False
        attempt.save(update_fields=["require_nickname"])

        context = {
            "attempt": attempt,
            "qr_image_url": _qr_image_url(attempt.qr_payload),
            "nickname_form": LightningNicknameForm(initial={"nickname": nickname}),
            "application_form": LightningApplicationForm(),
            "first_login_nickname": nickname,
            "next_url": next_url,
            "debug_fake_enabled": _debug_fake_enabled(),
            "remaining_seconds": _remaining_seconds(attempt),
        }
        return render(request, "core/auth/lightning_session_panel.html", context)

    nickname = str(request.session.get(nickname_session_key, "")).strip()
    if not nickname:
        attempt.require_nickname = True
        attempt.save(update_fields=["require_nickname"])
        nickname_form = LightningNicknameForm()
        nickname_form.add_error(None, "닉네임 입력 단계가 만료되어 다시 입력해 주세요.")
        context = {
            "attempt": attempt,
            "qr_image_url": _qr_image_url(attempt.qr_payload),
            "nickname_form": nickname_form,
            "application_form": LightningApplicationForm(),
            "first_login_nickname": "",
            "next_url": next_url,
            "debug_fake_enabled": _debug_fake_enabled(),
            "remaining_seconds": _remaining_seconds(attempt),
        }
        return render(
            request, "core/auth/lightning_session_panel.html", context, status=400
        )

    application_form = LightningApplicationForm(request.POST)
    if not application_form.is_valid():
        context = {
            "attempt": attempt,
            "qr_image_url": _qr_image_url(attempt.qr_payload),
            "nickname_form": LightningNicknameForm(initial={"nickname": nickname}),
            "application_form": application_form,
            "first_login_nickname": nickname,
            "next_url": next_url,
            "debug_fake_enabled": _debug_fake_enabled(),
            "remaining_seconds": _remaining_seconds(attempt),
        }
        return render(
            request, "core/auth/lightning_session_panel.html", context, status=400
        )

    user = None
    while user is None:
        candidate = _random_username()
        if not User.objects.filter(username=candidate).exists():
            user = User.objects.create(username=candidate)

    user.set_unusable_password()
    user.save(update_fields=["password"])

    wallet_key = attempt.wallet_key.strip()
    if not wallet_key:
        return HttpResponseBadRequest(b"missing wallet key")

    application_note = application_form.cleaned_data["application_note"].strip()
    _ensure_member_enrollment(
        user,
        wallet_key,
        nickname=nickname,
        application_note=application_note,
        request_obj=request,
    )
    LightningIdentity.objects.update_or_create(
        user=user,
        defaults={
            "wallet_key": wallet_key,
            "last_authenticated_at": timezone.now(),
        },
    )

    attempt.user = user
    attempt.save(update_fields=["user"])
    request.session.pop(nickname_session_key, None)

    login(request, user)
    response = HttpResponse("", status=204)
    response["HX-Redirect"] = next_url
    return response
