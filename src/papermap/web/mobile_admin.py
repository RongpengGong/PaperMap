"""Loopback-only controls for the separate, opt-in phone listener."""
import base64
import io
import json

from fastapi import Form, Request
from fastapi.responses import RedirectResponse


def _pairing_image(text):
    import qrcode
    from qrcode.image.svg import SvgPathImage

    out = io.BytesIO()
    qrcode.make(text, image_factory=SvgPathImage, box_size=5, border=4).save(out)
    return "data:image/svg+xml;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def mount_mobile(app, mobile, registry, render, csrf_token, check_csrf, groups):
    def context(pairing=None, error="", selected_address="", selected_ids=()):
        rows = registry.query("SELECT id,title FROM papers WHERE status='done' ORDER BY title COLLATE NOCASE, id")
        code = json.dumps(pairing, separators=(",", ":")) if pairing else ""
        return dict(lan=mobile.status(), addresses=mobile.addresses(), papers=rows, code=code,
                    qr=_pairing_image(code) if code else "", error=error,
                    selected_address=selected_address, selected_ids=selected_ids)

    def page(request, **kwargs):
        fragment = request.headers.get("x-papermap-panel") == "mobile"
        template = "_mobile_sync.html" if fragment else "mobile_sync.html"
        response = render(request, template, settings_section="mobile", settings_groups=groups, **context(**kwargs))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/settings/mobile/panel")
    def panel(request: Request):
        return page(request)

    @app.post("/settings/mobile/start")
    def start(request: Request, csrf: str = Form(...), address: str = Form(""),
              paper_id: list[int] = Form(default=[])):
        check_csrf(csrf, csrf_token)
        try:
            return page(request, pairing=mobile.start(address, paper_id))
        except (OSError, LookupError, ValueError, RuntimeError) as exc:
            return page(request, error=str(exc), selected_address=address, selected_ids=paper_id)

    @app.post("/settings/mobile/show")
    def show(request: Request, csrf: str = Form(...)):
        check_csrf(csrf, csrf_token)
        try:
            return page(request, pairing=mobile.pairing())
        except ValueError as exc:
            return page(request, error=str(exc))

    @app.post("/settings/mobile/stop")
    def stop(request: Request, csrf: str = Form(...)):
        check_csrf(csrf, csrf_token)
        mobile.stop()
        if request.headers.get("x-papermap-panel") == "mobile":
            return page(request)
        return RedirectResponse("/settings?section=mobile", status_code=303)

    return context
