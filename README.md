<h1 align="center">PaperMap · 本地文献知识地图</h1>

<h3 align="center">把你读过的论文，变成一张会自己长大的知识地图</h3>

<p align="center">
  论文、卡片、笔记和对话都保存在你自己的电脑上。导入 PDF 后自动写卡片、分类、建学科页；读的时候和 AI 讨论原文，写的时候逐句核对引用。
</p>

<p align="center">
  <b>中文</b> | <a href="README.en.md">English</a>
</p>

<p align="center">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-已测试-1B54E8?style=for-the-badge&logo=apple&labelColor=20242D">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-34D399?style=for-the-badge&logo=python&logoColor=white&labelColor=20242D">
  <img alt="local first" src="https://img.shields.io/badge/数据-只存本机-E3B341?style=for-the-badge&labelColor=20242D">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/github/license/RongpengGong/PaperMap?style=for-the-badge&color=5319E7&labelColor=20242D"></a>
</p>

如果 PaperMap 对你有帮助，欢迎点右上角的 **⭐ Star**，也欢迎[赞助](#赞助)支持后续开发。

---

## 为什么做 PaperMap

读文献的人常常遇到这些问题：

* **读过就忘。** 读了几十篇，过几个月想不起哪篇说了什么、用的什么方法、样本多大。
* **知识是零散的。** 论文在 Zotero，笔记在 Obsidian，和 AI 的讨论留在网页里，彼此串不起来。
* **领域全貌要自己拼。** 一个方向有哪些研究范式、哪些结论互相矛盾、哪里还缺证据，只能从头梳理。
* **写作时心里没底。** 稿件里这句话后面引的那篇，原文真的这么说吗？

PaperMap 在你自己的电脑上运行。它把每篇论文读成一张结构化卡片，按研究范式和主题归类，为每个学科维护一个随新论文更新的页面，并且让每个结论都能点回 PDF 里的原句。

---

## 核心功能

### 1. 导入即整理
* **拖进来就行。** 把 PDF 拖进窗口，或者让 PaperMap 监听一个文件夹（包括 Zotero 的存储目录，只读不改）。
* **识别与去重。** 读出 DOI，到 Crossref / OpenAlex 核对；改名副本、预印本与正式版只保留一张卡片。
* **论文卡片。** 一句话结论、研究设计（样本、物种、平台）、方法与软件、主要发现、局限。卡片可以直接编辑，改坏了能退回任何一个历史版本。
* **多标签分类。** 方法学（单细胞、空间转录组、湿实验、临床队列……）× 主题（分类表可自定义），拿不准的进「待审核」。

### 2. 知识地图与学科
* **交互式知识地图**：学科、论文、研究问题之间的关系一目了然。
* **学科页**：代表文献、常用工具、方法进展、**相互矛盾的结论**；新论文进来自动追加，定期由模型整合。
* **新学科检测**：分类表覆盖不到的方向攒够几篇后提示你建立，并生成一本**学习手册**（A 部分只引用你库里的论文，B 部分是标明需核实的背景知识）。

### 3. 边读边想
* **内置 PDF 阅读器**：目录、搜索、高亮、批注、框选图表、本机 OCR、划词翻译、全文段落对照翻译。
* **AI 对话**：在论文旁边和任意已配置的模型讨论（ChatGPT / Claude 订阅可选推理强度），回答标页码、点击跳回 PDF；PDF 里选中一段即可「问 AI」。每个页面右侧都有「问 AI」抽屉。讨论结束后，要点经你确认才写进笔记。
* **右键即用**：在卡片里选中文字右键，直接高亮、改颜色、加粗；任何地方选中都能「收藏这句」。**收藏簿**每周由 AI 按主题归纳成知识点，每条标出处。
* **技能**：用「精读」等提示词技能深度解读一篇论文（不执行任何脚本）。

### 4. 证据与研究问题
* **证据笔记**：围绕研究问题摘录原句（锚定到页码和位置），标「支持 / 相反 / 待核对」，并排对照样本、方法和结论。
* **这篇论文改变了什么**：新论文进来时对照已有证据生成「更新卡」，两边都要引用原文，由你确认才生效。
* **证据缺口**：记下还缺什么证据；新论文可能补上时提醒你，并给出值得先读的段落。
* **预印本 → 正式版**：两个版本并存，旧摘录仍指向原页；改动或消失的段落会提示你核对。

### 5. 追踪新文献
* 每天从 **OpenAlex、PubMed、bioRxiv / medRxiv** 查找与你的学科、证据缺口、关注作者相关的新论文，以及引用了你库里论文的新文章。
* 关机几天后自动补查；同一篇被多个来源发现只出现一次，并写明推荐理由；每周一条系统通知简报。
* 「收进来」时自动寻找合法的开放获取 PDF，走正常的导入流程。

### 6. 写作与引用核对
* **写作**：选一组论文（星标、用途标签、分组、学科），模型只根据它们写一段带引用的文字，缺的证据单独列出；导出 Word / BibTeX。
* **引用核对**：上传 Word 稿件，逐句检查被引论文是否真的支持这句话：支持 / 部分支持 / 没找到 / 相反，每条给出原文句子和页码；给不出原句的判断不被采信。
* 另外还有**全库问答**、**全文搜索**、**对比表**、**数据资源表**（GEO / SRA 等登录号）、**引用网络**、**BibTeX / RIS 导出**。

### 7. 数据始终在你手里
* 知识库是普通的 **Markdown 文件夹**，可以直接用 [Obsidian](https://obsidian.md) 打开；状态保存在本机 SQLite。
* 每天自动备份数据库；可以把整个知识库每天**同步到移动硬盘或云盘文件夹**；删除的论文进回收站 30 天。
* **⌘K** 快速跳转、可折叠侧边栏、「今天读什么」、状态检查、简体 / 繁體 / English / Deutsch 界面。
* 本机只读 **MCP 服务**和插件接口：让 Claude Code 等工具查询你的证据（只监听 127.0.0.1）。

> **隐私**：论文文本（标题、摘要、关键章节）会发送到**你自己选择的**模型服务商。放在排除文件夹（默认 `_private`、`drafts`）里的 PDF 不会被读取或发送。PaperMap 不内置任何密钥，也不会把数据发给作者。

---

## 快速开始

### 安装

**macOS 应用**：从 [Releases](https://github.com/RongpengGong/PaperMap/releases) 下载 `PaperMap-macOS.zip`，解压后把 `PaperMap.app` 拖进「应用程序」。应用还没有经过 Apple 公证：第一次打开如果提示无法验证开发者，请到「系统设置 → 隐私与安全性」点「仍要打开」。

**用 Python 安装**（Python 3.10+）：

```bash
pip install "papermap[desktop] @ git+https://github.com/RongpengGong/PaperMap"
papermap            # 首次运行会创建配置并打开窗口
```

> Windows 版可以构建，但还没有在 Windows 上完整实测，欢迎反馈问题。

### 第一次使用（3 分钟）

1. **连接模型**：「设置 → AI 服务 → 模型与账号」。可以用 **ChatGPT 订阅**或 **Claude 订阅（本机 Claude Code）** 登录，也可以填 DeepSeek、Anthropic、OpenAI、Gemini、Kimi、智谱、通义等的 API Key，或任何 OpenAI / Anthropic 兼容的自定义服务。点「测试连接」看到 ✓ 再保存。
2. **导入论文**：把 PDF 拖进窗口；或在「设置 → 文献库 → 导入与文件夹」加一个自动导入文件夹（比如 Zotero 的 `storage`，只读不改）。一次导入 5 篇以上会先估算费用，等你确认。
3. **看「使用指南」**（左侧「更多」里），按 **⌘K** 随时跳转。

**费用**：以 DeepSeek 为例，一篇长文（卡片 + 分类）不到 1 美分；用 ChatGPT / Claude 订阅不另外计费。可以在设置里设每月预算。

更详细的说明（每个页面、翻译服务、插件与 MCP、命令行、开机自启、数据位置、常见问题）见 **[docs/USAGE.md](docs/USAGE.md)**。

---

## 开发

```bash
git clone https://github.com/RongpengGong/PaperMap && cd PaperMap
python -m venv .venv && .venv/bin/pip install -e ".[dev,desktop]"
.venv/bin/python -m pytest -q          # 400+ 个测试
.venv/bin/papermap serve --port 8799   # 只开网页，用浏览器调试
.venv/bin/pip install -e ".[build]" && .venv/bin/python packaging/build.py   # 打包桌面应用
```

设计文档和各阶段的决策记录在 [CLAUDE.md](CLAUDE.md)。欢迎提 Issue 和 Pull Request。

---

## 赞助

PaperMap 是利用业余时间开发的个人项目，免费开源。如果它帮你省下了时间，欢迎请作者喝杯咖啡，支持后续开发 ☕

<table align="center">
  <tr>
    <td align="center"><img src="docs/assets/sponsor/wechat.png" alt="微信支付" width="200"><br><sub>微信支付</sub></td>
    <td align="center"><img src="docs/assets/sponsor/alipay.jpg" alt="支付宝" width="200"><br><sub>支付宝</sub></td>
    <td align="center"><img src="docs/assets/sponsor/paypal.jpg" alt="PayPal" width="200"><br><sub>PayPal</sub></td>
  </tr>
</table>

---

## 致谢

* 元数据来自 [Crossref](https://www.crossref.org/) 与 [OpenAlex](https://openalex.org/)；新文献来自 OpenAlex、[PubMed](https://pubmed.ncbi.nlm.nih.gov/) 与 [bioRxiv / medRxiv](https://www.biorxiv.org/)；开放获取全文来自 [PMC Open Access](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/)。
* PDF 解析使用 [PyMuPDF](https://pymupdf.readthedocs.io/)，笔记编辑器基于 [Tiptap](https://tiptap.dev/)，桌面窗口基于 [pywebview](https://pywebview.flowrl.com/)。
* README 的结构参考了 [Agentero](https://github.com/poco-ai/Agentero)。

## 开源许可证

见 [LICENSE](LICENSE)。第三方组件及其许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
