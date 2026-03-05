from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from .message_share import (
    EVENT_MEMBER_DUES_CONFIRMED,
    send_member_status_share_messages,
)
from core.models import (
    CollectionBalanceCache,
    Member,
    MemberAddressHistory,
    MemberDuesPayment,
    MemberReviewLog,
    build_monthly_dues_plan,
    ensure_member_domain_records,
    get_basic_dues_setting,
    log_member_review,
    open_member_year_membership,
)


class DuesPaymentServiceError(Exception):
    pass


@dataclass(frozen=True)
class BlinkPaymentSendResult:
    status: str
    reference: str
    note: str


@dataclass(frozen=True)
class CollectionBalanceSummary:
    onchain_sats: int | None
    lightning_sats: int | None
    total_sats: int | None
    note: str
    source_owner: str


PENDING_PAYMENT_STATUSES = {
    MemberDuesPayment.STATUS_READY,
    MemberDuesPayment.STATUS_WAITING_ONCHAIN,
    MemberDuesPayment.STATUS_WAITING_LIGHTNING,
}
REQUEST_GATE_PAYMENT_STATUSES = PENDING_PAYMENT_STATUSES | {
    MemberDuesPayment.STATUS_FAILED
}

BALANCE_REFRESH_INTERVAL = timedelta(hours=1)


def format_due_payment_review_note(payment: MemberDuesPayment | None) -> str:
    if payment is None:
        return "회비확인 처리"

    if payment.payment_method == MemberDuesPayment.METHOD_ONCHAIN:
        txid = payment.onchain_txid.strip()
        return f"온체인: {txid}" if txid else "온체인"

    if payment.payment_method == MemberDuesPayment.METHOD_LIGHTNING:
        invoice = payment.lightning_invoice.strip()
        return f"라이트닝: {invoice}" if invoice else "라이트닝"

    return "회비확인 처리"


def _month_start(dt: datetime) -> date:
    localized = timezone.localtime(dt)
    return date(localized.year, localized.month, 1)


def _resolve_member_due_amount(member: Member) -> int:
    setting = get_basic_dues_setting()
    monthly_plan = build_monthly_dues_plan(
        annual_dues_sats=int(setting.annual_dues_sats),
        year=int(setting.configured_year),
    )
    target_month = int(timezone.localtime(member.requested_at).month)
    for row in monthly_plan:
        if int(row["month"]) == target_month:
            return int(row["total_fee_sats"])
    return int(setting.annual_dues_sats)


def get_staff_requested_due_payment(member: Member) -> MemberDuesPayment | None:
    return (
        member.dues_payments.filter(
            requested_by_staff_at__isnull=False,
            status__in=REQUEST_GATE_PAYMENT_STATUSES,
        )
        .order_by("-requested_by_staff_at", "-created_at", "-id")
        .first()
    )


def _resolve_collection_address() -> MemberAddressHistory | None:
    # 기본은 백오피스 어드민의 현재 주소를 사용한다.
    preferred = (
        MemberAddressHistory.objects.filter(is_current=True, member__is_backoffice_admin=True)
        .select_related("member")
        .order_by("-created_at", "-id")
        .first()
    )
    if preferred is not None:
        return preferred
    # 운영자 권한 플래그가 바뀐 경우에도 기존 현재 주소를 재사용할 수 있게 fallback.
    return (
        MemberAddressHistory.objects.filter(is_current=True)
        .select_related("member")
        .order_by("-created_at", "-id")
        .first()
    )


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
    timeout_sec: int = 10,
) -> dict[str, object]:
    request_headers = dict(headers or {})
    body_bytes = None
    if payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url=url, data=body_bytes, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8")
            return dict(json.loads(raw)) if raw else {}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise DuesPaymentServiceError(str(exc)) from exc


