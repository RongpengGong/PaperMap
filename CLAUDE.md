# PaperMap — 本地文献知识地图（项目说明）

> 给 Claude Code：这是本项目的完整需求与设计说明。开始工作前通读一遍，**从「阶段 1」开始做，做完一个阶段、通过验收后再进入下一阶段**。遇到本文没写清楚的设计选择，先问用户再动手。
> 对话用中文；代码、注释、变量名、commit message 用英文。

---

## 1. 这是什么

一个在**用户自己电脑上运行**的文献管家：

- **功能 1 — 新文献检测**：监听一个指定的「入口文件夹」，有 PDF 进来就提取 DOI。是新文章 → 处理并写进知识库；已经处理过 → 完全静默（只记日志）。
- **功能 2 — 子学科分类与知识地图**：用 LLM（DeepSeek）读论文，按研究范式/方法学做**多标签**分类（单细胞、空间转录组、湿实验机制、临床队列、预测模型……）。
  - 归入已有学科 → 增量更新该学科的知识地图页面；
  - 出现全新学科 → 先进入「待定区」，满足阈值并经用户确认后，生成一本新的**学习手册**。

**使用者**：作者本人单机使用；程序可分享给朋友，朋友在**各自电脑**上安装运行、用**各自的 API Key**。因此必须：安装简单、跨平台（macOS + Windows）、不内置任何个人配置或密钥、分类体系可定制。

---

## 2. 已确定的设计决策（不要推翻，有疑问先问）

| 决策 | 结论 |
|---|---|
| PDF 来源 | 入口文件夹负责接收本地 PDF（不管来自 Zotero 还是手动下载，不依赖 Zotero）；文献追踪（§16）负责发现候选；用户点「收进来」后，两者共用同一套去重和处理流程，**只有一个文献库** |
| 扫描方式 | 递归扫描子文件夹；`watchdog` 实时监听 + 启动时全量补扫（防止关机期间漏掉） |
| 原文件 | **默认绝不移动、重命名、修改**入口文件夹里的 PDF（会破坏 Zotero 链接）。归档复制为可选功能 |
| 去重主键 | 规范化 DOI（小写、去掉 `https://doi.org/` 前缀）；无 DOI 时兜底用文件 SHA-256 + 标题模糊匹配 |
| 知识存储 | **纯 Markdown 文件夹**（数据本体）+ SQLite（状态与索引）。兼容 Obsidian 打开，但**不依赖** Obsidian |
| 展示界面 | 程序自带**本地网页**（浏览器打开），普通用户无需安装任何笔记软件 |
| LLM | DeepSeek，走 **OpenAI 兼容接口**（`base_url` + `api_key` + `model` 均可配置），方便换其他模型 |
| PDF 解析 | 默认 **PyMuPDF**（纯 Python，零外部依赖）；GROBID 仅作可选增强，不能成为必需依赖 |
| 元数据 | Crossref + OpenAlex（免费 API），优先于 LLM 抽取元数据 |
| 分类 | 多标签、两个维度（方法学轴 + 主题轴）；种子分类表可编辑；新类别需阈值 + 用户确认 |
| 语言 | Python ≥ 3.10 |

---

## 3. 技术栈

- 文件监听：`watchdog`
- PDF：`pymupdf`
- HTTP：`httpx`（带重试、超时、限速）
- LLM：`openai` SDK（指向 DeepSeek 的 base_url）
- 数据库：SQLite（标准库 `sqlite3`，或 `sqlmodel`，二选一保持简单）
- 配置：YAML（`pyyaml` 或 `pydantic-settings`）
- Web：`FastAPI` + `uvicorn`；前端尽量轻（Jinja2 模板 + 一个网络图库，如 vis-network 或 Cytoscape.js，静态文件随包分发，**不依赖 CDN**，离线可用）
- 嵌入向量（阶段 3 用于类别去重）：优先用 LLM 提供方的 embedding 接口；若不可用再考虑本地小模型，作为可选依赖
- 打包：先 `pyproject.toml`，支持 `pip install` / `uv tool install`，命令行入口 `papermap`；稳定后再做 PyInstaller
- 测试：`pytest`

---

## 4. 目录结构

