from django.contrib import admin
from django.urls import include, path

from core.views import admin_login_redirect, superadmin_login

urlpatterns = [
    path("back-office/admin/", admin.site.urls),
    path("superadmin/login/", superadmin_login, name="superadmin-login"),
    path("accounts/login/", admin_login_redirect, name="login"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
