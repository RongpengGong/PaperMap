import base64
import json
import uuid

import pymupdf
import pytest

from papermap import mobile_lan
from .test_settings_sections import nodes
from .test_web import _token, client

FRAGMENT = {"X-PaperMap-Panel": "mobile"}


@pytest.fixture
def phone_controls(config, registry, monkeypatch):
    """Exercise the real admin page and QR encoder without opening a LAN port."""
    class LocalControls:
        ips = ["192.168.1.2"]

        def __init__(self, config, registry):
            self.config = config
            self.shared = False
            self.code = None
            self.processing = False

        def addresses(self):
            return self.ips

        def status(self):
            return {"active": bool(self.code), "address": self.ips[0] if self.ips else "",
                    "papers": 2 if self.shared else 0, "processing": self.processing}

        def start(self, address, *, processing=False, inbox=False, review=False, web=False):
            self.web = web
            if address not in self.ips:
                raise ValueError("请选择此电脑的局域网 IPv4 地址。")
            self.shared = True
            self.processing = processing
            _, ca, pin = mobile_lan._identity(address)
            self.code = {"v": 1, "url": "https://192.168.1.2:12345", "libraryId": str(uuid.uuid4()),
                         "token": "S" * 43, "pin": pin, "ca": ca}
            return self.code

        def stop(self):
            self.kept = self.code is not None or getattr(self, "kept", False)
            self.shared = False
            self.code = None
            self.processing = False

        def sidebar(self):
            return {"active": True, "papers": 2, "minutesAgo": 3} if self.code else {"active": False}

        def paired(self):
            return self.code is not None or getattr(self, "kept", False)

        def revoke(self):
            self.stop()
            self.kept = False

        def pairing(self):
            if not self.code:
                raise ValueError("请先开启局域网共享。")
            return self.code

    controls = LocalControls(config, registry)
    monkeypatch.setattr(mobile_lan, "MobileLan", lambda *args: controls)
    return client(config, registry), controls


def test_the_whole_library_is_shared_and_the_secret_lifetime(phone_controls, registry):
    web, controls = phone_controls
    registry.create_paper(title="Selected work", status="done")
    registry.create_paper(title="Another work", status="done")
    registry.create_paper(title="Still pending", status="new")  # no card yet: not offered
    tok = _token(web)
    page = nodes(web.get("/settings?section=mobile").text)
    nav = page.all(lambda n: n.attrs.get("data-settings-link") == "mobile")[0]
    assert nav.attrs["aria-current"] == "page"
    mobile = page.all(lambda n: n.attrs.get("data-settings-panel") == "mobile")[0]
    assert "hidden" not in mobile.attrs
    assert not mobile.all(lambda n: n.attrs.get("name") == "paper_id")  # the phone picks, not the computer
    assert "共享整个文献库（现在 2 篇" in mobile.text()
    for action in ("start", "show", "stop"):
        assert web.post("/settings/mobile/" + action, data={"csrf": "wrong"}).status_code == 403
    assert not controls.shared
    start = web.post("/settings/mobile/start", headers=FRAGMENT, data={"csrf": tok, "address": controls.ips[0]})
    assert start.status_code == 200 and "no-store" in start.headers["cache-control"]
    assert controls.shared and "整个文献库" in start.text and "「我的笔记」（手机上只读）" in start.text
    assert "Selected work" not in start.text  # no title list on the share page
    qr = nodes(start.text).all(lambda n: n.tag == "textarea")[0].text()
    assert json.loads(qr) == controls.code
    assert "data:image/svg+xml;base64" in start.text and "<aside" not in start.text
    assert controls.code["token"] not in web.get("/settings?section=mobile").text
    assert controls.code["token"] not in web.get("/settings/mobile/panel", headers=FRAGMENT).text
    assert controls.code["token"] in web.post("/settings/mobile/show", data={"csrf": tok}).text
    assert web.post("/settings/mobile/stop", data={"csrf": tok}, follow_redirects=False).status_code == 303
    assert not controls.shared
    assert "请先开启局域网共享。" in web.post("/settings/mobile/show", data={"csrf": tok}, headers=FRAGMENT).text


def test_empty_library_and_missing_network(phone_controls):
    web, controls = phone_controls
    controls.ips = []
    page = web.get("/settings?section=mobile").text
    assert "尚未找到局域网地址，请连接 Wi-Fi 后刷新。" in page
    assert "共享整个文献库（现在 0 篇" in page
    form = nodes(page).all(lambda n: n.attrs.get("action") == "/settings/mobile/start")[0]
    assert "disabled" in form.all(lambda n: n.tag == "button")[0].attrs


