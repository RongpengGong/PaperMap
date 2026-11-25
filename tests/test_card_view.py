"""The paper card shown as tiles, right-click formatting inside it, and "Ask AI" from a card selection."""
import re
from pathlib import Path

from papermap import library
from papermap.web.card_view import cardify, notes_written

from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_reader_navigation import run_js
from .test_web import _token

TAGS = re.compile(r"<[^>]+>")


def _card_body(html: str) -> str:
    return html.split("data-card-body>", 1)[1].split("</div>\n", 1)[0]


def test_card_tiles_keep_the_text_and_markdown(config, registry, pdfs):
    c, _, _ = reader_client(config, registry, pdfs)
    row = registry.paper_by_doi(OWN_DOI)
    card_file = config.knowledge_dir / row["kb_path"]
    before = card_file.read_text("utf-8")
    html = c.get(f"/paper/{row['id']}/panel").text
    for kind in ("one", "question", "design", "methods", "tools", "findings", "limits", "fields"):
        assert f'class="cc cc-{kind}' in html, kind
    # study design facts as label/value tiles, tools as separate chips
    assert '<strong class="cc-k">' in html and '<span class="cc-v">n=12</span>' in html
    assert '<code>Seurat</code><span class="cc-sep">, </span><code>cell2location</code>' in html
    # the notes preview sits outside the formattable card and links to the notes tab
    assert "data-card-notes-open" in html and "data-card-notes-text" in html
    assert card_file.read_text("utf-8") == before  # display only


def test_cardify_adds_wrappers_only():
    rendered = ("<p><em>Zhang, 2024</em></p>\n<h2>研究设计 / 数据</h2>\n<p>Single-cell + spatial\n- <strong>样本量</strong>: n=12\n"
                "- <strong>物种</strong>: human</p>\n<h2>Key findings</h2>\n<ol>\n<li>One</li>\n</ol>\n"
                "<h2>A heading the user added</h2>\n<p>—</p>\n")
    out = cardify(rendered)
    assert TAGS.sub("", out) == TAGS.sub("", rendered)  # right-click formatting counts occurrences in this text
    assert '<ul class="cc-facts">' in out and 'class="cc cc-findings"' in out
    assert 'class="cc cc-other cc-empty"' in out and '<div class="cc-lead">' in out
    assert cardify("<p>no sections</p>") == "<p>no sections</p>"


def test_card_format_skips_the_title_shown_in_the_header(config, registry, pdfs):
    c, _, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    # "liver" is in the title (header only), the takeaway (1st in the card) and the design tissues (2nd)
    r = c.post(f"/paper/{pid}/card/format",
               json={"csrf": tok, "text": "liver", "occurrence": 1, "kind": "highlight", "color": "#c8f7c5"}).json()
    assert r["ok"]
    design = r["html"].split('class="cc cc-design', 1)[1].split("</section>", 1)[0]
    assert ">liver</mark>" in design and "<mark" not in r["html"].split('class="cc cc-design', 1)[0]


def test_notes_tab_reads_as_tiles_and_edits_in_the_editor(config, registry, pdfs):
    c, _, _ = reader_client(config, registry, pdfs)
    row = registry.paper_by_doi(OWN_DOI)
    notes = library.notes_path(config.knowledge_dir, row["kb_path"])
    template = notes.read_text("utf-8") if notes.exists() else ""
    assert not notes_written(template)  # a fresh notes file opens in the editor
    html = c.get(f"/paper/{row['id']}/panel").text
    assert 'data-notes-mode="edit"' in html and "data-notes-cards hidden" in html
    text = ("# 我的笔记 · x\n\n（这个文件完全属于你，PaperMap 永远不会改动它。）\n\n## 与我相关的点\n\n- niches before invasion?\n\n"
            "## 方法\n\n`cell2location`, `Seurat`\n")
    notes.write_text(text, "utf-8")
    html = c.get(f"/paper/{row['id']}/panel").text
    assert 'data-notes-mode="cards"' in html
    tiles = html.split("data-notes-cards", 1)[1].split("data-notes-live", 1)[0]
    assert 'class="cc cc-other"' in tiles and "niches before invasion?" in tiles and 'class="cc-chips"' in tiles
    assert f"{text}</textarea>" in html  # the editor still gets the file as written
    assert notes.read_text("utf-8") == text
    tok = _token(c)
    r = c.post("/api/notes-preview", json={"csrf": tok, "text": text, "cards": True}).json()
    assert r["ok"] and "card-tiles" in r["html"]
    assert c.post("/api/notes-preview", json={"csrf": tok, "text": template, "cards": True}).json() == {"ok": True, "html": ""}
    assert "card-tiles" not in c.post("/api/notes-preview", json={"csrf": tok, "text": text}).json()["html"]


