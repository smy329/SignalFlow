from rest_framework.routers import DefaultRouter

from telegram_monitor.api_views import ParsedSignalViewSet, TelegramMessageViewSet, TelegramSourceViewSet

router = DefaultRouter()
router.register("sources", TelegramSourceViewSet, basename="source")
router.register("messages", TelegramMessageViewSet, basename="message")
router.register("signals", ParsedSignalViewSet, basename="signal")

urlpatterns = router.urls
