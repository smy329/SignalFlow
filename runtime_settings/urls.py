from django.urls import path

from runtime_settings import views

urlpatterns = [
    path("settings/", views.settings_page, name="settings"),  # redirects to the first group
    path("settings/reset/", views.reset_field, name="settings-reset"),
    path("settings/<slug:slug>/save/", views.save_group, name="settings-save"),
    path("settings/<slug:slug>/", views.group_page, name="settings-group"),
]
