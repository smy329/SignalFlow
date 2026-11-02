from django.urls import path

from telegram_monitor import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("parser-playground/", views.parser_playground_page, name="parser-playground"),
    path("parser-playground/parse/", views.parser_playground_parse, name="parser-playground-parse"),
]
