from django import template

from runtime_settings import registry

register = template.Library()


@register.simple_tag
def settings_groups():
    """(name, slug) of every settings group, in page order, for the sidebar."""
    return [(g, registry.slug_of(g)) for g in registry.GROUPS]
