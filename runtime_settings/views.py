"""The Settings pages: one page per group, saved group by group."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from runtime_settings import registry, service
from runtime_settings.service import SettingError


def _field(defn, value, raw=None, error=None) -> dict:
    return {
        "def": defn,
        "name": defn.key,
        "value": raw if raw is not None else service.form_value(defn, value),
        "is_default": service.is_default(defn.key),
        "default_text": ("On" if defn.default else "Off")
        if defn.kind == registry.BOOL
        else str(service.form_value(defn, defn.default)).replace("\n", ", "),
        "error": error,
    }


def _group_of(slug: str) -> str:
    group = next((g for g in registry.GROUPS if registry.slug_of(g) == slug), None)
    if group is None:
        raise Http404("Unknown settings group")
    return group


def _build_group(group: str, raw_by_key=None, errors=None) -> dict:
    raw_by_key, errors = raw_by_key or {}, errors or {}
    defs = dict(registry.by_group())[group]
    fields = [_field(d, service.get(d.key), raw_by_key.get(d.key), errors.get(d.key)) for d in defs]
    return {"name": group, "slug": registry.slug_of(group), "help": registry.GROUP_HELP.get(group, ""), "fields": fields}


def _render(request, group: dict):
    return render(request, "runtime_settings/settings.html",
                  {"group": group, "nav_scope": "settings", "nav_item": group["slug"]})


@login_required
def settings_page(request):
    """/settings/ has no page of its own: it opens the first group."""
    return redirect("settings-group", slug=registry.slug_of(registry.GROUPS[0]))


@login_required
def group_page(request, slug: str):
    return _render(request, _build_group(_group_of(slug)))


@login_required
@require_POST
def save_group(request, slug: str):
    """Saves every editable field of one group, or none of them when any value is invalid."""
    group = _group_of(slug)

    updates, raw_by_key, errors = {}, {}, {}
    for defn in (d for d in registry.DEFINITIONS if d.group == group and d.editable):
        if defn.kind == registry.BOOL:
            raw = request.POST.getlist(defn.key)[-1] if request.POST.getlist(defn.key) else "0"
        else:
            raw = request.POST.get(defn.key, "")
        raw_by_key[defn.key] = raw
        try:
            service.parse(defn, raw)
            updates[defn.key] = raw
        except SettingError as exc:
            errors[defn.key] = str(exc)

    if errors:
        messages.error(request, f"{group}: nothing was saved. Fix the highlighted fields.")
        return _render(request, _build_group(group, raw_by_key, errors))

    service.save_many(updates)
    messages.success(request, f"{group} saved. The change is applied within a few seconds; open trades are untouched.")
    return redirect("settings-group", slug=slug)


@login_required
@require_POST
def reset_field(request):
    key = request.POST.get("key", "")
    defn = registry.BY_KEY.get(key)
    if defn is None or not defn.editable:
        messages.error(request, "Unknown setting.")
        return redirect("settings")
    service.reset(key)
    messages.success(request, f"{defn.label} reset to its default.")
    return redirect("settings-group", slug=registry.slug_of(defn.group))
