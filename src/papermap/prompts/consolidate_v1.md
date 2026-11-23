<!-- prompt_version: consolidate_v1 -->
You consolidate what a researcher's own paper library says about one research field.
Use ONLY the paper summaries provided. Every item MUST cite reference ids like ["P1", "P4"]; never add facts that are not in the summaries. Put ids only in "refs", not inside the text.

Produce:
- "overview": 3-6 items that together describe the field as seen in this library (what is studied, with which designs/platforms, at what scale).
- "progress": up to 6 items on method variants and progress - how approaches differ or evolved across these papers (newer platforms, larger cohorts, new analysis tricks). Compare papers where possible.
- "disputes": items where papers reach different or conflicting conclusions, or use incompatible definitions/methods for the same question. Each dispute MUST cite at least two papers and state both sides. If there are no real disagreements, return an empty list - do not invent conflicts.
{language_rule}

Return ONLY one JSON object:
{
  "overview": [{"text": "string", "refs": ["P1"]}],
  "progress": [{"text": "string", "refs": ["P1", "P2"]}],
  "disputes": [{"text": "string", "refs": ["P1", "P2"]}]
}
