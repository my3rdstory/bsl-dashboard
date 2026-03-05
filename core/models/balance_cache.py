from django.db import models
from django.utils import timezone


class CollectionBalanceCache(models.Model):
    key = models.CharField(max_length=32, unique=True)
    source_owner_display_name = models.CharField(max_length=80, blank=True, default="")
    source_onchain_address = models.CharField(max_length=100, blank=True, default="")
    source_blink_wallet_id = models.CharField(max_length=120, blank=True, default="")
    onchain_sats = models.BigIntegerField(null=True, blank=True)
    lightning_sats = models.BigIntegerField(null=True, blank=True)
    total_sats = models.BigIntegerField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "수금 잔액 캐시"
        verbose_name_plural = "수금 잔액 캐시"

    def __str__(self) -> str:
        return self.key