def test_processing_requires_explicit_consent_and_supports_import_into_empty_library(phone_controls):
    web, controls = phone_controls
    form = nodes(web.get("/settings/mobile/panel").text)
    option = form.all(lambda n: n.attrs.get("name") == "processing")[0]
    assert "checked" not in option.attrs
    start = web.post("/settings/mobile/start", headers=FRAGMENT,
                     data={"csrf": _token(web), "address": controls.ips[0], "processing": "true"})
    assert controls.processing and controls.shared
    assert "本次配对已允许" in start.text
    assert web.post("/settings/mobile/stop", data={"csrf": _token(web)}, follow_redirects=False).status_code == 303
    assert not controls.processing


def test_full_library_access_for_the_phone_is_a_separate_unticked_choice(phone_controls):
    web, controls = phone_controls
    form = nodes(web.get("/settings/mobile/panel").text)
    option = form.all(lambda n: n.attrs.get("name") == "web")[0]
    assert "checked" not in option.attrs
    web.post("/settings/mobile/start", headers=FRAGMENT, data={"csrf": _token(web), "address": controls.ips[0]})
    assert controls.web is False
    web.post("/settings/mobile/start", headers=FRAGMENT,
             data={"csrf": _token(web), "address": controls.ips[0], "web": "true"})
    assert controls.web is True


def test_pairing_qr_decodes_to_exact_displayed_payload(phone_controls, registry):
    # macOS already ships a barcode decoder. Rendering the actual SVG catches clipping,
    # encoding/escaping mistakes, low contrast, and oversized certificate payloads.
    vision = pytest.importorskip("Vision")
    from Foundation import NSData, NSDictionary

    web, controls = phone_controls
    registry.create_paper(title="QR check", status="done")
    result = web.post("/settings/mobile/start", data={"csrf": _token(web), "address": controls.ips[0]})
    doc = nodes(result.text)
    qr = doc.all(lambda n: n.tag == "img" and n.attrs.get("alt") == "手机配对二维码")[0]
    text = doc.all(lambda n: n.tag == "textarea" and "readonly" in n.attrs)[0].text()
    svg = base64.b64decode(qr.attrs["src"].split(",")[1])
    with pymupdf.open(stream=svg, filetype="svg") as image:
        pixels = image[0].get_pixmap(matrix=pymupdf.Matrix(5, 5)).tobytes("png")
    request = vision.VNDetectBarcodesRequest.alloc().init()
    request.setSymbologies_([vision.VNBarcodeSymbologyQR])
    handler = vision.VNImageRequestHandler.alloc().initWithData_options_(
        NSData.dataWithBytes_length_(pixels, len(pixels)), NSDictionary.dictionary())
    ok, _ = handler.performRequests_error_([request], None)
    assert ok
    assert [str(item.payloadStringValue()) for item in request.results()] == [text]
    assert json.loads(text) == controls.code


def test_old_paper_choices_in_a_form_post_are_ignored(phone_controls):
    web, controls = phone_controls
    shared = web.post("/settings/mobile/start", headers=FRAGMENT,
                      data={"csrf": _token(web), "address": controls.ips[0], "paper_id": ["1"], "inbox": "true"})
    assert shared.status_code == 200 and controls.code and controls.shared


def test_a_kept_pairing_can_be_revoked(phone_controls):
    web, controls = phone_controls
    tok = _token(web)
    assert "/settings/mobile/revoke" not in web.get("/settings/mobile/panel", headers=FRAGMENT).text
    web.post("/settings/mobile/start", headers=FRAGMENT, data={"csrf": tok, "address": controls.ips[0], "inbox": "true"})
    stopped = web.post("/settings/mobile/stop", headers=FRAGMENT, data={"csrf": tok}).text
    assert "/settings/mobile/revoke" in stopped  # closed, but the phone's pairing still works next time
    assert web.post("/settings/mobile/revoke", data={"csrf": "wrong"}).status_code == 403
    revoked = web.post("/settings/mobile/revoke", headers=FRAGMENT, data={"csrf": tok}).text
    assert "/settings/mobile/revoke" not in revoked and not controls.paired()


def test_sidebar_entry_shows_the_phone_state(phone_controls):
    web, controls = phone_controls
    tok = _token(web)
    link = nodes(web.get("/events").text).all(lambda n: "phone-link" in n.attrs.get("class", ""))[0]
    assert link.attrs["href"] == "/settings?section=mobile" and "未共享" in link.text()
    web.post("/settings/mobile/start", headers=FRAGMENT, data={"csrf": tok, "address": controls.ips[0], "inbox": "true"})
    link = nodes(web.get("/events").text).all(lambda n: "phone-link" in n.attrs.get("class", ""))[0]
    assert "3 分钟前" in " ".join(link.text().split())