### 4.1 代码仓库（就在当前文件夹）

```
claude_test/                 # 当前工作目录 = 项目根
├── CLAUDE.md                # 本文件
├── pyproject.toml
├── src/papermap/
│   ├── __init__.py
│   ├── cli.py               # papermap init / run / scan / serve / reprocess
│   ├── config.py            # 读取/校验配置
│   ├── watcher.py           # watchdog 监听 + 防抖 + 启动补扫
│   ├── pdf_extract.py       # PyMuPDF 文本/元数据提取
│   ├── doi.py               # DOI 提取、规范化、校验
│   ├── metadata.py          # Crossref / OpenAlex 查询与缓存
│   ├── registry.py          # SQLite 状态库
│   ├── llm.py               # LLM 客户端封装（重试、JSON 校验、token 计费记录）
│   ├── extract.py           # 论文结构化抽取（prompt + schema）
│   ├── classify.py          # 多标签分类 + 新类别检测（阶段 2/3）
│   ├── kb_writer.py         # 写 Markdown 知识库
│   ├── handbook.py          # 学习手册生成（阶段 3）
│   ├── web/                 # FastAPI 应用、模板、静态资源
│   └── prompts/             # 所有 prompt 模板（.md / .txt），带版本号
├── taxonomies/
│   ├── default.yaml         # 通用起步分类表
│   └── liver_omics.yaml     # 作者本人方向的预设（见 §8）
├── tests/
│   └── fixtures/            # 测试用的小 PDF / 模拟 API 响应
└── inbox/                   # 开发期间的入口文件夹（见 §11）
```

### 4.2 用户数据（位置可配置，默认在用户主目录下 `PaperMap/`）

```
PaperMap/
├── config.yaml
├── papermap.db              # SQLite
├── logs/
└── knowledge/               # Markdown 知识库（可直接作为 Obsidian vault 打开）
    ├── papers/              # 每篇一张卡片：2024_Smith_NatGenet_<doi-slug>.md
    ├── fields/              # 每个子学科一个 hub 页（知识地图节点）
    ├── handbooks/           # 新学科学习手册
    ├── concepts/            # 方法 / 工具 / 概念页（阶段 4）
    └── _pending/            # 待定新类别
```

> 用户数据与代码分离，升级程序不会动知识库；知识库整个拷走即可迁移/备份。建议对 `knowledge/` 自动 `git init` 做版本管理（可选，阶段 3）。

---

## 5. 配置文件（config.yaml 示例）

```yaml
inbox_dirs:
  - ~/Documents/claude_test/inbox
data_dir: ~/PaperMap
exclude:
  dir_names: ["_private", "drafts"]      # 这些子目录永不处理（保护未发表稿件）
  filename_patterns: ["*draft*", "*_SI.pdf", "*supplement*"]
llm:
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY           # 优先读环境变量；网页设置页也可写入本地配置
  model: deepseek-chat                    # 模型名可能更新，以 DeepSeek 官方文档为准，不要写死在代码里
  max_input_chars: 60000
  monthly_budget_usd: 5                   # 超出后暂停 LLM 调用并提示
output_language: zh                       # zh / en / bilingual
metadata:
  contact_email: ""                       # Crossref/OpenAlex polite pool 用
taxonomy_file: taxonomies/default.yaml
new_field_threshold: 3                    # 同一新类别攒够 N 篇才提议建立
web:
  host: 127.0.0.1
  port: 8765
```

`papermap init` 应交互式生成此文件（入口文件夹、数据目录、API Key、分类表预设、输出语言）。**仓库里绝不提交任何真实 Key。**

---

## 6. 功能 1：新文献检测流程

```
新文件事件 / 启动补扫
  → 排除规则过滤（exclude、非 PDF、隐藏文件、临时下载文件 .crdownload/.part）
  → 防抖：等文件大小连续 2–3 秒不变再处理
  → 计算 SHA-256；哈希已在库 → 记日志「同一文件/新路径」，结束
  → 提取 DOI（见下）
  → DOI 已在库 → 记录新路径，静默结束
  → 查 Crossref / OpenAlex 补全元数据，并做「同一作品」二次去重（预印本 vs 正式版）
  → 新论文 → 进入处理队列（LLM 抽取 → 分类 → 写知识库）
```

