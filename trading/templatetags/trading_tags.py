from django import template

from trading.scopes import SCOPES
from trading.services import switch_service

register = template.Library()


@register.inclusion_tag("trading/_switch.html", takes_context=True)
def trading_switch(context, slug: str, next_url: str = ""):
    """ON/OFF control for Real or Forward trades (used by the dashboard and the Sources pages)."""
    scope = SCOPES[slug]
    return {"scope": scope, "next_url": next_url, "csrf_token": context.get("csrf_token"), **switch_service.state(scope)}
