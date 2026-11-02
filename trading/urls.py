from django.urls import path

from trading import views, views_manual, views_messages, views_sources
from trading.scopes import SCOPES

urlpatterns = [
    path("sources/<int:pk>/toggle/", views_sources.source_toggle, name="source-toggle"),
    path("sources/<int:pk>/settings/", views_sources.source_settings, name="source-settings"),
    path("sources/<int:pk>/delete/", views_sources.source_delete, name="source-delete"),
]

# Manual Trade: place a trade from pasted signal text, and the manual trades of both worlds.
urlpatterns += [
    path("manual/", views_manual.place_page, name="manual-place"),
    path("manual/analyze/", views_manual.analyze, name="manual-analyze"),
    path("manual/preview/", views_manual.preview, name="manual-preview"),
    path("manual/place/", views_manual.place, name="manual-submit"),
    path("manual/result/<int:signal_id>/", views_manual.result, name="manual-result"),
    path("manual/trades/", views_manual.trades_redirect, name="manual-trades"),
    *[path(f"manual/trades/{tab}/", views_manual.trades_page, {"tab": tab}, name=f"manual-trades-{tab}") for tab in views.TRADE_TABS],
    path("manual/history/", views_manual.history_page, name="manual-history"),
    path("api/manual/positions/", views_manual.positions_api, name="manual-api-positions"),
    path("api/manual/pending/", views_manual.pending_api, name="manual-api-pending"),
    path("api/manual/cancelled/", views_manual.cancelled_api, name="manual-api-cancelled"),
    path("api/manual/history/", views_manual.history_api, name="manual-api-history"),
    path("api/manual/positions/<int:trade_id>/close/", views_manual.close_api, name="manual-api-close"),
    path("api/manual/pending/<int:trade_id>/trade-now/", views_manual.trade_now_api, name="manual-api-trade-now"),
]

# /real/... and /forward/...: identical pages, each bound to its own source list and trade mode.
for slug in SCOPES:
    urlpatterns += [
        path(f"{slug}/switch/", views_sources.switch_set, {"scope": slug}, name=f"{slug}-switch"),
        path(f"{slug}/sources/", views_sources.sources_page, {"scope": slug}, name=f"{slug}-sources"),
        path(f"{slug}/sources/add/", views_sources.source_create, {"scope": slug}, name=f"{slug}-source-create"),
        path(f"{slug}/trades/", views.trades_redirect, {"scope": slug}, name=f"{slug}-trades"),
        *[
            path(f"{slug}/trades/{tab}/", views.trades_page, {"scope": slug, "tab": tab}, name=f"{slug}-trades-{tab}")
            for tab in views.TRADE_TABS
        ],
        path(f"{slug}/messages/", views_messages.messages_page, {"scope": slug}, name=f"{slug}-messages"),
        path(f"{slug}/history/", views.history_page, {"scope": slug}, name=f"{slug}-history"),
        path(f"api/{slug}/positions/", views.positions_api, {"scope": slug}, name=f"{slug}-api-positions"),
        path(f"api/{slug}/positions/<int:trade_id>/close/", views.close_api, {"scope": slug}, name=f"{slug}-api-close"),
        path(f"api/{slug}/pending/<int:trade_id>/trade-now/", views.trade_now_api, {"scope": slug}, name=f"{slug}-api-trade-now"),
        path(f"api/{slug}/pending/", views.pending_api, {"scope": slug}, name=f"{slug}-api-pending"),
        path(f"api/{slug}/cancelled/", views.cancelled_api, {"scope": slug}, name=f"{slug}-api-cancelled"),
        path(f"api/{slug}/history/", views.history_api, {"scope": slug}, name=f"{slug}-api-history"),
    ]