### DOI 提取规则（按优先级）

1. PDF 元数据（`doi`、`prism:doi`、`dc:identifier` 等 XMP 字段）
2. **只看前 1–2 页**正文的 DOI 正则匹配（**不能扫全文**：参考文献里有几十个 DOI）
3. 文件名中的 DOI 线索（例如 Nature 系列文件名 `s41586-026-11234-8.pdf` 对应 `10.1038/s41586-026-11234-8`）——只作为候选，必须经校验
4. 以上都没有 → 取首页标题，用 Crossref `query.bibliographic` 反查

**校验**：候选 DOI 必须能在 Crossref/OpenAlex 解析到，且返回标题与 PDF 首页标题模糊匹配（如 rapidfuzz ratio ≥ 85）。校验失败的标记为 `doi_unverified`，进入网页的「待审核」列表，不要硬处理。

DOI 正则注意：去掉结尾的标点 `.,;)]`，统一小写。

### 预印本 / 正式版

用 OpenAlex 的 work ID 或 Crossref 的 `relation`（`is-preprint-of` / `has-preprint`）关联；同一作品只保留一张卡片，卡片里列出所有 DOI。

### 无 DOI 的 PDF

学位论文、老文献、会议摘要：用 SHA-256 + 标题模糊匹配去重，正常处理，卡片标注「无 DOI」。

### 补充材料 / 非论文 PDF

文件名或首页特征判断（Supplementary Information、Reporting Summary 等），标为 `skipped_supplement`，不调用 LLM。

---

## 7. SQLite 表设计（起点，可调整）

```
papers(
  id INTEGER PK, doi TEXT UNIQUE NULL, openalex_id TEXT NULL,
  title TEXT, year INT, journal TEXT, first_author TEXT,
  status TEXT,          -- new / extracting / done / doi_unverified / skipped_supplement / error
  kb_path TEXT,         -- 知识库卡片路径
  prompt_version TEXT, model TEXT,
  created_at, updated_at
)
files(id PK, paper_id FK NULL, path TEXT, sha256 TEXT UNIQUE, size INT, mtime REAL, seen_at)
paper_dois(paper_id FK, doi TEXT, kind TEXT)   -- 正式版 / 预印本
fields(id PK, slug TEXT UNIQUE, name_zh, name_en, axis TEXT, status TEXT)  -- axis: method / topic; status: active / pending / rejected
paper_fields(paper_id, field_id, confidence REAL, is_primary BOOL)
llm_calls(id PK, paper_id, purpose, model, prompt_tokens, completion_tokens, cost_usd, created_at)
events(id PK, level, message, paper_id NULL, created_at)   -- 网页「活动日志」
```

`prompt_version` 用于以后改了 prompt 后执行 `papermap reprocess --prompt-version <old>` 批量重跑。

---

## 8. 功能 2：分类设计

### 两个维度、多标签

- **方法学轴（method）**：决定知识地图的主结构。
- **主题轴（topic）**：作为交叉标签，用于发现「空间组学 × CCA」这类交集。

一篇论文可以有多个方法标签，其中一个标记为 `is_primary`。

### 种子分类表（taxonomies/*.yaml）

`default.yaml` 应是通用的生物医学研究范式列表；`liver_omics.yaml` 是作者本人的预设，起点参考：

- method：单细胞转录组（scRNA-seq）、单细胞多组学（ATAC/CITE 等）、空间转录组（Visium/Xenium/CosMx/Stereo-seq 等）、空间蛋白组/多重成像（IMC、CODEX、MACSIMA 等）、湿实验机制研究（细胞/动物模型）、临床队列/流行病学、预测模型/机器学习、孟德尔随机化与遗传流行病学、Meta 分析/系统综述、综述、方法学/工具开发
- topic：HCC、CCA、PSC、MASLD/MASH、肝纤维化、肿瘤微环境、神经-免疫互作、代谢综合征、衰老生物学

每个类别包含：`slug`、`name_zh`、`name_en`、`axis`、`description`（给 LLM 判别用）、`synonyms`（例如 single-cell / scRNA-seq / 单细胞）。

### 分类流程