def test_pages_and_skill_readings_use_tiles(config, registry, pdfs):
    c, _, _ = reader_client(config, registry, pdfs)
    row = registry.paper_by_doi(OWN_DOI)
    hub = config.knowledge_dir / "fields" / "spatial-transcriptomics.md"
    before = hub.read_text("utf-8")
    page = c.get("/field/spatial-transcriptomics").text
    view = page.split('class="md card-view page-view" data-kb-notes=', 1)[1]
    assert 'class="cc cc-overview' in view and 'class="cc cc-mine' in view
    # kbnotes.js turns everything after this heading into the editable block, all inside one tile
    mine = view.split('class="cc cc-mine', 1)[1].split("</section>", 1)[0]
    assert '<h2 class="cc-h">我的笔记</h2><div class="cc-b">' in mine and "程序永远不会改动这一块" in mine
    assert hub.read_text("utf-8") == before
    hb = config.knowledge_dir / "handbooks" / "spatial-transcriptomics.md"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("---\nslug: spatial-transcriptomics\n---\n\n# 手册\n\n## 概述\n\nx\n\n## 我的笔记\n\n<!-- user:begin -->\n"
                  "mine\n<!-- user:end -->\n\n## A. 来自你的文献库\n\n### 代表研究\n\n- a\n\n## B. 背景知识补充（LLM 生成，需核实）\n\n- b\n", "utf-8")
    page = c.get("/handbook/spatial-transcriptomics").text
    assert all(f'class="cc cc-{k}' in page for k in ("overview", "mine", "library", "background"))
    card = config.knowledge_dir / row["kb_path"]
    (card.parent / f"{card.stem}.papermap-close-reading.md").write_text(
        "---\nskill: papermap-close-reading\nskill_name: 精读\ntitle: t\ndoi: d\nmodel: m\ngenerated: '2026-11-25'\n---\n\n"
        "# 精读\n\n## 论点\n\n作者认为……\n", "utf-8")
    html = c.get(f"/paper/{row['id']}/panel").text
    skill = html.split('data-body="skill-papermap-close-reading"', 1)[1].split("</section></div>", 1)[0]
    assert '<div class="md card-view">' in skill and 'class="cc cc-other"' in skill


def test_notes_written_ignores_headings_and_the_file_hint():
    assert not notes_written("# 我的笔记 · t\n\n（这个文件完全属于你，PaperMap 永远不会改动它。）\n\n## 与我相关的点\n\n")
    assert not notes_written("# My notes · t\n\n(This file is yours; PaperMap never changes it.)\n\n## Relevance to me\n\n-\n")
    assert notes_written("## 与我相关的点\n\n- a question\n")
    assert not notes_written(None)


def test_card_selection_offers_ask_ai_without_a_page():
    script = Path(__file__).parents[1] / "src/papermap/web/static/selection.js"
    run_js(r"""
const assert = require('node:assert/strict'), vm = require('node:vm');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
function menu(where) {
  const handlers = {}, asked = [];
  const pop = {style:{}, offsetWidth:100, offsetHeight:50, h:{}, addEventListener(t, f) { this.h[t] = f; },
               remove() {}, contains() { return false; }};
  const document = {body:{dataset:{csrf:'x'}, appendChild() {}}, addEventListener(t, f) { handlers[t] = f; },
                    createElement() { return pop; }, createRange() { return {setStart() {}, setEnd() {}, toString: () => ''}; }};
  const panel = {dataset:{paper:'7'}, querySelector: s => s === '[data-paper-chat-drawer]' ? {} : null};
  const area = {contains: () => true};
  const node = {nodeType:1, closest(s) {
    if (s === '#main_content, .ai-drawer') return {};
    if (s === '.panel') return panel;
    if (s === '[data-card-body]') return where === 'card' ? area : null;
    if (s === '[data-body="translation"] .bilingual') return where === 'translation' ? area : null;
    if (s === '[data-notes-cards]') return where === 'notes' ? area : null;
    if (s === '[data-quote-page]') return {dataset:{quotePage:'5'}};
    return null;
  }};
  const range = {commonAncestorContainer: node, startContainer: node, startOffset: 0};
  const window = {getSelection: () => ({rangeCount:1, toString: () => 'Finding two', getRangeAt: () => range}),
                  pmChatAsk: (...a) => asked.push(a)};
  vm.runInNewContext(source, {window, document, innerWidth:800, innerHeight:600, fetch: async () => ({}), alert() {}});
  let prevented = false;
  handlers.contextmenu({target:{closest: () => null}, clientX:10, clientY:10,
                        preventDefault() { prevented = true; }, stopPropagation() {}});
  assert.ok(prevented);
  return {pop, asked};
}
const card = menu('card');
assert.match(card.pop.innerHTML, /data-act="ask"/);
assert.match(card.pop.innerHTML, /data-hl=/);  // formatting stays in the same menu
card.pop.h.click({target:{closest: () => ({dataset:{act:'ask'}})}});
assert.deepEqual(card.asked, [['7', 'Finding two', null]]);  // card text has no PDF page
const tr = menu('translation');
assert.doesNotMatch(tr.pop.innerHTML, /data-hl=/);
tr.pop.h.click({target:{closest: () => ({dataset:{act:'ask'}})}});
assert.deepEqual(tr.asked, [['7', 'Finding two', 5]]);
const notes = menu('notes');
assert.doesNotMatch(notes.pop.innerHTML, /data-hl=/);  // notes are formatted in their own editor
notes.pop.h.click({target:{closest: () => ({dataset:{act:'ask'}})}});
assert.deepEqual(notes.asked, [['7', 'Finding two', null]]);
""", str(script))
