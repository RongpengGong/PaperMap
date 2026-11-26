"""Language defaults follow the system; cards and translations can use more languages; gallery and
data resources filter by category."""
from papermap import i18n, kb_writer, translate
from papermap.figures import figure_types
from .test_reader import client
from .test_web import _token


def test_locale_tags_map_to_interface_languages():
    assert i18n.from_locale("zh-Hans-CN") == "zh-CN"
    assert i18n.from_locale("zh_TW.UTF-8".split(".")[0]) == "zh-TW"
    assert i18n.from_locale("zh-Hant-HK") == "zh-TW"
    assert i18n.from_locale("de-DE") == "de"
    assert i18n.from_locale("fr-FR") == "en"  # languages without a catalog read English
    assert i18n.from_locale("C") is None


def test_unset_language_follows_system_and_cards_follow_interface(config, monkeypatch):
    config.raw["appearance"].pop("language", None)
    monkeypatch.setattr(i18n, "system_language", lambda: "de")
    assert i18n.config_language(config) == "de"
    assert config.content_language == "de" and config.output_language == "en"  # German prose, English structure
    assert i18n.translation_target(config) == "de"
    assert "German" in i18n.prose_language(config)
    config.raw["appearance"]["language"] = "zh-TW"
    assert config.content_language == "zh-TW" and config.output_language == "zh"
    config.raw["output_language"] = "en"  # an explicit choice wins over the interface
    config.raw["translation"]["target_language"] = "ja"
    assert config.content_language == "en" and i18n.translation_target(config) == "ja"
    config.validate()


def test_card_headings_in_german_and_traditional_chinese_keep_notes_detection():
    meta = {"title": "T", "doi": "10.1234/x", "authors": [{"name": "A B"}], "year": 2024}
    card = {"one_sentence": "Kern.", "key_findings": ["x"]}
    for lang, heading in (("de", "## Kernaussage"), ("zh-TW", "## 一句話結論")):
        text = kb_writer.render_card(meta, card, [], [], lang, stem="s")
        assert heading in text
        assert "## " + kb_writer.headings(lang)["mine"] in text
        part = kb_writer.editable_part(text, "zh")  # detection does not depend on the current language
        assert heading in part and kb_writer.headings(lang)["mine"] not in part


def test_translation_prompts_name_the_target_and_official_codes():
    assert "繁体中文" in translate.build_prompt("x", "zh-TW", "translate", "", [], False)
    assert "德文" in translate.build_prompt("x", "de", "translate", "", [], False)
    assert translate.NIUTRANS_CODES["zh-TW"] == "cht"


def test_settings_save_new_languages(config, registry, pdfs):
    c, _, _ = client(config, registry, pdfs)
    page = c.get("/settings?section=translation").text
    assert 'value="ja"' in page and 'value="zh-TW"' in page
    assert 'data-lang-pick="system"' in c.get("/settings?section=appearance").text


def test_figure_types_and_gallery_filters(config, registry, pdfs):
    assert set(figure_types("UMAP of all cells; heatmap of marker genes; Kaplan–Meier survival")) >= \
        {"embedding", "heatmap", "survival"}
    c, pipe, _ = client(config, registry, pdfs)
    from papermap import figures as figmod

    figmod.ensure_schema(registry)
    pid = registry.query("SELECT id FROM papers")[0]["id"]
    registry.execute("DELETE FROM figures")
    registry.execute("INSERT OR REPLACE INTO figures_state VALUES (?, 'rev')", (pid,))
    for i, legend in enumerate(["Fig. 1 | UMAP of cells", "Fig. 2 | Volcano plot", "Extended Data Fig. 1 | UMAP again"]):
        registry.execute("INSERT INTO figures(id,paper_id,label,page,x0,y0,x1,y1,legend,created_at) "
                         "VALUES (?,?,?,0,0,0,10,10,?,'now')", (i + 1, pid, legend.split(" |")[0], legend))
    assert c.get("/figures").text.count('class="fig-card"') == 3
    assert c.get("/figures?kind=embedding").text.count('class="fig-card"') == 2
    assert c.get("/figures?kind=embedding&main=1").text.count('class="fig-card"') == 1
    assert c.get("/figures?q=volcano").text.count('class="fig-card"') == 1
    assert c.get("/figures?scope=star").text.count('class="fig-card"') == 0
    registry.execute("UPDATE papers SET starred=1 WHERE id=?", (pid,))
    assert c.get("/figures?scope=star").text.count('class="fig-card"') == 3
    assert c.get("/figures?fragment=1&offset=0").text.count('class="fig-card"') == 3
    assert "revision=rev" in c.get("/figures").text  # thumbnails are cacheable by revision


def test_resources_filter_by_search_and_purpose(config, registry, pdfs):
    c, _, _ = client(config, registry, pdfs)
    from papermap import purposes, resources

    resources.ensure_schema(registry)
    pid = registry.query("SELECT id FROM papers")[0]["id"]
    for acc, kind in (("GSE123456", "geo"), ("PXD000111", "pride")):
        registry.execute("INSERT INTO data_resources(paper_id,kind,label,accession,url,role,page,context,created_at) "
                         "VALUES (?,?,?,?,'',?,0,'deposited here','now')", (pid, kind, kind.upper(), acc, "generated"))
    assert "GSE123456" in c.get("/resources?q=GSE").text and "PXD000111" not in c.get("/resources?q=GSE").text
    assert "PXD000111" not in c.get("/resources?scope=u:data").text
    purposes.set_tag(registry, [pid], "data", True)
    page = c.get("/resources?scope=u:data&kind=pride").text
    assert "PXD000111" in page and "GSE123456" not in page
    assert "PXD000111" in c.get("/resources.csv?q=PXD").text
    _token(c)