1. 先用 OpenAlex topics/concepts、PubMed MeSH（若能取到）作为先验，放进 prompt。
2. LLM 只能从现有类别中选，并给出 confidence；若确实都不合适，可**提议**新类别（返回名称 + 描述 + 理由）。
3. confidence 低于阈值 → 进入网页「待审核」，不硬分。

### 新类别（防止「学科爆炸」）

- 提议的新类别先和现有类别及其 synonyms 做名称归一 + embedding 相似度比较，相似的直接并入已有类别。
- 不相似 → 进 `_pending/`，累计到 `new_field_threshold` 篇后在网页上提示用户「建立 / 合并到某类 / 拒绝」。
- 用户确认后才正式建立该类，并触发学习手册生成。

---

## 9. 知识库内容

### 9.1 论文卡片 `papers/*.md`

YAML frontmatter（doi、title、authors、year、journal、fields、topics、file_paths、added）+ 正文：

- 一句话结论
- 研究问题
- 研究设计 / 数据（样本量、物种、组织、平台、队列）
- 核心方法与工具（软件包名单独列出，便于以后做概念页）
- 主要发现（3–5 条）
- 局限性
- 与我相关的点（可留空给用户填写，程序更新时**不得覆盖**这一节）
- 链接到所属学科：`[[fields/spatial-transcriptomics]]`

LLM 抽取用 JSON Schema 约束输出，解析失败自动重试一次，仍失败则标记 `error` 并保留原始返回便于调试。输入只给标题、摘要、方法、结果、图注等关键部分，控制在 `max_input_chars` 以内（有的 PDF 很大，例如测试文件之一约 80 MB，以图片为主，要处理好）。

### 9.2 学科 hub 页 `fields/*.md`（知识地图节点）

固定分区，**新论文只往对应分区追加，不让 LLM 整页重写**：

- 概述（建立时生成）
- 代表文献（自动追加，按年份）
- 常用方法与工具
- 方法变体与新进展
- 争议 / 不一致的结论
- 与其他学科的交叉（主题轴统计）

每累计 N 篇（默认 10）触发一次「整合」：LLM 重写概述与进展小结，旧版本由 git 保留可回滚。用户手写的内容放在 `<!-- user -->` 标记区域内，程序永不改动。

### 9.3 学习手册 `handbooks/*.md`

新学科确认后生成，**明确分成两部分并标注来源**：

- **A. 来自你的文献库**（每条带 DOI 引用）：该领域在你库中的代表研究、用到的方法、主要发现
- **B. 背景知识补充（LLM 生成，需核实）**：核心概念、标准分析流程、常用工具包、常见陷阱、建议阅读顺序

### 9.4 溯源

所有结论尽量回链到 DOI（能做到页码更好）。不允许生成没有来源的「事实」写进 A 部分。

---

## 10. 本地网页（`papermap serve`）

- **首页**：最近新增论文、处理队列状态、本月 token 花费
- **知识地图**：交互式网络图（节点 = 学科 / 论文，可切换只看学科；点击跳到对应页面）
- **学科页 / 手册页 / 论文卡片**：渲染 Markdown
- **待审核**：DOI 未验证、低置信分类、待建新类别（按钮：确认 / 合并 / 拒绝）
- **设置**：入口文件夹、API Key（只存本地）、模型、输出语言、预算、排除规则、分类表编辑
- **活动日志**

只监听 `127.0.0.1`。

---

## 11. 测试数据

当前文件夹里已有 3 个用户提供的 PDF（Nature 系列，文件名即 DOI 后缀）：

- `s41586-026-11234-8.pdf`（约 83 MB）
- `s41588-026-02779-5.pdf`（约 16 MB）
- `s41698-026-01718-1_reference.pdf`（约 8 MB；文件名带 `_reference`，需确认它是正文还是参考/补充材料——这是个很好的边界测试）

开发时建议在项目根建 `inbox/` 作为入口文件夹。**移动或复制这些 PDF 之前先问用户**；更推荐的做法是在 config 里直接把入口文件夹指向现有位置，或者复制一份到 `inbox/`。

