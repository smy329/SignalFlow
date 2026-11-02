"""Messages page: everything received from the sources of one mode (Real or Forward)."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render

from runtime_settings import service as runtime
from telegram_monitor.services import message_service, message_status
from trading.scopes import SCOPES


@login_required
def messages_page(request, scope: str):
    if scope not in SCOPES:
        raise Http404
    sc = SCOPES[scope]
    page = Paginator(message_service.messages_for_mode(sc.source_mode), runtime.get("display.messages_page_size")).get_page(request.GET.get("page"))
    rows = []
    for m in page.object_list:
        status, reason = message_status.describe(m.parse_status, m.error_code)
        rows.append({"message": m, "status": status, "reason": reason, "is_signal": status == message_status.SIGNAL})
    context = {"scope": sc, "page": page, "rows": rows, "nav_scope": sc.slug, "nav_item": "messages"}
    return render(request, "trading/messages.html", context)
