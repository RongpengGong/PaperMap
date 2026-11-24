"""PaperMap skills must be local installations, including on older configurations."""
from pathlib import Path

import pytest

from papermap import plugins, skills_runtime as skills

from .test_plugin_contributions import package  # noqa: F401 - enabled prompt plugin fixture
from .test_plugins import symlink
from .test_reader import client
from .test_web import _token


def write_skill(folder, body):
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(body, encoding="utf-8")
    return folder


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_only_papermap_sources_are_discovered(config, user_home, package):  # noqa: F811
    host_skills = {
        ".claude": "nature-paper-xray",
        ".codex": "codex-only",
        ".agents": "agents-only",
        ".chatgpt": "chatgpt-only",
    }
    for host, name in host_skills.items():
        write_skill(user_home / host / "skills" / name, "External instructions.")
    write_skill(user_home / ".claude" / "skills" / skills.FALLBACK_DEFAULT, "Foreign override.")
    write_skill(config.data_dir / "skills" / "local-only", "My installed skill.")
    config.raw["skills"]["default"] = "nature-paper-xray"  # saved by an older version

    found = skills.discover(config)
    assert set(host_skills.values()).isdisjoint(found)
    assert found[skills.FALLBACK_DEFAULT].source == "builtin"
    assert skills.default_skill_id(config, found) == skills.FALLBACK_DEFAULT
    assert found["local-only"].source == "papermap"
    assert found["ext.research.read"].source == "plugin"
    plugins.set_enabled(config, "research", False)
    assert "ext.research.read" not in skills.discover(config)

    skills.install(config, user_home / ".claude" / "skills" / "nature-paper-xray")
    config.raw["skills"]["default"] = "removed-skill"
    assert skills.default_skill_id(config, skills.discover(config)) == skills.FALLBACK_DEFAULT


def test_web_run_and_manual_install_are_scoped(config, registry, pdfs, user_home, monkeypatch):
    external = write_skill(user_home / ".claude" / "skills" / "private-reader", "old instructions")
    config.raw["skills"]["default"] = external.name
    web, pipe, _ = client(config, registry, pdfs)
    csrf = _token(web)
    for page in ("/skills", "/paper/1/panel"):
        html = web.get(page).text
        assert skills.FALLBACK_DEFAULT in html and external.name not in html
    assert not web.post("/paper/1/skill", json={"csrf": csrf, "skill": external.name}).json()["ok"]
    assert not registry.query("SELECT id FROM jobs WHERE kind='skill'")
    with pytest.raises(ValueError):
        pipe._job_skill({"paper_id": 1, "arg": external.name}, lambda *_: None)

    submitted = []

    def record_job(kind, paper_id, arg):
        submitted.append((kind, paper_id, arg))
        return len(submitted)

    monkeypatch.setattr(pipe.jobs, "submit", record_job)
    config.raw["skills"]["auto_new_papers"] = True
    pipe._auto_jobs(1)
    assert submitted == [("skill", 1, skills.FALLBACK_DEFAULT)]

    installed = web.post("/api/skills/install", json={"csrf": csrf, "path": str(external)}).json()
    assert installed["ok"] and installed["id"] == external.name
    (external / "SKILL.md").write_text("changed at the source", encoding="utf-8")
    owned = skills.discover(config)[external.name]
    assert owned.source == "papermap" and owned.body == "old instructions"
    assert external.name in web.get("/paper/1/panel").text
    assert web.post("/paper/1/skill", json={"csrf": csrf, "skill": external.name}).json()["ok"]
    assert submitted[-1] == ("skill", 1, external.name)


def test_skill_paths_cannot_link_into_other_apps(config, user_home):
    external = write_skill(user_home / ".codex" / "skills" / "private-reader", "External body.")
    root = config.data_dir / "skills"
    root.mkdir(exist_ok=True)
    symlink(root / "linked-skill", external, directory=True)
    manifest = root / "linked-manifest"
    manifest.mkdir()
    symlink(manifest / "SKILL.md", external / "SKILL.md")
    assert {"linked-skill", "linked-manifest", "private-reader"}.isdisjoint(skills.discover(config))

    local = write_skill(root / "notes", "Review the local reference. See ../../skills-other/outside.md")
    refs = local / "references"
    refs.mkdir()
    (refs / "good.md").write_text("Local reference.", encoding="utf-8")
    symlink(refs / "foreign.md", external / "SKILL.md")
    sibling = config.data_dir / "skills-other"
    sibling.mkdir()
    (sibling / "outside.md").write_text("Unrelated skill file.", encoding="utf-8")
    # Even a sibling with a common path prefix must be outside the allowed root.
    found = skills.discover(config)
    if "notes" in found and found["notes"].supported:
        assert skills._references(found["notes"]) == [("good.md", "Local reference.")]


def test_linked_library_skill_root_is_not_scanned(config, user_home):
    external = write_skill(user_home / ".agents" / "skills" / "foreign-root", "External body.")
    local_root = config.data_dir / "skills"
    if local_root.exists():
        local_root.rmdir()  # some config versions pre-create the empty skills directory
    symlink(local_root, external.parent, directory=True)
    found = skills.discover(config)
    assert "foreign-root" not in found and skills.FALLBACK_DEFAULT in found


def test_saved_reading_survives_disabling_skill(config, registry, pdfs, package, user_home):  # noqa: F811
    web, pipe, _ = client(config, registry, pdfs)
    sid = "ext.research.read"
    pdf, card = pipe._paper_files(1)
    saved = skills.run(skills.discover(config)[sid], config, registry, pipe.llm, 1, pdf, card)
    plugins.set_enabled(config, "research", False)

    generated = saved.read_text(encoding="utf-8")
    skills.output_path(card, "wrong-name").write_text(generated, encoding="utf-8")
    skills.output_path(card, "incomplete").write_text("---\nskill: incomplete\n---\nOther", encoding="utf-8")
    skills.output_path(card, "translation").write_text("# A translation", encoding="utf-8")
    external = user_home / "linked.md"
    external.parent.mkdir(exist_ok=True)
    external.write_text(generated.replace(sid, "linked"), encoding="utf-8")
    symlink(skills.output_path(card, "linked"), external)

    html = web.get("/paper/1/panel").text
    assert f'data-tab="skill-{sid}"' in html and "Evidence check" in html and "测试技能输出" in html
    for key in ("wrong-name", "incomplete", "notes", "translation", "linked"):
        assert f'data-tab="skill-{key}"' not in html
    assert f'<option value="{sid}"' not in html and sid not in web.get("/skills").text
    assert not web.post("/paper/1/skill", json={"csrf": _token(web), "skill": sid}).json()["ok"]
