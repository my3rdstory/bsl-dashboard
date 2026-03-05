from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone


def health_fragment(request: HttpRequest) -> HttpResponse:
    now = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
    return render(request, "core/partials/system_status.html", {"checked_at": now})
