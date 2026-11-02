from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="telegram_monitor/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("runtime_settings.urls")),
    path("", include("trading.urls")),
    path("api/", include("telegram_monitor.urls_api")),
    path("", include("telegram_monitor.urls")),
]
