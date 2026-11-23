from fastapi.testclient import TestClient

from papermap import resources as res
from papermap.web.app import create_app

from .test_pipeline import make_pipeline


def test_extract_roles_and_superscripts():
    pages = ["Raw data have been deposited in the Gene Expression Omnibus under accession\nGSE123456. "
             "Public scRNA-seq datasets were obtained from GEO (GSE13949551, GSE148673) and TCGA-BRCA. "
             "Code is available at https://github.com/lab/tool. Proteomics: PXD012345; EGA: EGAS00001004567."]
    items = {i["accession"]: i for i in res.extract_text(pages)}
    assert items["GSE123456"]["role"] == "generated"
    assert items["GSE139495"]["role"] == "reused" and "GSE13949551" not in items  # superscript '51' stripped
    assert items["GSE148673"]["url"].endswith("acc=GSE148673")
    assert {"TCGA-BRCA", "github.com/lab/tool", "PXD012345", "EGAS00001004567"} <= set(items)
    assert items["github.com/lab/tool"]["role"] == "generated"


def test_resources_page_and_csv(config, registry, pdfs, tmp_path):
    from .conftest import BODY, make_pdf
    pdf = make_pdf(pdfs["paper"].parent / "data.pdf", [
        "#Spatial transcriptomics reveals fibroblast niches in liver cancer\nhttps://doi.org/10.1234/demo.2024.001\n"
        f"Abstract {BODY[:400]}\nData availability: sequencing data have been deposited in GEO under accession GSE200001."])
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdf)
    rows = res.rows(registry)
    assert [r["accession"] for r in rows] == ["GSE200001"] and rows[0]["role"] == "generated"
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    page = c.get("/resources").text
    assert "GSE200001" in page and "本研究产生" in page and "/reader/1?page=1&q=GSE200001" in page
    csv = c.get("/resources.csv?kind=geo").text
    assert csv.startswith("﻿paper,") and "GSE200001" in csv
    assert "GSE200001" not in c.get("/resources?kind=ega").text
    assert "GSE200001" in c.get("/resources?role=generated").text
    assert "GSE200001" not in c.get("/resources?role=_none").text
