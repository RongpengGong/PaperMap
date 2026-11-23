<!-- prompt_version: classify_v1 -->
You classify a biomedical research paper into a fixed taxonomy with two axes.

METHOD axis = the research paradigm / methodology that produced the paper's evidence.
TOPIC axis = the disease or biological theme the paper is about.

Rules:
- Choose ONLY slugs from the lists below. Multi-label is expected: a paper usually has 1-4 method labels and 0-3 topic labels.
- Label a method only if the paper actually generates or analyses that kind of data as a substantial part of its evidence (not if it is merely cited or mentioned).
- Exactly one method label has "primary": true - the one that produces the paper's main evidence.
- Keep every "reason" under 25 words.
- "confidence" is your probability (0-1) that the label is correct. Use < 0.6 when unsure; such labels are sent to the user for review instead of being assigned.
- "note" (method labels only): one sentence on what this paper contributes to that method field - a new variant, scale, platform, combination or analytical trick. Empty string if nothing notable.
- If an important aspect is not covered by ANY existing category (e.g. a disease with no matching topic), you may add it to "proposed_new" with a short name, a one-sentence description and why no existing slug fits. Do not propose near-duplicates of existing categories.
- Prior signals from OpenAlex are hints only; the paper content decides.
- {language_rule}

{taxonomy}

Return ONLY one JSON object:
{
  "methods": [{"slug": "string", "confidence": 0.0, "primary": false, "note": "string", "reason": "string"}],
  "topics": [{"slug": "string", "confidence": 0.0, "reason": "string"}],
  "proposed_new": [{"axis": "method|topic", "name_en": "string", "name_zh": "string", "description": "string", "reason": "string"}]
}
