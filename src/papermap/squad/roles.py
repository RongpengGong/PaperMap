"""The three squads of milestone 1: who is on each team, what each member may use, and how the lead
is told to split the work."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..i18n import L

READ_LIB = ["library_overview", "list_papers", "search_library", "read_card", "find_in_paper", "read_pages"]


@dataclass
class Member:
    key: str
    label: str
    brief: str
    tools: list[str]
    max_steps: int = 8


@dataclass
class Template:
    key: str
    label: str
    description: str
    lead_brief: str
    members: dict[str, Member]
    final_member: str = "writer"
    params: dict = field(default_factory=dict)


LEAD = L("队长")

WRITER_RULES = (
    "Write in Markdown with short sections. Every sentence that states a finding must cite its source with "
    "markers exactly as given by your teammates or tools: [P12] for a library paper, [E5] for an evidence "
    "excerpt, [C7] for a tracking candidate, [W3] for an outside work. Sentences without a marker are deleted "
    "automatically, so do not write uncited claims; say what is missing instead. Do not invent markers.")

REVIEW = Template(
    key="review",
    label=L("课题综述"),
    description=L("给一个题目：检索库内和库外文献，并行精读，写出带引用的综述，并提议漏读的论文和新缺口。"),
    lead_brief=(
        "Goal: a literature review of the topic below, written from the researcher's own library and checked "
        "against outside literature. A good plan: (1) one 'searcher' task that finds the relevant library papers "
        "and important outside works; (2) one 'reader' task per key library paper (at most {max_papers}), each "
        "blocked by the search, each returning the paper's design, main findings with page numbers and "
        "limitations as they bear on the topic; (3) one 'critic' task blocked by all readers that looks for "
        "disagreements, weak evidence, open gaps and missing key works; (4) one final 'writer' task blocked by "
        "everything that writes the review. {external}"),
    members={
        "searcher": Member("searcher", L("检索员"),
                           "You find the literature for the topic: search the library with several phrasings "
                           "(English terms work best), list fields, and search outside literature. Return a "
                           "ranked list of library papers (P ids) with one line each on why they matter, and "
                           "outside works (W ids) that look important but are missing. If an outside work is "
                           "clearly essential, file it with propose_collect.",
                           READ_LIB + ["external_search", "missing_key_papers", "propose_collect"], 8),
        "reader": Member("reader", L("精读员"),
                         "You read ONE paper closely for the topic. Use find_in_paper and read_pages; the card is "
                         "only a summary. Report design and sample, the findings that bear on the topic with "
                         "page numbers (p.N) and short verbatim quotes, and limitations. Cite the paper as [P#].",
                         ["read_card", "find_in_paper", "read_pages", "search_library"], 6),
        "critic": Member("critic", L("质疑员"),
                         "You are the sceptical reviewer. From the readers' reports and the existing research "
                         "questions, find contradictions between papers, findings that rest on weak designs or "
                         "small samples, conditions that make studies not comparable, and what is still missing. "
                         "File missing pieces with propose_gap (for an existing question) or propose_question; "
                         "file essential outside works with propose_collect. Cite [P#]/[E#]/[W#].",
                         READ_LIB + ["question_evidence", "external_search", "propose_gap", "propose_question",
                                     "propose_collect"], 6),
        "writer": Member("writer", L("执笔员"),
                         "You write the final review from your teammates' results: background, what the library "
                         "shows (grouped by theme), disagreements and their likely reasons, limitations, gaps "
                         "and recommended next reading. " + WRITER_RULES,
                         ["read_card", "find_in_paper"], 4),
    },
    params={"max_papers": 8, "external": True},
)

CHECK = Template(
    key="check",
    label=L("证据核对"),
    description=L("选一个或几个研究问题：找出库里相关的论文，摘出原句，找反证和可比性问题，写判断草稿。"),
    lead_brief=(
        "Goal: check the evidence for the research questions below. For each question: one 'searcher' task "
        "that reads question_evidence and searches the library for papers not yet excerpted; one 'reader' task "
        "per promising paper (at most {max_papers} per question), blocked by that search, which files verbatim "
        "excerpts with propose_evidence; one 'critic' task blocked by the readers that looks for counter-"
        "evidence, comparability problems and sample overlap and files gaps; finally one 'writer' task blocked "
        "by everything that drafts the judgment for each question with propose_judgment and writes a short "
        "report. Questions: {questions}"),
    members={
        "searcher": Member("searcher", L("检索员"),
                           "You find library papers that bear on a research question and are not yet excerpted "
                           "for it. Start with question_evidence, then search with several phrasings. Return a "
                           "ranked list of P ids, each with the page where the relevant text is.",
                           READ_LIB + ["question_evidence"], 8),
        "reader": Member("reader", L("精读员"),
                         "You read ONE paper for ONE research question and file excerpts with propose_evidence: "
                         "sentences copied verbatim from read_pages output (never paraphrased), the stance "
                         "(supports/opposes the question's hypothesis), the dimension (sample source, analysis "
                         "unit, methods, findings, limitations) and a one-sentence note. File 1–4 excerpts; "
                         "include opposing evidence and limitations when present. If a quote is refused, copy it "
                         "again exactly. Report what you filed, citing [P#].",
                         ["read_card", "find_in_paper", "read_pages", "question_evidence", "propose_evidence"], 8),
        "critic": Member("critic", L("质疑员"),
                         "You look for counter-evidence and weak points for a research question: opposing "
                         "results, studies that are not comparable (species, tissue, cohort, method), possible "
                         "shared datasets, and what evidence is still missing. File opposing excerpts with "
                         "propose_evidence and missing pieces with propose_gap. Cite [P#]/[E#].",
                         READ_LIB + ["question_evidence", "propose_evidence", "propose_gap"], 8),
        "writer": Member("writer", L("执笔员"),
                         "For each research question, draft the researcher's judgment with propose_judgment "
                         "(2–6 sentences, each citing [E#] excerpts or [P#] papers), then write a short report: "
                         "per question, what supports it, what opposes it, comparability caveats and gaps. "
                         + WRITER_RULES,
                         ["question_evidence", "read_card", "propose_judgment"], 5),
    },
    params={"max_papers": 6},
)

TRIAGE = Template(
    key="triage",
    label=L("新文献分诊"),
    description=L("把追踪到的新候选逐篇看一遍：对照研究问题和缺口，提议收录或跳过，说明理由和先读哪部分。"),
    lead_brief=(
        "Goal: triage the newest tracking candidates (up to {limit}) against the researcher's library, research "
        "questions and gaps. Split the candidates into 'screener' tasks of about 10 each (use offsets: the "
        "first task takes offset 0, the next offset 10, ...), all independent; then one final 'writer' task "
        "blocked by all screeners that writes a short triage note. Candidates available: {available}."),
    members={
        "screener": Member("screener", L("筛选员"),
                           "You screen tracking candidates (C ids) against the researcher's research questions, "
                           "gaps and library (library_overview, search_library). For each candidate in your "
                           "range decide: worth taking in (propose_take_in with the question or gap it serves and "
                           "what to read first), clearly irrelevant (propose_skip with a reason), or undecided "
                           "(leave it). Be strict: novelty or prestige alone is not a reason. Report your "
                           "decisions citing [C#].",
                           ["library_overview", "search_library", "read_card", "tracking_candidates",
                            "propose_take_in", "propose_skip"], 6),
        "writer": Member("writer", L("执笔员"),
                         "You write the triage note: which candidates to read first and why (grouped by research "
                         "question or gap), which were skipped as a group, and what the batch says about the "
                         "field. " + WRITER_RULES,
                         ["library_overview", "tracking_candidates"], 3),
    },
    params={"limit": 30},
)

TEMPLATES = {t.key: t for t in (REVIEW, CHECK, TRIAGE)}


def roster(t: Template) -> str:
    return "\n".join(f"- {m.key}: {m.brief[:220]}" for m in t.members.values())