**大 PDF 不要提交进 git**：`.gitignore` 加入 `*.pdf`、`inbox/`、数据目录、`.env`。`tests/fixtures/` 里用自己生成的小 PDF（例如用 PyMuPDF 造一个带 DOI 的单页 PDF、一个参考文献里有多个 DOI 的 PDF、一个无 DOI 的 PDF）。

需要补的边界测试用例（可请用户再提供）：一篇 bioRxiv 预印本 + 其正式发表版、一篇无 DOI 的学位论文、一份补充材料 PDF、一个重复文件（同内容不同文件名）。

---

## 12. 开发阶段与验收标准

### 阶段 1 — MVP：检测 + 去重 + 论文卡片（先做这个）

- [ ] `pyproject.toml`、包结构、`papermap` 命令行入口
- [ ] `papermap init` 生成配置
- [ ] `doi.py`：提取、规范化、校验，带单元测试（参考文献 DOI 不能被误取）
- [ ] `metadata.py`：Crossref/OpenAlex 查询，本地缓存，失败可重试；测试中用模拟响应
- [ ] `registry.py`：SQLite 建表与读写
- [ ] `watcher.py`：监听 + 防抖 + 启动补扫 + 排除规则
- [ ] `llm.py` + `extract.py`：结构化抽取，JSON 校验，记录 token 用量
- [ ] `kb_writer.py`：生成论文卡片 Markdown
- [ ] `papermap scan`（一次性扫描）与 `papermap run`（常驻监听）
- [ ] 最简网页：论文列表 + 卡片查看 + 设置 API Key

**验收**：把 3 个测试 PDF 放进入口文件夹 → 每篇正确识别 DOI 并生成卡片；再放一个同名副本或重命名副本 → 不重复处理、不调 LLM；重启程序 → 不重复处理；无 Key 时给出清晰提示而不是崩溃。

### 阶段 2 — 分类 + 学科 hub 页

- [ ] 分类表 YAML 加载与校验；`init` 时选择预设
- [ ] `classify.py`：多标签、confidence、低置信进待审核
- [ ] 学科 hub 页按分区追加
- [ ] 网页：知识地图网络图、学科页、待审核页

**验收**：测试论文被归入合理的方法/主题类别；hub 页自动出现代表文献；改分类表后 `reprocess` 能重新分类。

### 阶段 3 — 新学科检测 + 学习手册

- [ ] 新类别提议 → 归一/相似度去重 → `_pending/` → 达阈值提示用户
- [ ] 网页确认/合并/拒绝
- [ ] `handbook.py`：A/B 两部分手册
- [ ] 知识库 git 自动提交（可选）

### 阶段 4 — 打磨与分发

- [ ] 定期整合 hub 页；概念/工具页；跨论文关联（同方法不同结论等）
- [ ] 首次批量导入：先估算费用，用户确认后再跑；支持断点续跑
- [ ] 预算上限与费用面板
- [ ] 开机自启（macOS launchd / Windows 计划任务）说明或命令
- [ ] Windows 实测；PyInstaller 打包；README（中英文）面向非技术用户

---

## 13. 约束与原则

- **隐私**：只有通过排除规则的 PDF 内容才会发给 LLM；在 README 和首次启动时明确告知用户「论文文本会发送到你配置的 LLM 服务商」。
- **省钱**：元数据能从 Crossref/OpenAlex 拿就不用 LLM；只给 LLM 关键章节；已处理的论文绝不重复调用（除非显式 `reprocess`）。
- **鲁棒**：网络失败、API 限流、PDF 损坏、超大 PDF 都不能让常驻进程崩溃；错误写进 `events`，在网页上可见。
- **幂等**：任何步骤中断后重跑，结果一致，不产生重复卡片。
- **不覆盖用户内容**：卡片的「与我相关的点」、hub 页 `<!-- user -->` 区域永不改写。
- **可配置而非写死**：模型名、API 地址、分类表、语言、阈值都在配置里。
- 外部 API（DeepSeek 模型名与定价、OpenAlex 是否需要 key、Crossref 限速规则）以**当时的官方文档为准**，实现前先查一下。
- 小步提交：每完成一个模块跑测试再 commit。

---

## 14. 阶段 5 — 本地桌面应用（2026-11-23 与用户讨论定稿）

