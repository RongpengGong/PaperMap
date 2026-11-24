"""Desktop bilingual reading anchors and independent versions for older translations."""
import hashlib
import json
from html.parser import HTMLParser

from .test_web import client


class Paragraphs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.version = None
        self.anchors = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("data-body") == "translation":
            self.version = attrs["data-translation-version"]
        if "data-tr-anchor" in attrs:
            self.anchors.append((tag, attrs["data-tr-anchor"]))


def test_old_and_regenerated_translations_have_distinct_reading_positions(config, registry):
    browser = client(config, registry)
    card = config.knowledge_dir / "papers" / "Example" / "Example.md"
    card.parent.mkdir(parents=True)
    card.write_text("# Example", encoding="utf-8")
    pid = registry.create_paper(title="Example", status="done", kb_path=card.relative_to(config.knowledge_dir).as_posix())
    path = card.with_suffix(".translation.json")
    endpoint = f"/paper/{pid}/panel"

    assert Paragraphs(browser.get(endpoint).text).version is None
    data = {"generated": "2026-11-24", "paragraphs": [
        {"kind": "meta", "src": "Original metadata"},
        {"kind": "heading", "src": "Results", "tr": "结果"},
        {"src": "Evidence", "tr": "证据"},
    ]}
    path.write_text(json.dumps(data), encoding="utf-8")
    first = Paragraphs(browser.get(endpoint).text)
    assert first.version == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.anchors == [("p", "0-src"), ("h3", "1-src"), ("h3", "1-tr"), ("p", "2-src"), ("p", "2-tr")]
    assert Paragraphs(browser.get(endpoint).text).version == first.version

    data["paragraphs"][2]["tr"] = "重新生成的译文"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Paragraphs(browser.get(endpoint).text).version != first.version
    path.write_text('{"service":"archived"}', encoding="utf-8")
    empty = Paragraphs(browser.get(endpoint).text)
    assert empty.version and not empty.anchors


def test_restore_uses_paragraph_and_fraction_across_tabs_and_viewport_changes():
    from pathlib import Path

    from .test_reader_navigation import run_js

    tracker = Path(__file__).parents[1] / "src" / "papermap" / "web" / "static" / "translation-position.js"
    run_js(r"""
const assert = require('node:assert/strict');
const ticks = new Map(), stored = new Map();
let id = 0, compact = false, height = 100;
global.window = new EventTarget();
global.getComputedStyle = () => ({ overflowY: compact ? 'visible' : 'auto' });
global.sessionStorage = { getItem: k => stored.get(k) || null, setItem: (k,v) => stored.set(k,v) };
global.CSS = { escape: s => s };
global.ResizeObserver = class { observe() {} disconnect() {} };
global.requestAnimationFrame = fn => { ticks.set(++id, fn); return id; };
global.cancelAnimationFrame = i => ticks.delete(i);
function frame() { for (const [i, fn] of ticks) { ticks.delete(i); fn(); } }
function panelFor(paper, version) {
  const panel = new EventTarget(), side = new EventTarget(), split = new EventTarget();
  for (const v of [side,split]) Object.assign(v,{scrollTop:0, clientWidth:500, clientHeight:400,
    clientTop:0, getBoundingClientRect:() => ({top:0})});
  const nodes = Array.from({length:15},(_,i)=>({ dataset:{trAnchor:i+'-tr'}, getBoundingClientRect() {
    const top = i*height - (compact ? split : side).scrollTop;
    return {top,height,bottom:top+height};
  }}));
  const grid = { querySelectorAll:() => nodes, clientWidth:500, get clientHeight(){ return height*nodes.length; } };
  const body = {dataset:{translationVersion:version}, isConnected:true, hidden:false, closest:()=>side,
    querySelector:s=>s==='.bilingual'?grid:nodes.find(n=>s.includes('"'+n.dataset.trAnchor+'"'))};
  panel.dataset={paper}; panel.querySelector=s=>s==='.panel-split'?split:body;
  return {panel,side,split};
}
require(process.argv[1]);
const tracker = window.pmTranslationPosition;
const first=panelFor('1','revision'), a=tracker.attach(first.panel);
a.show(); frame(); first.side.scrollTop=675; first.side.dispatchEvent(new Event('scroll'));
a.leave(); first.side.scrollTop=0; a.show(); frame(); assert.equal(first.side.scrollTop,675);
first.panel.dispatchEvent(new Event('pm:chat-layout')); compact=true; height=60; frame();
assert.equal(first.split.scrollTop,405);
const other=panelFor('2','revision'), b=tracker.attach(other.panel);
b.show(); frame(); assert.equal(other.split.scrollTop,0);
const reloaded=panelFor('1','revision'), same=tracker.attach(reloaded.panel);
same.show(); frame(); assert.equal(reloaded.split.scrollTop,405);
const newer=panelFor('1','new'), fresh=tracker.attach(newer.panel);
fresh.show(); frame(); assert.equal(newer.split.scrollTop,0);
fresh.detach();
""", str(tracker))