def _blink_graphql(api_key: str, query: str, variables: dict[str, object]) -> dict[str, object]:
    primary_endpoint = str(
        getattr(settings, "BLINK_GRAPHQL_ENDPOINT", "https://api.blink.sv/graphql")
    ).strip()
    if not primary_endpoint:
        primary_endpoint = "https://api.blink.sv/graphql"

    endpoint_candidates = [primary_endpoint]
    for fallback_endpoint in [
        "https://api.blink.sv/graphql",
        "https://api.staging.blink.sv/graphql",
    ]:
        if fallback_endpoint not in endpoint_candidates:
            endpoint_candidates.append(fallback_endpoint)

    header_candidates = [
        {"X-API-KEY": api_key},
        {"Authorization": f"Bearer {api_key}"},
        {"Authorization": f"Bearer {api_key}", "X-API-KEY": api_key},
    ]

    def _is_unauthorized(text: str) -> bool:
        lowered = text.lower()
        return "401" in text or "unauthorized" in lowered or "unauthenticated" in lowered

    unauthorized_errors: list[str] = []
    first_non_auth_error: DuesPaymentServiceError | None = None
    for endpoint in endpoint_candidates:
        for headers in header_candidates:
            try:
                result = _http_json(
                    endpoint,
                    method="POST",
                    headers=headers,
                    payload={"query": query, "variables": variables},
                    timeout_sec=15,
                )
            except DuesPaymentServiceError as exc:
                error_text = str(exc)
                if _is_unauthorized(error_text):
                    unauthorized_errors.append(f"{endpoint} -> {error_text}")
                    continue
                if first_non_auth_error is None:
                    first_non_auth_error = exc
                continue

            if "errors" in result and result["errors"]:
                error_text = str(result["errors"])
                if _is_unauthorized(error_text):
                    unauthorized_errors.append(f"{endpoint} -> {error_text}")
                    continue
                raise DuesPaymentServiceError(error_text)
            return result

    if first_non_auth_error is not None:
        raise first_non_auth_error

    if unauthorized_errors:
        tried = ", ".join(endpoint_candidates)
        raise DuesPaymentServiceError(
            "블링크 인증 실패(401): API Key 또는 Wallet ID가 유효하지 않거나 엔드포인트가 다를 수 있습니다. "
            f"현재 시도 엔드포인트: {tried} (필요 시 BLINK_GRAPHQL_ENDPOINT 확인)"
        )
    raise DuesPaymentServiceError("블링크 GraphQL 호출에 실패했습니다.")


def _resolve_blink_wallet_id(api_key: str, preferred_wallet_id: str = "") -> str:
    query = """
    query MeWallets {
      me {
        defaultAccount {
          wallets {
            id
            walletCurrency
          }
        }
      }
    }
    """
    result = _blink_graphql(api_key, query, {})
    wallets = (
        result.get("data", {})
        .get("me", {})
        .get("defaultAccount", {})
        .get("wallets", [])
    )
    if not isinstance(wallets, list) or not wallets:
        raise DuesPaymentServiceError(
            "블링크 계정 지갑 정보를 찾을 수 없습니다. API Key 권한을 확인해 주세요."
        )

    wallet_currency_map: dict[str, str] = {}
    btc_wallet_ids: list[str] = []
    for wallet in wallets:
        if not isinstance(wallet, dict):
            continue
        wallet_id = str(wallet.get("id", "")).strip()
        wallet_currency = str(wallet.get("walletCurrency", "")).strip().upper()
        if not wallet_id:
            continue
        wallet_currency_map[wallet_id] = wallet_currency
        if wallet_currency == "BTC":
            btc_wallet_ids.append(wallet_id)

    if preferred_wallet_id:
        normalized = preferred_wallet_id.strip()
        if normalized in wallet_currency_map and wallet_currency_map[normalized] == "BTC":
            return normalized
        if normalized in wallet_currency_map:
            raise DuesPaymentServiceError("블링크 Wallet ID는 BTC 지갑만 사용할 수 있습니다.")
        raise DuesPaymentServiceError("입력한 블링크 Wallet ID가 API Key 계정에 없습니다.")

    if not btc_wallet_ids:
        raise DuesPaymentServiceError("API Key 계정에서 BTC 지갑을 찾지 못했습니다.")
    return btc_wallet_ids[0]


def validate_and_resolve_blink_wallet_id(api_key: str, wallet_id: str = "") -> str:
    normalized_api_key = str(api_key).strip()
    if not normalized_api_key:
        raise DuesPaymentServiceError("블링크 API Key를 입력해 주세요.")
    return _resolve_blink_wallet_id(normalized_api_key, wallet_id)


def _parse_sats_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if "." in text:
            return int(float(text))
        return int(text)
    if isinstance(value, dict):
        for key in ("amount", "value", "sats"):
            if key in value:
                return _parse_sats_int(value.get(key))
    return 0


