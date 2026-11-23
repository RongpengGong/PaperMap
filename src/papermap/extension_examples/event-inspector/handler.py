"""Inspect the event delivered by PaperMap using only its supplied JSON data."""


def handle(event):
    kind = event["type"]
    if kind == "plugin.run":
        return {"status": "ready", "listening_for": ["paper.ready", "field.proposed"]}
    data = event["data"]
    result = {"event_id": event["id"], "type": kind}
    if kind == "paper.ready":
        result["paper_id"] = data["paper_id"]
    elif kind == "field.proposed":
        result["field_id"] = data["field_id"]
        result["slug"] = data["slug"]
    return result
