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
            self.shared = []
            self.code = None
            self.processing = False

        def addresses(self):
            return self.ips

        def status(self):
            return {"active": bool(self.code), "address": self.ips[0] if self.ips else "",
                    "paperIds": self.shared, "processing": self.processing}

        def start(self, address, ids, *, processing=False, inbox=False, review=False):
            if not ids and not processing and not inbox:
                raise ValueError("请选择要共享的论文。")
            if address not in self.ips:
                raise ValueError("请选择此电脑的局域网 IPv4 地址。")
            self.shared = list(ids)
            self.processing = processing
            _, ca, pin = mobile_lan._identity(address)
            self.code = {"v": 1, "url": "https://192.168.1.2:12345", "libraryId": str(uuid.uuid4()),
                         "token": "S" * 43, "pin": pin, "ca": ca}
            return self.code

        def stop(self):
            self.kept = self.code is not None or getattr(self, "kept", False)
            self.shared = []
            self.code = None
            self.processing = False

        def sidebar(self):
            return {"active": True, "papers": len(self.shared), "minutesAgo": 3} if self.code else {"active": False}

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


def test_desktop_selection_and_secret_lifetime(phone_controls, registry):
    web, controls = phone_controls
    pid = registry.create_paper(title="Selected work", status="done")
    other = registry.create_paper(title="Another work", status="done")
    registry.create_paper(title="Still pending", status="new")
    tok = _token(web)
    page = nodes(web.get("/settings?section=mobile").text)
    nav = page.all(lambda n: n.attrs.get("data-settings-link") == "mobile")[0]
    assert nav.attrs["aria-current"] == "page"
    mobile = page.all(lambda n: n.attrs.get("data-settings-panel") == "mobile")[0]
    assert "hidden" not in mobile.attrs
    options = mobile.all(lambda n: n.attrs.get("name") == "paper_id")
    assert {int(n.attrs["value"]) for n in options} == {pid, other}
    assert not any("checked" in n.attrs for n in options)
    for action in ("start", "show", "stop"):
        assert web.post("/settings/mobile/" + action, data={"csrf": "wrong"}).status_code == 403
    assert not controls.shared
    empty = web.post("/settings/mobile/start", headers=FRAGMENT, data={"csrf": tok, "address": controls.ips[0]})
    assert "请选择要共享的论文。" in empty.text and not controls.shared
    start = web.post("/settings/mobile/start", headers=FRAGMENT,
                     data={"csrf": tok, "address": controls.ips[0], "paper_id": [pid]})
    assert start.status_code == 200 and "no-store" in start.headers["cache-control"]
    assert controls.shared == [pid] and "Selected work" in start.text and "Another work" not in start.text
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
    assert "暂无已入库的论文" in page
    form = nodes(page).all(lambda n: n.attrs.get("action") == "/settings/mobile/start")[0]
    assert "disabled" in form.all(lambda n: n.tag == "button")[0].attrs


def test_processing_requires_explicit_consent_and_supports_import_into_empty_library(phone_controls):
    web, controls = phone_controls
    form = nodes(web.get("/settings/mobile/panel").text)
    option = form.all(lambda n: n.attrs.get("name") == "processing")[0]
    assert "checked" not in option.attrs
    start = web.post("/settings/mobile/start", headers=FRAGMENT,
                     data={"csrf": _token(web), "address": controls.ips[0], "processing": "true"})
    assert controls.processing and not controls.shared
    assert "本次配对已允许" in start.text
    assert web.post("/settings/mobile/stop", data={"csrf": _token(web)}, follow_redirects=False).status_code == 303
    assert not controls.processing


def test_pairing_qr_decodes_to_exact_displayed_payload(phone_controls, registry):
    # macOS already ships a barcode decoder. Rendering the actual SVG catches clipping,
    # encoding/escaping mistakes, low contrast, and oversized certificate payloads.
    vision = pytest.importorskip("Vision")
    from Foundation import NSData, NSDictionary

    web, controls = phone_controls
    pid = registry.create_paper(title="QR check", status="done")
    result = web.post("/settings/mobile/start", data={"csrf": _token(web), "address": controls.ips[0], "paper_id": pid})
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


def test_sharing_no_papers_says_what_the_phone_will_miss(phone_controls):
    web, controls = phone_controls
    tok = _token(web)
    form = web.get("/settings/mobile/panel", headers=FRAGMENT).text
    hint = nodes(form).all(lambda n: "data-mobile-none" in n.attrs)[0]
    assert "hidden" not in hint.attrs  # nothing is ticked yet
    lists_only = web.post("/settings/mobile/start", headers=FRAGMENT,
                          data={"csrf": tok, "address": controls.ips[0], "inbox": "true"})
    assert lists_only.status_code == 200 and controls.code
    assert "这次没有共享任何论文" in lists_only.text


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