def _fetch_onchain_address_balance_sats(address: str) -> int:
    normalized_address = str(address).strip()
    if not normalized_address:
        raise DuesPaymentServiceError("온체인 주소가 비어 있습니다.")

    base_url = str(getattr(settings, "MEMPOOL_API_BASE_URL", "https://mempool.space")).rstrip("/")
    address_url = f"{base_url}/api/address/{quote_plus(normalized_address)}"
    address_data = _http_json(address_url, timeout_sec=8)
    chain_stats = address_data.get("chain_stats", {}) if isinstance(address_data, dict) else {}
    mempool_stats = (
        address_data.get("mempool_stats", {}) if isinstance(address_data, dict) else {}
    )

    confirmed_balance = int(chain_stats.get("funded_txo_sum", 0) or 0) - int(
        chain_stats.get("spent_txo_sum", 0) or 0
    )
    unconfirmed_balance = int(mempool_stats.get("funded_txo_sum", 0) or 0) - int(
        mempool_stats.get("spent_txo_sum", 0) or 0
    )
    return confirmed_balance + unconfirmed_balance


def _fetch_blink_btc_wallet_balance_sats(api_key: str) -> int:
    normalized_key = str(api_key).strip()
    if not normalized_key:
        raise DuesPaymentServiceError("블링크 API Key가 비어 있습니다.")

    query = """
    query MeWalletBalances {
      me {
        defaultAccount {
          wallets {
            id
            walletCurrency
            balance
          }
        }
      }
    }
    """
    result = _blink_graphql(normalized_key, query, {})
    wallets = (
        result.get("data", {})
        .get("me", {})
        .get("defaultAccount", {})
        .get("wallets", [])
    )
    if not isinstance(wallets, list) or not wallets:
        raise DuesPaymentServiceError("블링크 지갑 잔액 정보를 찾을 수 없습니다.")

    total_balance = 0
    btc_wallet_count = 0
    for wallet in wallets:
        if not isinstance(wallet, dict):
            continue
        wallet_currency = str(wallet.get("walletCurrency", "")).strip().upper()
        if wallet_currency != "BTC":
            continue
        btc_wallet_count += 1
        total_balance += _parse_sats_int(wallet.get("balance"))

    if btc_wallet_count <= 0:
        raise DuesPaymentServiceError("블링크 BTC 지갑을 찾지 못했습니다.")
    return total_balance


