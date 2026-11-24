"""Real reading persists across sessions. Loading an embedded preview is passive."""
from pathlib import Path
import re
import time

from papermap import reading_progress
from papermap.registry import Registry

from .conftest import OWN_DOI
from .test_reader import client
from .test_reader_navigation import run_js
from .test_web import _token


def test_reader_progress_lifecycle(config, registry, pdfs):
    web, _, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    reader = f"/reader/{pid}"
    api = f"/api/reader/{pid}"
    assert 'data-start=""' in web.get(reader + "?embed=1").text
    assert web.get(api + "/info").status_code == 200
    assert web.get(api + "/page/0/words").status_code == 200
    assert registry.get_paper(pid)["read_status"] == "unread"
    assert web.get("/api/reading/recent").json()["recent"] == []

    at = time.time_ns() // 1000
    payload = {"csrf": _token(web), "at": at, "page": 1, "offset": 0.46}
    post = lambda **override: web.post(api + "/progress", json=payload | override)
    assert post(csrf="").status_code == 403
    for invalid in ({"page": 2}, {"page": -1}, {"offset": 1.2}, {"offset": True}, {"at": "now"}):
        assert post(**invalid).status_code == 422
    assert post().json()["saved"] is True
    for older in (at - 1, at):
        assert post(at=older, page=0, offset=0).json()["saved"] is False

    reopened = Registry(config.db_path)
    try:
        row = reopened.get_paper(pid)
        assert (row["reading_page"], row["reading_offset"], row["read_status"]) == (1, 0.46, "reading")
        assert reading_progress.recent(reopened) == [{"id": pid, "title": row["title"], "page": 1}]
    finally:
        reopened.close()
    regular = web.get(reader).text
    assert 'data-start=""' in regular and 'data-resume-page="1" data-resume-offset="0.46"' in regular
    assert 'data-start="1"' in web.get(reader + "?page=1").text
    shelf = re.search(r'<section id="recent-reading".*?</section>', web.get("/").text, re.S).group()
    assert f'href="/?id={pid}&amp;read=1"' in shelf and "第 2 页" in shelf and "hidden" not in shelf
    assert re.search(r'<section id="recent-reading"[^>]+hidden', web.get("/?q=demo").text)
    assert 'id="recent-reading"' not in web.get("/?c=none").text

    registry.execute("UPDATE papers SET read_status='read' WHERE id=?", (pid,))
    assert post(at=at + 1).json()["recent"] == []
    assert registry.get_paper(pid)["read_status"] == "read"


def test_recent_is_limited_and_ordered(registry):
    for n in range(1, 6):
        registry.execute("INSERT INTO papers (id,title,created_at,updated_at) VALUES (?,?,'','')", (n, f"Paper {n}"))
        reading_progress.save(registry, n, {"page": n-1, "offset": 0, "at": n}, 5)
    assert [r["id"] for r in reading_progress.recent(registry)] == [5, 4, 3]
    registry.execute("UPDATE papers SET status='ignored' WHERE id=5")
    assert [r["id"] for r in reading_progress.recent(registry)] == [4, 3, 2]


def test_passive_preview_and_normalized_restoration():
    script = Path(__file__).parents[1] / "src/papermap/web/static/reader-position.js"
    run_js(r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
function target() {
  const handlers = {};
  return {on: handlers, addEventListener(type, fn) { handlers[type] = fn; }};
}
function reader(embedded, storage = new Map()) {
  const root = {dataset:{paper:1, resumePage:1, resumeOffset:.25, resumeAt:1}};
  const viewport = Object.assign(target(), {scrollTop:0, clientHeight:100});
  const els = [{offsetTop:12,offsetHeight:400},{offsetTop:422,offsetHeight:600}];
  const window = target(), document = target(), posts = [];
  window.self = window; window.top = embedded ? {} : window;
  window.parent = {postMessage(){}};
  vm.runInNewContext(source, {window, document, location:{origin:'http://127.0.0.1'},
    performance:{timeOrigin:100, now:()=>10}, sessionStorage:{
      getItem:key=>storage.get(key) || null, setItem:(key, value)=>storage.set(key, value)},
    fetch: async (url, opts) => { posts.push(JSON.parse(opts.body)); return {ok:true,json:async()=>({})}; },
    setTimeout:()=>1, clearTimeout:()=>{}});
  const progress = window.pmReadingProgress(root, viewport, els,'token');
  return {els, root, viewport, document, window, progress, posts};
}
const store = new Map(), preview = reader(true,store);
preview.progress.restore(); preview.progress.start();
assert.equal(preview.viewport.scrollTop,560);
preview.viewport.on.scroll();
preview.document.on.pointerdown({isTrusted:false}); preview.window.on.pagehide();
assert.equal(preview.posts.length,0);
preview.viewport.on.wheel({isTrusted:true});
preview.viewport.scrollTop=710; preview.viewport.on.scroll(); preview.window.on.pagehide();
assert.equal(preview.posts[0].page,1); assert.equal(preview.posts[0].offset,.5);
const resumed=reader(false,store);
resumed.els[1].offsetTop=312; resumed.els[1].offsetHeight=300;
resumed.progress.restore(); resumed.progress.start(); resumed.window.on.pagehide();
assert.equal(resumed.viewport.scrollTop,450); assert.equal(resumed.posts[0].offset,.5);
const explicit=reader(false,store);
explicit.progress.start(); explicit.window.on.pagehide();
assert.equal(explicit.posts[0].page,0); assert.equal(explicit.posts[0].offset,0);
""", str(script))
