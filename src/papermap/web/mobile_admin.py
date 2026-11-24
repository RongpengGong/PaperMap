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
    def context(pairing=None, error="", selected_address=None, selected_ids=None, processing=None, inbox=None,
                review=None):
        last = mobile.remembered() if hasattr(mobile, "remembered") else {}
        if selected_ids is None:
            selected_ids = last.get("paperIds", [])
        if selected_address is None:
            selected_address = last.get("address", "")
        if processing is None:
            processing = last.get("processing", False)
        if inbox is None:
            inbox = last.get("inbox", True)
        if review is None:
            review = last.get("review", False)
        since = last.get("at", "")
        rows = registry.query("""SELECT id,title,created_at FROM papers WHERE status='done'
            OR (status IN ('new','extracting','error') AND kb_path IS NOT NULL)
            ORDER BY title COLLATE NOCASE, id""")
        code = json.dumps(pairing, separators=(",", ":")) if pairing else ""
        from .. import mobile_backup

        backup = mobile_backup.summary(mobile.config) if hasattr(mobile, "config") else {"count": 0}
        return dict(lan=mobile.status(), addresses=mobile.addresses(), backup=backup, papers=rows, code=code,
                    qr=_pairing_image(code) if code else "", error=error,
                    selected_address=selected_address, selected_ids=selected_ids, processing=processing, inbox=inbox,
                    review=review, remembered=bool(last), paired=mobile.paired() if hasattr(mobile, "paired") else False,
                    new_ids={r["id"] for r in rows if since and (r["created_at"] or "") > since})

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
              paper_id: list[int] = Form(default=[]), processing: bool = Form(False),
              inbox: bool = Form(False), review: bool = Form(False)):
        check_csrf(csrf, csrf_token)
        try:
            return page(request, pairing=mobile.start(address, paper_id, processing=processing, inbox=inbox,
                                                      review=review))
        except (OSError, LookupError, ValueError, RuntimeError) as exc:
            return page(request, error=str(exc), selected_address=address, selected_ids=paper_id, processing=processing,
                        inbox=inbox, review=review)

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

    @app.post("/settings/mobile/revoke")
    def revoke(request: Request, csrf: str = Form(...)):
        check_csrf(csrf, csrf_token)
        mobile.revoke()
        if request.headers.get("x-papermap-panel") == "mobile":
            return page(request)
        return RedirectResponse("/settings?section=mobile", status_code=303)

    return context
