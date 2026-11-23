"""Keep the embedded PDF and the library's top-level navigation independent."""
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from .conftest import OWN_DOI
from .test_reader import client


APP_JS = Path(__file__).parents[1] / "src" / "papermap" / "web" / "static" / "app.js"


def run_js(script, *args):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for browser-script navigation tests")
    result = subprocess.run([node, "-e", script, *args], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def reader(config, registry, pdfs):
    browser, _, _ = client(config, registry, pdfs)
    return browser, registry.paper_by_doi(OWN_DOI)["id"]


def test_document_navigation_to_embedded_url_opens_standalone(reader):
    browser, pid = reader
    path = f"/reader/{pid}"
    params = {"embed": 1, "page": 2, "q": "gene expression"}
    response = browser.get(path, params=params, headers={"Sec-Fetch-Dest": "document"}, follow_redirects=False)
    assert response.status_code == 307
    target = urlsplit(response.headers["location"])
    assert target.path == path
    assert parse_qs(target.query) == {"page": ["2"], "q": ["gene expression"]}
    full = browser.get(response.headers["location"])
    assert 'class="sidebar"' in full.text and f'href="/?id={pid}"' in full.text

    frame = browser.get(path, params=params, headers={"Sec-Fetch-Dest": "iframe"}, follow_redirects=False)
    assert frame.status_code == 200 and 'class="bare"' in frame.text


def test_reader_with_no_fetch_metadata_keeps_iframe_but_recovers_standalone(reader):
    browser, pid = reader
    url = f"/reader/{pid}?q=gene%20expression&embed=1&page=2#note"
    response = browser.get(url, follow_redirects=False)
    assert response.status_code == 200 and 'class="bare"' in response.text
    head = response.text.split("</head>", 1)[0]
    scripts = re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", head, re.S | re.I)
    assert scripts, "Direct navigation needs a fallback when the browser omits Sec-Fetch-Dest"
    run_js(r"""
const vm = require("node:vm"), assert = require("node:assert/strict");
const origin = "http://127.0.0.1:8765", scripts = JSON.parse(process.argv[1]);
for (const framed of [true, false]) {
  const location = new URL(process.argv[2], origin);
  let target;
  location.replace = value => { target = new URL(value, location); };
  const root = {location, URL, URLSearchParams};
  root.window = root.self = root;
  root.top = root.parent = framed ? {} : root;
  const context = vm.createContext(root);
  for (const code of scripts) vm.runInContext(code, context);
  if (framed) {
    assert.equal(target, undefined);
  } else {
    assert.ok(target);
    assert.equal(target.pathname, location.pathname);
    assert.equal(target.searchParams.has("embed"), false);
    assert.equal(target.searchParams.get("page"), "2");
    assert.equal(target.searchParams.get("q"), "gene expression");
    assert.equal(target.hash, "#note");
  }
}
""", json.dumps(scripts), url)


def test_settings_return_uses_the_parent_document_and_repairs_legacy_links():
    run_js(r"""
const vm = require("node:vm"), fs = require("node:fs"), assert = require("node:assert/strict");
const app = fs.readFileSync(process.argv[1], "utf8");
const origin = "http://127.0.0.1:8765", key = "papermap.libraryReturn";
const state = new Map(), sessionStorage = {getItem:k => state.get(k) ?? null, setItem:(k,v) => state.set(k,v)};
function open(path, framed = false) {
  const location = new URL(path, origin), callbacks = new Map(), back = {href:"/"};
  const classes = new Set([framed ? "bare" : "app"]);
  const document = {
    referrer:"", body:{dataset:{}, classList:{contains:c => classes.has(c)}},
    querySelector:() => null, querySelectorAll:s => s === "[data-settings-back]" ? [back] : [],
    getElementById:() => null, addEventListener:() => {},
  };
  const root = {document, location, URL, URLSearchParams, sessionStorage, localStorage:{getItem:() => null},
    setTimeout:()=>1, setInterval:()=>1, addEventListener:(name,fn) => {
      if (!callbacks.has(name)) callbacks.set(name, []);
      callbacks.get(name).push(fn);
    }};
  root.window = root.self = root;
  root.top = root.parent = framed ? {} : root;
  vm.runInNewContext(app, root);
  return {get href(){ const url = new URL(back.href, origin); return url.pathname + url.search + url.hash; },
    leave:() => (callbacks.get("pagehide") || []).forEach(fn => fn({}))};
}
const library = "/?c=2&q=atlas&id=15", parent = open(library);
const iframe = open("/reader/15?embed=1", true);
assert.equal(state.get(key), library);
parent.leave(); iframe.leave();
assert.equal(state.get(key), library);
assert.equal(open("/settings").href, library);
for (const [saved, expected] of [
  ["/reader/15?embed=1", "/?id=15"], ["/reader/15?page=2&embed=1&q=x#note", "/?id=15"],
  ["/reader/15?page=2", "/reader/15?page=2"]
]) {
  state.set(key, saved);
  assert.equal(open("/settings").href, expected);
}
""", str(APP_JS))