> 阶段 1–4 已完成。用户希望 PaperMap 是**本地 app**，不是在浏览器里打开的网页。参考了 [Agentero](https://github.com/poco-ai/Agentero)（Tauri 桌面应用），但它太复杂；我们保留 Python 技术栈，用 **pywebview** 实现原生窗口。以下决定替代 §2 / §10 里「浏览器打开本地网页」的设定，其余原则不变。

### 14.1 应用形态
- 双击 PaperMap 弹出**自己的原生窗口**（pywebview：macOS 用 WKWebView，Windows 用 Edge WebView2），有 Dock / 任务栏图标、应用菜单、⌘Q 退出。
- 关闭窗口后台继续监听；点 Dock 图标或再次打开应用 → 窗口重新出现。登录自启时只在后台运行、不弹窗口。
- 界面改成**桌面软件布局**：左侧固定侧边栏（论文 / 知识地图 / 学科 / 手册 / 工具 / 待审核 / 费用 / 设置），右侧内容区；论文列表点选后右侧直接显示卡片（列表 + 详情双栏），不再整页跳转。
- 系统集成：原生「选择文件夹 / 选择文件」对话框；处理完成弹系统通知；外部链接（DOI 等）在系统浏览器打开。
- 后端逻辑（检测、分类、知识库、费用）与数据目录 `~/PaperMap` 不变；内部仍用本地 HTTP 服务给窗口提供页面（只监听 127.0.0.1）。
- **阶段 1–4 的全部功能保留**，只换外壳和布局：知识地图、多标签分类、学科 hub 页、定期整合（争议 / 进展）、新学科检测与待审核、学习手册、工具页、分类表编辑与重新分类、费用与预算。

### 14.2 导入方式
- **拖拽导入（主要方式）**：用户把自己下载好的 PDF 拖进应用窗口（或 Dock 图标）→ 复制进文献库并处理。
- 「自动导入文件夹」（原入口文件夹监听，含 Zotero 目录）保留为可选功能；这些文件夹里的原文件仍然**绝不移动、改名、修改**。

### 14.3 文献库结构：每篇论文一个文件夹
```
knowledge/papers/<year>_<Author>_<Journal>_<doi-slug>/
├── <同名>.pdf              原文副本（macOS APFS 克隆，不额外占空间；其他系统为普通复制）
├── <同名>.md               我们生成的论文卡片
├── <同名>.translation.md   段落对照翻译（可选）
└── <同名>.notes.md         用户自己的笔记（程序永不改写）
```
- 文件名用同名前缀，保证 Obsidian 里 `[[...]]` 链接唯一。
- 已有的平铺卡片需要迁移进各自的文件夹（数据库 kb_path 同步更新，hub / 手册链接自动重建）。
- 代价（已向用户说明并接受）：在 Zotero 里做的批注不会出现在副本上。

### 14.4 翻译模块
- **翻译服务可以添加多个**，每个服务可选模式：翻译 / 润色 / 自定义 prompt。
  - **bilibili Index-Translate（内置、默认）**：官方免费接口 `https://index-translate.bilibili.com/v1`，OpenAI 兼容，**无需 Key**；模型 `Index-Translate-35B-A3B` / `-9B` / `-2B`；整篇长文可用长文档模型 `Index-Nailong-9B`（术语、人名前后一致）。2026-11-23 实测可用。官方说明「将来是否免费未知」。
  - **用主模型翻译**：复用已配置的 LLM（官方 / 自定义）。
  - **自定义翻译 API**：Base URL、Key、四种协议、模型名（与主模型的自定义配置一致）；为以后接其他翻译接口留口子。
- **不要** Agentero 的预设模型格式：模型配置一律沿用我们现有的「官方 / 自定义 + 计费」体系。
- **术语表**：利用 Index-Translate 的术语强制对齐，把分类表同义词、卡片里的工具名、基因名等作为术语表传入。
- **自动备用**：翻译服务出错或停用时，回退到主模型。
- 隐私：使用 bilibili 免费服务时原文会发到 B 站服务器，设置页和首次使用时要明确提示。
- 第一步输出：段落对照的全文翻译 Markdown（一段原文 + 一段译文），存进该论文的文件夹。

### 14.5 技能（Skill）接口
- 技能 = 一个文件夹，格式与 Claude / Agentero 技能一致：`SKILL.md`（含 frontmatter）+ `references/*.md` 等参考文档。用户可以把已有技能文件夹拖进 PaperMap。
- **只支持纯提示词技能**：技能里带脚本时不执行，提示「需要运行脚本，不支持」（安全底线：不运行来路不明的代码）。
- **技能来源**：程序自带的技能 + 自动发现用户已装的 Claude 技能（`~/.claude/skills`）+ 用户添加到 `~/PaperMap/skills` 的技能。
- **默认技能**：已安装 nature-paper-xray（精读 X 光）时用它，它和卡片互补：卡片讲「说了什么」，X 光讲「为什么这么做、证据撑到哪」。**它的许可证无法确认（目录里没有 LICENSE，也查不到上游仓库），所以不随程序分发**。程序自带一个我们自己写的「精读」技能（papermap-close-reading），作为没有 X 光时的默认。
- 用法：论文详情里「用技能解读」，输入全文（不受卡片的 6 万字符上限限制），用主模型，结果存为该论文文件夹里的 `<同名>.<技能名>.md`；默认手动触发，设置里可开「新论文自动解读」。费用计入费用页。
- 以后可把 nature-paper-card 的「结论 ↔ 证据对照表」和「研究想法」改写成纯提示词版本，作为第二个技能。

### 14.6 不做的（Agentero 有、我们不要）
ACP 连接外部 Agent、MCP 服务、RSS / arXiv 每日推荐 / Cool Papers、S3 同步、Skill 市场、预设模型。
> 2026-11-23 更新：泛泛的「每日推荐」仍不做；**围绕自己文献库的新文献追踪**改为要做，见 §16 第 6 项。

### 14.7 阶段划分
- **5a**：原生窗口 + 侧边栏界面 + 拖拽导入 + 每篇一个文件夹（含迁移）。
- **5b**：翻译服务与全文对照翻译（含术语表、备用）；技能接口 + 默认 X 光技能。
- **5c**（第二步）：应用内 PDF 阅读器（目录、搜索、高亮批注）+ 划词翻译；可选从 Zotero 导入（保留标签和笔记）。

**验收（5a + 5b）**：双击应用出现独立窗口而非浏览器标签；把 PDF 拖进窗口后，几十秒内在列表中出现，生成卡片与翻译，文件夹结构如 14.3；关闭窗口后拖到入口文件夹的 PDF 仍被处理；Windows 构建可启动（需在 Windows 上实测）。

---

## 15. 阶段 6 — 用好已积累的知识（2026-11-23 用户确认全部要做）

按依赖顺序实现，每项单独测试、提交：

1. **全库全文搜索**：SQLite FTS5（trigram，中英文都能搜）索引 PDF 每页正文、卡片、笔记、批注、译文、技能输出；结果按论文分组，正文命中可直接在阅读器跳到对应页。不花 LLM 费用。
2. **向文献库提问**：先用全文索引找相关段落和卡片，再让模型只根据这些材料回答，每句标出处（作者年份 + DOI，正文命中带页码）；没有出处的内容不写。
3. **阅读状态**：未读 / 在读 / 已读、星标；列表可筛选。
4. **导出参考文献**：选中论文或整个分组导出 BibTeX / RIS（元数据来自 Crossref）。
5. **数据资源表**：从全文提取 GEO / EGA / ArrayExpress / SRA / CNGB / PRIDE 等登录号、代码仓库（GitHub/Zenodo）；正则提取 + 卡片信息，不额外花钱；可按学科/分组筛选、导出 CSV。
6. **对比表**：选几篇或一个学科，生成队列/样本量/平台/方法/结论/局限对比表（来自卡片，必要时 LLM 归纳），导出 Markdown/CSV。
7. **引用网络**：OpenAlex 的参考文献与被引；库内互引画进知识地图；「被你库里多篇论文反复引用但你还没收」的漏读提醒；新引用提醒。
8. **笔记互链**：笔记里 `[[` 选论文/学科，论文页显示反向链接。
9. **图片库**：提取每篇论文的图和图注，可浏览；用支持看图的模型讲解单张图。
10. **从 Zotero 导入**：读取 Zotero 本地库（只读副本），带入标签、分组和笔记；不改 Zotero 数据。

**状态（2026-11-23）**：第 6 阶段 10 项全部完成并安装（109 个测试通过）。尚未做：Windows 实测、正式代码签名、无 PDF 的 Zotero 条目（需要全文才能生成卡片）。

---

## 16. 阶段 7 — 这篇论文改变了什么（2026-11-23 用户确认范围和顺序）

### 16.1 贯穿全阶段的规则
**发现论文、模型提出判断、用户确认知识变化，是三件不同的事**，数据和界面上都要分开：
- 追踪到一篇相关**摘要** → 只能提醒用户去读；
- 拿到**全文**并定位到原句 → 才能生成更新卡（状态「待核对」）；
- 用户**确认或改写**后 → 这条判断才进入学科页的「最近确认的变化」。

### 16.2 任务（按顺序，每项单独测试、提交、重装）
1. **自动备份**：每天备份数据库和设置，保留最近 N 份，设置页可恢复。
   - 区分「恢复数据库和设置」与「整库迁移」：卡片、笔记、PDF 副本在 `knowledge/`，不在备份里；恢复后要能说明哪些论文的 PDF / 卡片 / 笔记能找到，哪些找不到。
   - 数据库是 SQLite WAL，必须用 SQLite **在线备份接口**，不能直接复制文件。
   - 验收：在临时库里真正恢复一次，再点一条旧摘录，能回到原句。
2. **研究问题上地图**：问题挂到学科（可挂多个），在知识地图显示；PaperMap 从学科争议和卡片提议问题，用户确认后生效。
3. **更新卡：这篇论文改变了什么**：按研究问题找相关旧证据；影响类型固定：
   - **未改变当前判断**（必须有这个选项，防止模型为了“新意”硬找变化）、又一篇支持证据、**本库在这个问题下收录的首条干预证据**（不写“首次”）、结论相反、扩展到新人群/物种/组织、方法改进；
   - 新旧结论比较时，**两边都要有原文锚点**；研究对象或条件不同时，可标「可比性待核对」；
   - 每句可点回 PDF 原句；默认「待核对」，用户确认 / 改写 / 保留原判断；学科页加「最近确认的变化」。
4. **整合不再只取最新 40 篇**：现在按发表年份排序，刚导入的经典老论文也可能排不进。改为分别考虑「最近入库」与「最近发表」，并给用户确认的关键证据、相反证据留固定名额。
5. **还缺什么证据**：问题下可留缺口；导入新论文时提醒「这篇可能回答了你的问题」，并给出值得先读的段落。
6. **新文献追踪**：学科自动订阅 + 引用了库内论文的新文献 + 关注作者；来源 PubMed、bioRxiv/medRxiv、OpenAlex；免费打分 + 前几十篇模型精排；「收进来」时找合法 OA PDF，然后走与入口文件夹相同的去重和处理流程；第 5 项的缺口可变成检索式。
   - 「每天一次」要可验证：关机三天、第四天打开，能从**各来源上次成功的位置**补查；同一篇被两个来源发现只出现**一个候选**，并能看到被推荐的理由。
   - 估时最不确定：先用一个来源跑通「发现 → 去重 → 收进来 → 匹配研究问题」，再接另外两个。
7. **用途标签与导出**：用途标签、侧边栏星标分组、按标签导出 BibTeX（复用现有星标筛选和导出）。星标表达“我关心这篇”，只影响展示和阅读顺序，**不影响证据强弱判断**。

### 16.3 端到端验收场景：预印本后来出了正式版
两个来源发现同一篇预印本 → 只提醒一次；用户读完并确认一条证据；几个月后正式版换了页码、改了结论 → PaperMap 把两个版本关联起来，**保留旧摘录指向的原页**，并提示用户核对发生变化的段落。这个场景跑通，第 2–6 项才算连成一件事。

**状态**：第 1 项（自动备份）已完成，在真实文献库的临时副本上验收过「备份 → 删摘录 → 恢复 → 摘录回到原句」。第 2 项（研究问题上地图）已完成：问题可挂多个学科、显示在地图和学科页；模型提议的问题需确认才生效。其余进行中。
