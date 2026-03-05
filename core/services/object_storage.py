from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify


class ObjectStorageError(Exception):
    pass


@dataclass(frozen=True)
class ObjectUploadResult:
    object_key: str
    stored_filename: str
    content_type: str
    file_size_bytes: int


@dataclass(frozen=True)
class ObjectStorageConfig:
    endpoint_url: str
    bucket: str
    access_key: str
    secret_key: str
    signature_version: str
    download_expires_seconds: int


@dataclass(frozen=True)
class ObjectBytesResult:
    body: bytes
    content_type: str


def _load_config() -> ObjectStorageConfig:
    endpoint_url = str(getattr(settings, "IWINV_OBJECT_STORAGE_ENDPOINT", "")).strip()
    bucket = str(getattr(settings, "IWINV_OBJECT_STORAGE_BUCKET", "")).strip()
    access_key = str(getattr(settings, "IWINV_OBJECT_STORAGE_ACCESS_KEY", "")).strip()
    secret_key = str(getattr(settings, "IWINV_OBJECT_STORAGE_SECRET_KEY", "")).strip()
    signature_version = str(
        getattr(settings, "IWINV_OBJECT_STORAGE_SIGNATURE_VERSION", "s3")
    ).strip()
    expires = int(getattr(settings, "IWINV_OBJECT_STORAGE_DOWNLOAD_EXPIRES", 600))

    return ObjectStorageConfig(
        endpoint_url=endpoint_url.rstrip("/"),
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        signature_version=signature_version or "s3",
        download_expires_seconds=expires,
    )


def is_object_storage_configured() -> bool:
    config = _load_config()
    return bool(
        config.endpoint_url
        and config.bucket
        and config.access_key
        and config.secret_key
    )


def _build_s3_client():
    config = _load_config()
    if not is_object_storage_configured():
        raise ObjectStorageError("오브젝트 스토리지 환경변수가 설정되지 않았습니다.")

    try:
        import boto3
        from botocore.config import Config
    except Exception as exc:  # pragma: no cover - dependency runtime guard
        raise ObjectStorageError("boto3 의존성을 불러오지 못했습니다.") from exc

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        config=Config(
            signature_version=config.signature_version,
            s3={"addressing_style": "path"},
        ),
    )


def _build_object_key(member_id: int, original_name: str) -> str:
    now = timezone.localtime()
    suffix = Path(original_name or "file").suffix.lower()
    stem = Path(original_name or "file").stem
    stem_slug = slugify(stem)[:40] or "file"
    random_key = uuid4().hex
    return (
        f"activities/{now:%Y/%m}/{int(member_id)}/"
        f"{now:%Y%m%d%H%M%S}_{stem_slug}_{random_key}{suffix}"
    )


def _try_convert_image_to_webp(uploaded_file) -> tuple[bytes, str] | None:
    content_type = str(getattr(uploaded_file, "content_type", "") or "").strip().lower()
    if not content_type.startswith("image/"):
        return None
    if content_type == "image/svg+xml":
        return None

    try:
        from PIL import Image
    except Exception:
        return None

    try:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        with Image.open(uploaded_file) as image:
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = BytesIO()
            image.save(
                output,
                format="WEBP",
                optimize=True,
                quality=82,
                method=6,
            )
        return output.getvalue(), "image/webp"
    except Exception:
        return None
    finally:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)


def upload_activity_attachment(uploaded_file, member_id: int) -> ObjectUploadResult:
    config = _load_config()
    client = _build_s3_client()
    original_name = str(getattr(uploaded_file, "name", "") or "file")
    stored_filename = original_name
    content_type = str(getattr(uploaded_file, "content_type", "") or "").strip()
    if not content_type:
        content_type = "application/octet-stream"
    file_size_bytes = int(getattr(uploaded_file, "size", 0) or 0)

    converted = _try_convert_image_to_webp(uploaded_file)
    body_bytes: bytes | None = None
    if converted is not None:
        body_bytes, content_type = converted
        file_size_bytes = len(body_bytes)
        stored_filename = f"{Path(original_name).stem}.webp"

    object_key = _build_object_key(member_id=member_id, original_name=stored_filename)

    try:
        if body_bytes is not None:
            client.put_object(
                Bucket=config.bucket,
                Key=object_key,
                Body=body_bytes,
                ContentType=content_type,
            )
        else:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            client.upload_fileobj(
                uploaded_file,
                config.bucket,
                object_key,
                ExtraArgs={"ContentType": content_type},
            )
    except Exception as exc:  # pragma: no cover - network/service errors
        raise ObjectStorageError("첨부파일 업로드에 실패했습니다.") from exc
    return ObjectUploadResult(
        object_key=object_key,
        stored_filename=stored_filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
    )


def delete_object(object_key: str) -> None:
    config = _load_config()
    client = _build_s3_client()
    try:
        client.delete_object(Bucket=config.bucket, Key=object_key)
    except Exception:
        return


def build_attachment_download_url(object_key: str) -> str:
    config = _load_config()
    client = _build_s3_client()
    try:
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": config.bucket, "Key": object_key},
                ExpiresIn=config.download_expires_seconds,
            )
        )
    except Exception as exc:  # pragma: no cover - network/service errors
        raise ObjectStorageError("첨부파일 다운로드 URL 생성에 실패했습니다.") from exc


def download_object_bytes(object_key: str) -> ObjectBytesResult:
    config = _load_config()
    client = _build_s3_client()
    try:
        response = client.get_object(Bucket=config.bucket, Key=object_key)
        body_stream = response.get("Body")
        body_bytes = body_stream.read() if body_stream is not None else b""
        content_type = str(response.get("ContentType", "") or "").strip()
        if not content_type:
            content_type = "application/octet-stream"
        return ObjectBytesResult(body=body_bytes, content_type=content_type)
    except Exception as exc:  # pragma: no cover - network/service errors
        raise ObjectStorageError("첨부파일 미리보기 로드에 실패했습니다.") from exc
