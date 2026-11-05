"""Settings > Secondary Server routes (fork-only feature)."""

import logging
from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import ImmutableMultiDict

from web.config import templates
from web.dependencies import parse_form
from web.services import secondary_server_service as svc

logger = logging.getLogger(__name__)

router = APIRouter()


def _alert(request: Request, kind: str, message: str):
    return templates.TemplateResponse(
        request, "partials/alert.html", {"type": kind, "message": message}
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def secondary_page(request: Request):
    return templates.TemplateResponse(
        request,
        "settings/secondary.html",
        {
            "page_title": "Secondary Server Settings",
            "active_tab": "secondary",
            "settings": svc.get_secondary_settings(),
        },
    )


@router.put("", response_class=HTMLResponse)
def save_secondary(request: Request, form: ImmutableMultiDict = Depends(parse_form)):
    ok, message = svc.save_connection(
        enabled=form.get("enabled") == "on",
        name=form.get("name", ""),
        plex_url=form.get("plex_url", ""),
        plex_token=form.get("plex_token", ""),
        primary_name=form.get("primary_name", ""),
    )
    return _alert(request, "success" if ok else "error", message)


@router.post("/test", response_class=HTMLResponse)
def test_secondary(form: ImmutableMultiDict = Depends(parse_form)):
    ok, message = svc.test_connection(form.get("plex_url", "").strip(), form.get("plex_token", "").strip())
    badge = "badge-success" if ok else "badge-error"
    return HTMLResponse(f'<span class="badge {badge}">{escape(message)}</span>')


@router.get("/users", response_class=HTMLResponse)
def secondary_users(request: Request):
    return templates.TemplateResponse(
        request,
        "settings/partials/secondary_users_table.html",
        {"settings": svc.get_secondary_settings(), "message": None},
    )


@router.post("/users/sync", response_class=HTMLResponse)
def sync_secondary_users(request: Request):
    result = svc.sync_users()
    if not result["success"]:
        return _alert(request, "error", f"Sync failed: {result['error']}")
    message = f"Synced {len(result['users'])} users"
    if result["added_count"]:
        message += f" (+{result['added_count']} new)"
    if result["removed_count"]:
        message += f" (-{result['removed_count']} removed)"
    return templates.TemplateResponse(
        request,
        "settings/partials/secondary_users_table.html",
        {"settings": svc.get_secondary_settings(), "message": message},
    )


@router.put("/users", response_class=HTMLResponse)
def save_secondary_users(request: Request, form: ImmutableMultiDict = Depends(parse_form)):
    ok = svc.save_user_preferences(dict(form))
    return _alert(request, "success" if ok else "error",
                  "Secondary users saved" if ok else "Failed to save settings")
