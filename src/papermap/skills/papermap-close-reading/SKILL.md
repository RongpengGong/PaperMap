---
name: papermap-close-reading
description: 精读一篇生物医学论文：论点—证据对照、承重实验、替代解释、没有报告的内容、读完该带走什么。Close reading of one biomedical paper — claims vs evidence, load-bearing experiments, alternative explanations, gaps. Ships with PaperMap (MIT).
---

# 精读（PaperMap 自带）

Read the supplied paper the way an experienced lab member explains it at journal club: what the authors actually claim, which experiments carry those claims, and how far the evidence reaches. Not a summary, not a translation, not a review score.

## Output (Markdown, in this order)

1. **一句话：这篇到底声称了什么** — the central claim in the authors' own strength of wording (keep "suggests" vs "shows").
2. **研究是怎么走到这一步的** — the concrete gap or observation the work starts from, and the main bet (the key data type, model or cohort the conclusion depends on).
3. **论点 ↔ 证据对照表** — a table: claim | supporting evidence (figure/table/panel, assay, n) | strength (strong / moderate / weak) | what would change the conclusion.
4. **承重实验 vs 装饰** — which experiments or analyses the main claim cannot survive without, and which only add colour. Name the control, ablation, knockout, validation cohort or orthogonal assay that settles it; if none exists, say the paper does not test it.
5. **替代解释** — for each major causal or mechanistic claim, the most plausible alternative explanation (confounding, batch/cohort effects, compositional effects in single-cell or spatial data, reverse causation, selection) and whether the paper rules it out.
6. **没有报告的内容** — things a reader needs but the paper does not report (sample sizes per group, statistics, multiple-testing control, data/code availability, independent validation, sex/age, effect sizes). Write "未报告" rather than guessing.
7. **方法上值得学的地方** — specific analytical or experimental tricks worth reusing, with enough detail to find them in the methods.
8. **读完该带走什么** — 3–5 bullet take-aways, plus open questions the paper leaves.

## Rules

- Use only the supplied text. Never invent numbers, figure panels, cohorts or citations; if the text extraction lost something (e.g. figure content), say so.
- Point to evidence by figure/table/section names as they appear in the text.
- Separate what the authors state from your own assessment ("作者认为…" vs "我的判断…").
- Keep gene, protein, software and dataset names in their original form.
