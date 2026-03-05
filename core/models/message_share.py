from __future__ import annotations

from django.db import models
from django.utils import timezone


class MessageShareSetting(models.Model):
    admin_email = models.TextField(blank=True, default="")
    gmail_sender_email = models.CharField(max_length=254, blank=True, default="")
    gmail_sender_name = models.CharField(max_length=120, blank=True, default="")
    gmail_app_password = models.CharField(max_length=255, blank=True, default="")
    discord_channel_id = models.TextField(blank=True, default="")
    send_member_pending_email = models.BooleanField(default=True)
    send_member_pending_discord = models.BooleanField(default=True)
    send_member_approved_email = models.BooleanField(default=True)
    send_member_approved_discord = models.BooleanField(default=True)
    send_member_dues_confirmed_email = models.BooleanField(default=True)
    send_member_dues_confirmed_discord = models.BooleanField(default=True)
    send_activity_created_email = models.BooleanField(default=True)
    send_activity_created_discord = models.BooleanField(default=True)
    send_settlement_requested_email = models.BooleanField(default=True)
    send_settlement_requested_discord = models.BooleanField(default=True)
    send_settlement_completed_email = models.BooleanField(default=True)
    send_settlement_completed_discord = models.BooleanField(default=True)
    recruiting_message_html = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "메시지 공유 설정"
        verbose_name_plural = "메시지 공유 설정"

    def __str__(self) -> str:
        return "메시지 공유 설정"


def get_message_share_setting() -> MessageShareSetting:
    setting = MessageShareSetting.objects.order_by("-id").first()
    if setting is None:
        setting = MessageShareSetting.objects.create()
    return setting
