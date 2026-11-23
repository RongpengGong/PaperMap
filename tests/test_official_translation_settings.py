from papermap.config import load

from .test_settings_sections import field_data, nodes, settings_client  # noqa: F401


def test_translation_credentials_are_local_to_each_service(settings_client, config):
    def settings_form():
        response = settings_client.get("/settings?section=translation")
        assert response.status_code == 200
        form = nodes(response.text).all(lambda n: n.attrs.get("id") == "settings")[0]
        return response.text, form, field_data(form)

    def save(data):
        assert settings_client.post("/settings", data=data, follow_redirects=False).status_code == 303
        return {s["id"]: s for s in load(config.path).raw["translation"]["services"]}

    _, _, data = settings_form()
    keys = {f"tr_{sid}_api_key": f"secret-for-{sid}" for sid in ("niutrans", "zhipu", "tencent")}
    data.update(keys, tr_default="niutrans", tr_niutrans_app_id="test-app-id")
    services = save(data)
    for sid in ("niutrans", "zhipu", "tencent"):
        assert services[sid]["api_key"] == keys[f"tr_{sid}_api_key"]
    assert services["niutrans"]["app_id"] == "test-app-id"
    html, form, data = settings_form()
    assert all(value not in html for value in (*keys.values(), "test-app-id"))
    assert all(data[name] == "" for name in (*keys, "tr_niutrans_app_id"))
    shown = form.all(lambda n: "data-tr-pane" in n.attrs and "hidden" not in n.attrs)
    assert [n.attrs["data-tr-pane"] for n in shown] == ["niutrans"]

    data.update(tr_default="tencent", tr_niutrans_clear="1", tr_zhipu_api_key="replacement")
    services = save(data)
    assert not services["niutrans"].get("app_id") and not services["niutrans"].get("api_key")
    assert services["tencent"]["api_key"] == "secret-for-tencent"
    assert services["zhipu"]["api_key"] == "replacement"


def test_official_translators_cannot_be_deleted_as_custom_services(settings_client):
    html = settings_client.get("/settings?section=translation").text
    csrf = nodes(html).all(lambda n: n.attrs.get("id") == "csrf")[0].attrs["value"]
    for sid in ("niutrans", "zhipu", "tencent"):
        response = settings_client.post(f"/api/translation/services/{sid}/delete", json={"csrf": csrf})
        assert response.status_code == 200 and response.json()["ok"] is False