def _build_balance_note(cache_row: CollectionBalanceCache) -> str:
    note_parts = ["잔액은 1시간 간격으로 조회합니다."]
    if cache_row.last_fetched_at is not None:
        fetched_at_text = timezone.localtime(cache_row.last_fetched_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        note_parts.append(f"최근 조회 {fetched_at_text}")
    else:
        note_parts.append("최근 조회 이력이 없습니다.")
    if cache_row.last_error:
        note_parts.append(f"조회 오류: {cache_row.last_error} (마지막 저장 금액 표시)")
    return " / ".join(note_parts)


def get_collection_balance_summary() -> CollectionBalanceSummary:
    cache_row, _ = CollectionBalanceCache.objects.get_or_create(key="collection")
    collection_address = _resolve_collection_address()
    if collection_address is None:
        return CollectionBalanceSummary(
            onchain_sats=cache_row.onchain_sats,
            lightning_sats=cache_row.lightning_sats,
            total_sats=cache_row.total_sats,
            note=(
                "설정/주소등록에 현재 사용 주소가 없습니다. "
                + _build_balance_note(cache_row)
            ),
            source_owner=cache_row.source_owner_display_name or "-",
        )

    now = timezone.now()
    source_owner = str(collection_address.member.display_name)
    source_onchain_address = str(collection_address.bitcoin_onchain_address or "").strip()
    source_wallet_id = str(collection_address.blink_wallet_id or "").strip()

    should_refresh = True
    if (
        cache_row.last_attempted_at is not None
        and now - cache_row.last_attempted_at < BALANCE_REFRESH_INTERVAL
    ):
        should_refresh = False

    if cache_row.source_onchain_address != source_onchain_address:
        should_refresh = True
    if cache_row.source_blink_wallet_id != source_wallet_id:
        should_refresh = True
    if cache_row.source_owner_display_name != source_owner:
        should_refresh = True

    resolved_onchain_sats = cache_row.onchain_sats
    resolved_lightning_sats = cache_row.lightning_sats
    errors: list[str] = []

    if should_refresh:
        fetched_onchain_sats: int | None = None
        fetched_lightning_sats: int | None = None

        if source_onchain_address:
            try:
                fetched_onchain_sats = _fetch_onchain_address_balance_sats(
                    source_onchain_address
                )
            except DuesPaymentServiceError as exc:
                errors.append(f"온체인 조회 실패: {str(exc)}")
        else:
            errors.append("온체인 주소 미등록")

        blink_api_key = collection_address.get_blink_api_key()
        if blink_api_key:
            try:
                fetched_lightning_sats = _fetch_blink_btc_wallet_balance_sats(blink_api_key)
            except DuesPaymentServiceError as exc:
                errors.append(f"라이트닝 조회 실패: {str(exc)}")
        else:
            errors.append("블링크 API Key 미등록")

        if fetched_onchain_sats is not None:
            resolved_onchain_sats = fetched_onchain_sats
        if fetched_lightning_sats is not None:
            resolved_lightning_sats = fetched_lightning_sats

        cache_row.source_owner_display_name = source_owner
        cache_row.source_onchain_address = source_onchain_address
        cache_row.source_blink_wallet_id = source_wallet_id
        cache_row.onchain_sats = resolved_onchain_sats
        cache_row.lightning_sats = resolved_lightning_sats
        if resolved_onchain_sats is not None or resolved_lightning_sats is not None:
            cache_row.total_sats = int(resolved_onchain_sats or 0) + int(
                resolved_lightning_sats or 0
            )
        else:
            cache_row.total_sats = None
        cache_row.last_attempted_at = now
        if fetched_onchain_sats is not None or fetched_lightning_sats is not None:
            cache_row.last_fetched_at = now
        cache_row.last_error = " / ".join(errors)[:255]
        cache_row.save(
            update_fields=[
                "source_owner_display_name",
                "source_onchain_address",
                "source_blink_wallet_id",
                "onchain_sats",
                "lightning_sats",
                "total_sats",
                "last_attempted_at",
                "last_fetched_at",
                "last_error",
                "updated_at",
            ]
        )

    return CollectionBalanceSummary(
        onchain_sats=cache_row.onchain_sats,
        lightning_sats=cache_row.lightning_sats,
        total_sats=cache_row.total_sats,
        note=_build_balance_note(cache_row),
        source_owner=source_owner,
    )


def send_lightning_payment_to_address(
    *,
    amount_sats: int,
    lightning_address: str,
    memo: str = "",
) -> BlinkPaymentSendResult:
    target_address = str(lightning_address).strip()
    if int(amount_sats) <= 0:
        raise DuesPaymentServiceError("전송 금액은 1 sats 이상이어야 합니다.")
    if not target_address:
        raise DuesPaymentServiceError("수신 라이트닝 주소가 비어 있습니다.")

    collection_address = _resolve_collection_address()
    if collection_address is None:
        raise DuesPaymentServiceError("설정/주소등록에 블링크 정보를 먼저 등록해 주세요.")

    blink_api_key = collection_address.get_blink_api_key()
    if not blink_api_key:
        raise DuesPaymentServiceError("블링크 API Key가 설정되지 않았습니다.")

    wallet_id = validate_and_resolve_blink_wallet_id(
        blink_api_key,
        collection_address.blink_wallet_id,
    )
    if collection_address.blink_wallet_id != wallet_id:
        collection_address.blink_wallet_id = wallet_id
        collection_address.save(update_fields=["blink_wallet_id"])

    mutation = """
    mutation LnAddressPaymentSend($input: LnAddressPaymentSendInput!) {
      lnAddressPaymentSend(input: $input) {
        errors {
          message
        }
        status
        transaction {
          id
          externalId
          settlementAmount
          status
        }
      }
    }
    """
    variables = {
        "input": {
            "walletId": wallet_id,
            "lnAddress": target_address,
            "amount": int(amount_sats),
        }
    }
    result = _blink_graphql(blink_api_key, mutation, variables)
    payload = (
        result.get("data", {})
        .get("lnAddressPaymentSend", {})
    )
    errors = payload.get("errors", [])
    if isinstance(errors, list) and errors:
        first_error = errors[0] if isinstance(errors[0], dict) else {}
        error_message = str(first_error.get("message", "")).strip()
        raise DuesPaymentServiceError(
            error_message or "블링크 라이트닝 전송에 실패했습니다."
        )

    status_value = str(payload.get("status", "")).strip().upper()
    if status_value not in {"SUCCESS", "PENDING", "ALREADY_PAID"}:
        raise DuesPaymentServiceError(
            "블링크 라이트닝 전송 상태가 유효하지 않습니다."
        )

    transaction = payload.get("transaction", {}) if isinstance(payload, dict) else {}
    reference = ""
    if isinstance(transaction, dict):
        reference = str(
            transaction.get("externalId") or transaction.get("id") or ""
        ).strip()
    transfer_note_parts = [f"상태: {status_value}"]
    if reference:
        transfer_note_parts.append(f"거래참조: {reference}")
    settlement_amount = ""
    if isinstance(transaction, dict):
        settlement_amount = str(transaction.get("settlementAmount", "")).strip()
    if settlement_amount:
        transfer_note_parts.append(f"정산금액: {settlement_amount}")

    return BlinkPaymentSendResult(
        status=status_value,
        reference=reference,
        note=", ".join(transfer_note_parts),
    )


def _issue_blink_invoice(payment: MemberDuesPayment, collection_address: MemberAddressHistory) -> tuple[str, str]:
    blink_api_key = collection_address.get_blink_api_key()
    if not blink_api_key:
        raise DuesPaymentServiceError("블링크 API Key가 설정되지 않았습니다.")
    wallet_id = validate_and_resolve_blink_wallet_id(
        blink_api_key,
        collection_address.blink_wallet_id,
    )
    if collection_address.blink_wallet_id != wallet_id:
        collection_address.blink_wallet_id = wallet_id
        collection_address.save(update_fields=["blink_wallet_id"])
    if payment.destination_blink_wallet_id != wallet_id:
        payment.destination_blink_wallet_id = wallet_id
        payment.save(update_fields=["destination_blink_wallet_id", "updated_at"])

    mutation = """
    mutation LnInvoiceCreate($input: LnInvoiceCreateInput!) {
      lnInvoiceCreate(input: $input) {
        invoice {
          paymentRequest
          paymentHash
        }
        errors {
          message
        }
      }
    }
    """
    memo = f"BSL 회비 {payment.request_month:%Y-%m}"
    variables = {
        "input": {
            "walletId": wallet_id,
            "amount": int(payment.amount_sats),
            "memo": memo,
            "expiresIn": 2,
        }
    }
    result = _blink_graphql(blink_api_key, mutation, variables)
    payload = (
        result.get("data", {})
        .get("lnInvoiceCreate", {})
    )
    errors = payload.get("errors", [])
    if errors:
        raise DuesPaymentServiceError(str(errors[0].get("message", "블링크 인보이스 생성 실패")))

    invoice = payload.get("invoice") or {}
    payment_request = str(invoice.get("paymentRequest", "")).strip()
    payment_hash = str(invoice.get("paymentHash", "")).strip()
    if not payment_request:
        raise DuesPaymentServiceError("블링크 인보이스 응답이 비어 있습니다.")
    return payment_request, payment_hash


def _check_lightning_invoice_paid(
    payment: MemberDuesPayment,
    collection_address: MemberAddressHistory,
) -> bool:
    blink_api_key = collection_address.get_blink_api_key()
    if not blink_api_key or not payment.lightning_invoice:
        payment.check_note = "블링크 API Key 또는 인보이스 정보가 없습니다."
        payment.last_checked_at = timezone.now()
        payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        return False

    query = """
    query LnInvoicePaymentStatusByPaymentRequest($input: LnInvoicePaymentStatusByPaymentRequestInput!) {
      lnInvoicePaymentStatusByPaymentRequest(input: $input) {
        paymentHash
        paymentRequest
        status
      }
    }
    """
    try:
        result = _blink_graphql(
            blink_api_key,
            query,
            {"input": {"paymentRequest": payment.lightning_invoice}},
        )
    except DuesPaymentServiceError as exc:
        payment.check_note = f"라이트닝 확인 실패: {str(exc)[:180]}"
        payment.last_checked_at = timezone.now()
        payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        return False

    payload = (
        result.get("data", {})
        .get("lnInvoicePaymentStatusByPaymentRequest", {})
    )
    status_value = str(payload.get("status", "")).upper()
    now = timezone.now()
    payment.last_checked_at = now
    if status_value in {"PAID", "SETTLED", "SUCCESS"}:
        payment.lightning_invoice_paid_at = now
        payment.paid_at = now
        payment.status = MemberDuesPayment.STATUS_PAID
        payment.check_note = "라이트닝 입금 확인 완료"
        payment.save(
            update_fields=[
                "lightning_invoice_paid_at",
                "paid_at",
                "status",
                "check_note",
                "last_checked_at",
                "updated_at",
            ]
        )
        return True

    if status_value == "EXPIRED":
        payment.check_note = "라이트닝 인보이스 만료 (라이트닝 결제를 다시 선택해 주세요)"
    else:
        payment.check_note = "라이트닝 입금 대기 중"
    payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
    return False


def _check_onchain_tx_confirmed(payment: MemberDuesPayment) -> bool:
    txid = payment.onchain_txid.strip()
    if not txid:
        payment.check_note = "TXID를 입력해 주세요."
        payment.last_checked_at = timezone.now()
        payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        return False

    base_url = str(getattr(settings, "MEMPOOL_API_BASE_URL", "https://mempool.space")).rstrip("/")
    tx_url = f"{base_url}/api/tx/{quote_plus(txid)}"

    try:
        tx_data = _http_json(tx_url, timeout_sec=12)
    except DuesPaymentServiceError as exc:
        payment.check_note = f"온체인 확인 실패: {str(exc)[:180]}"
        payment.last_checked_at = timezone.now()
        payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        return False

    outputs = tx_data.get("vout", []) if isinstance(tx_data, dict) else []
    received_amount = 0
    for output in outputs if isinstance(outputs, list) else []:
        if not isinstance(output, dict):
            continue
        if str(output.get("scriptpubkey_address", "")).strip() != payment.destination_onchain_address:
            continue
        received_amount += int(output.get("value", 0) or 0)

    now = timezone.now()
    payment.last_checked_at = now
    if received_amount < int(payment.amount_sats):
        payment.check_note = "입금 금액이 기준 회비보다 적거나 대상 주소가 일치하지 않습니다."
        payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        return False

    status_obj = tx_data.get("status", {}) if isinstance(tx_data, dict) else {}
    is_confirmed = bool(status_obj.get("confirmed"))
    if is_confirmed:
        payment.onchain_confirmed_at = now
    payment.paid_at = now
    payment.status = MemberDuesPayment.STATUS_PAID
    payment.check_note = (
        "온체인 입금 확인 완료 (1컨펌 이상)"
        if is_confirmed
        else "온체인 입금 확인 완료 (미확정)"
    )
    update_fields = ["paid_at", "status", "check_note", "last_checked_at", "updated_at"]
    if is_confirmed:
        update_fields.append("onchain_confirmed_at")
    payment.save(update_fields=update_fields)
    return True


def ensure_member_due_payment(member: Member) -> MemberDuesPayment:
    if member.status != Member.STATUS_APPROVED:
        raise DuesPaymentServiceError("회비확인 대기 상태 회원만 회비 납입 요청을 생성할 수 있습니다.")

    collection_address = _resolve_collection_address()
    requested_payment = get_staff_requested_due_payment(member)
    pending = (
        member.dues_payments.filter(
            status__in=PENDING_PAYMENT_STATUSES,
            requested_by_staff_at__isnull=False,
        )
        .order_by("-requested_by_staff_at", "-created_at", "-id")
        .first()
    )
    if pending is None:
        pending = (
            member.dues_payments.filter(status__in=PENDING_PAYMENT_STATUSES)
            .order_by("-created_at", "-id")
            .first()
        )
    due_amount = _resolve_member_due_amount(member)
    target_month = _month_start(member.requested_at)
    requested_by_staff_at = (
        pending.requested_by_staff_at
        if pending is not None
        else requested_payment.requested_by_staff_at if requested_payment is not None else None
    )

    destination_onchain = (
        collection_address.bitcoin_onchain_address if collection_address is not None else ""
    )
    destination_lightning = (
        collection_address.lightning_address if collection_address is not None else ""
    )
    destination_wallet_id = (
        collection_address.blink_wallet_id if collection_address is not None else ""
    )

    if pending is not None:
        updates: list[str] = []
        if pending.amount_sats != due_amount:
            pending.amount_sats = due_amount
            updates.append("amount_sats")
        if pending.request_month != target_month:
            pending.request_month = target_month
            updates.append("request_month")
        if pending.destination_onchain_address != destination_onchain:
            pending.destination_onchain_address = destination_onchain
            updates.append("destination_onchain_address")
        if pending.destination_lightning_address != destination_lightning:
            pending.destination_lightning_address = destination_lightning
            updates.append("destination_lightning_address")
        if pending.destination_blink_wallet_id != destination_wallet_id:
            pending.destination_blink_wallet_id = destination_wallet_id
            updates.append("destination_blink_wallet_id")
        if pending.requested_by_staff_at is None and requested_by_staff_at is not None:
            pending.requested_by_staff_at = requested_by_staff_at
            updates.append("requested_by_staff_at")
        if updates:
            pending.save(update_fields=updates + ["updated_at"])
        return pending

    return MemberDuesPayment.objects.create(
        member=member,
        request_month=target_month,
        amount_sats=due_amount,
        destination_onchain_address=destination_onchain,
        destination_lightning_address=destination_lightning,
        destination_blink_wallet_id=destination_wallet_id,
        requested_by_staff_at=requested_by_staff_at,
    )


def mark_due_request_by_staff(member: Member, actor) -> MemberDuesPayment:
    payment = ensure_member_due_payment(member)
    payment.requested_by_staff_at = timezone.now()
    payment.check_note = "회비지급요청 전송"
    payment.save(update_fields=["requested_by_staff_at", "check_note", "updated_at"])
    log_member_review(
        member=member,
        action=MemberReviewLog.ACTION_REQUEST_DUES,
        actor=actor,
        note="회비지급요청",
        acted_at=timezone.now(),
    )
    return payment


def select_member_due_payment_method(member: Member, method: str) -> MemberDuesPayment:
    if get_staff_requested_due_payment(member) is None:
        raise DuesPaymentServiceError("운영진의 회비지급요청 후에 결제를 진행할 수 있습니다.")

    payment = ensure_member_due_payment(member)
    method_value = str(method).strip()
    if method_value not in {MemberDuesPayment.METHOD_ONCHAIN, MemberDuesPayment.METHOD_LIGHTNING}:
        raise DuesPaymentServiceError("결제수단이 올바르지 않습니다.")

    now = timezone.now()
    payment.payment_method = method_value
    payment.last_checked_at = now

    if method_value == MemberDuesPayment.METHOD_ONCHAIN:
        payment.status = MemberDuesPayment.STATUS_WAITING_ONCHAIN
        payment.check_note = "온체인 TXID 입력 대기"
        payment.save(
            update_fields=[
                "payment_method",
                "status",
                "check_note",
                "last_checked_at",
                "updated_at",
            ]
        )
        return payment

    payment.status = MemberDuesPayment.STATUS_WAITING_LIGHTNING
    payment.check_note = "라이트닝 인보이스 생성 시도"
    payment.save(
        update_fields=[
            "payment_method",
            "status",
            "check_note",
            "last_checked_at",
            "updated_at",
        ]
    )

    collection_address = _resolve_collection_address()
    if collection_address is None:
        raise DuesPaymentServiceError("설정/주소등록에 블링크 주소를 먼저 등록해 주세요.")
    try:
        invoice, payment_hash = _issue_blink_invoice(payment, collection_address)
    except DuesPaymentServiceError as exc:
        payment.check_note = f"라이트닝 인보이스 생성 실패: {str(exc)[:160]}"
        payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        raise

    payment.lightning_invoice = invoice
    payment.lightning_invoice_hash = payment_hash
    payment.lightning_invoice_created_at = now
    payment.check_note = "라이트닝 결제 확인 대기"
    payment.save(
        update_fields=[
            "lightning_invoice",
            "lightning_invoice_hash",
            "lightning_invoice_created_at",
            "check_note",
            "last_checked_at",
            "updated_at",
        ]
    )
    return payment


def submit_member_due_onchain_txid(member: Member, txid: str) -> MemberDuesPayment:
    if get_staff_requested_due_payment(member) is None:
        raise DuesPaymentServiceError("운영진의 회비지급요청 후에 결제를 진행할 수 있습니다.")

    payment = ensure_member_due_payment(member)
    normalized_txid = str(txid).strip()
    if len(normalized_txid) < 24:
        raise DuesPaymentServiceError("유효한 TXID를 입력해 주세요.")

    payment.payment_method = MemberDuesPayment.METHOD_ONCHAIN
    payment.status = MemberDuesPayment.STATUS_WAITING_ONCHAIN
    payment.onchain_txid = normalized_txid
    payment.onchain_tx_submitted_at = timezone.now()
    payment.check_note = "온체인 TXID 확인 대기"
    payment.save(
        update_fields=[
            "payment_method",
            "status",
            "onchain_txid",
            "onchain_tx_submitted_at",
            "check_note",
            "updated_at",
        ]
    )
    return payment


def confirm_member_due_payment(member: Member, payment: MemberDuesPayment, *, actor=None) -> None:
    if payment.status != MemberDuesPayment.STATUS_PAID:
        return

    ensure_member_domain_records(member)
    settled_at = payment.paid_at or timezone.now()

    updated_member_fields: list[str] = []
    if member.status != Member.STATUS_ACTIVE:
        member.status = Member.STATUS_ACTIVE
        updated_member_fields.append("status")
    if updated_member_fields:
        member.save(update_fields=updated_member_fields)

    settlement = member.settlement
    if settlement.dues_paid_at != settled_at:
        settlement.dues_paid_at = settled_at
        settlement.save(update_fields=["dues_paid_at"])

    lifecycle = member.lifecycle
    lifecycle_updates: list[str] = []
    if lifecycle.signed_up_at != settled_at:
        lifecycle.signed_up_at = settled_at
        lifecycle_updates.append("signed_up_at")
    if lifecycle.withdrawn_at is not None:
        lifecycle.withdrawn_at = None
        lifecycle_updates.append("withdrawn_at")
    if lifecycle_updates:
        lifecycle.save(update_fields=lifecycle_updates)

    open_member_year_membership(member, reference_at=settled_at)

    has_confirm_log = member.review_logs.filter(
        action=MemberReviewLog.ACTION_CONFIRM_DUES,
        acted_at__gte=settled_at - timedelta(seconds=3),
        acted_at__lte=settled_at + timedelta(seconds=3),
    ).exists()
    if not has_confirm_log:
        log_member_review(
            member=member,
            action=MemberReviewLog.ACTION_CONFIRM_DUES,
            actor=actor,
            note=format_due_payment_review_note(payment),
            acted_at=settled_at,
        )

    actor_name = ""
    if actor is not None:
        actor_name = str(actor.get_username())
    send_member_status_share_messages(
        member,
        event_type=EVENT_MEMBER_DUES_CONFIRMED,
        actor_name=actor_name or "시스템 자동 확인",
        review_note="회비 입금 확인",
        dues_amount_sats=int(payment.amount_sats or 0),
        payment_note=format_due_payment_review_note(payment),
    )


def check_member_due_payment(member: Member) -> MemberDuesPayment | None:
    payment = (
        member.dues_payments.filter(
            status__in=PENDING_PAYMENT_STATUSES,
            requested_by_staff_at__isnull=False,
        )
        .order_by("-requested_by_staff_at", "-created_at", "-id")
        .first()
    )
    if payment is None:
        return None

    if payment.status == MemberDuesPayment.STATUS_WAITING_ONCHAIN:
        _check_onchain_tx_confirmed(payment)
    elif payment.status == MemberDuesPayment.STATUS_WAITING_LIGHTNING:
        collection_address = _resolve_collection_address()
        if collection_address is None:
            payment.check_note = "설정/주소등록 정보가 없어 라이트닝 확인을 진행할 수 없습니다."
            payment.last_checked_at = timezone.now()
            payment.save(update_fields=["check_note", "last_checked_at", "updated_at"])
        else:
            _check_lightning_invoice_paid(payment, collection_address)

    payment.refresh_from_db()
    if payment.status == MemberDuesPayment.STATUS_PAID:
        confirm_member_due_payment(member, payment, actor=None)
    return payment
