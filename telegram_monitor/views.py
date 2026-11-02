import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from runtime_settings import service as runtime
from telegram_monitor.models import MANUAL_TELEGRAM_ID, ParsedSignal, TelegramSource
from telegram_monitor.serializers import ParsedSignalSerializer
from telegram_monitor.services import parser_playground


@login_required
def dashboard(request):
    sources = TelegramSource.objects.exclude(telegram_id=MANUAL_TELEGRAM_ID)
    signals = ParsedSignal.objects.exclude(source__telegram_id=MANUAL_TELEGRAM_ID)  # manual trades have their own pages
    recent_signals = signals.select_related("source", "telegram_message")[: runtime.get("display.dashboard_recent_signals")]
    return render(
        request,
        "telegram_monitor/dashboard.html",
        {
            "sources": sources,
            "recent_signals": recent_signals,
            "recent_signals_json": ParsedSignalSerializer(recent_signals, many=True).data,
            "signal_count": signals.count(),
        },
    )


@login_required
def parser_playground_page(request):
    return render(request, "telegram_monitor/parser_playground.html", {"max_length": parser_playground.max_text_length()})


@login_required
@require_POST
def parser_playground_parse(request):
    """Dry-run only: nothing is stored, broadcast or traded."""
    try:
        text = json.loads(request.body or b"{}").get("text", "")
    except (ValueError, AttributeError):
        return JsonResponse({"detail": "Send JSON like {\"text\": \"...\"}."}, status=400)
    if not isinstance(text, str):
        return JsonResponse({"detail": "text must be a string."}, status=400)
    limit = parser_playground.max_text_length()
    if len(text) > limit:
        return JsonResponse({"detail": f"Message is longer than {limit} characters."}, status=400)
    return JsonResponse(parser_playground.preview(text))
