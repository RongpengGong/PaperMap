# PaperMap · 本地文献知识地图

[English](README.en.md)

PaperMap 在你自己的电脑上运行。你把论文 PDF 放进一个文件夹，它会自动：

1. **识别新论文**：读出 DOI，到 Crossref / OpenAlex 核对，重复的文件（改名、换位置、预印本 vs 正式版）不会重复处理；
2. **写论文卡片**：一句话结论、研究设计、方法与软件、主要发现、局限性；
3. **按研究范式分类**：单细胞、空间转录组、湿实验机制、临床队列、预测模型……（多标签，分类表可以自己改）；
4. **画知识地图**：每个学科一个页面，汇总代表文献、常用工具、方法进展、不同论文之间相互矛盾的结论；
5. **发现新方向**：遇到分类表覆盖不到的方向，攒够几篇后问你要不要建立新学科，并生成一本**学习手册**。

所有内容都是普通的 Markdown 文件，可以直接用 [Obsidian](https://obsidian.md) 打开，也可以只用 PaperMap 自带的网页。

> **隐私提示**：每篇新论文的文本（标题、摘要、关键章节）会发送到**你自己选择的** AI 模型服务商（例如 DeepSeek）。放在被排除的子文件夹（默认 `_private`、`drafts`）里的 PDF 永远不会被读取或发送。PaperMap 不内置任何密钥，也不会把数据发给作者。

---

## 安装

### 方式一：直接下载（推荐给不写代码的用户）

1. 下载对应系统的压缩包（`PaperMap-Windows` 或 `PaperMap-macOS`），解压到任意位置。
2. **macOS**：把 `PaperMap.app` 拖进「应用程序」文件夹，双击打开。第一次打开如果提示「无法验证开发者」，请在「系统设置 → 隐私与安全性」里点「仍要打开」；如果提示要访问「文稿」或「下载」文件夹，点「允许」（PaperMap 只读取你指定的入口文件夹）。
   **Windows**：双击 `PaperMap\PaperMap.exe`。
3. 浏览器会自动打开 PaperMap 的设置页。PaperMap 没有自己的窗口：它常驻在**菜单栏（macOS 右上角）/ 任务栏通知区域（Windows 右下角）**，点那个小图标可以「打开 PaperMap」「打开知识库文件夹」「登录时自动启动」「退出」。再次双击应用也只会打开浏览器，不会重复启动。

### 方式二：用 Python 安装（需要 Python 3.10 或更新版本）

```bash
pip install "papermap @ git+<仓库地址>"     # 或在源码目录里：pip install .
papermap                                  # 首次运行会自动创建配置并打开浏览器
```

也可以用 `uv tool install .` 安装。

---

## 第一次使用（3 分钟）

1. **连接模型**：打开「设置 → AI 服务 → 模型与账号」，选择一种方式：
   - **ChatGPT 订阅**：点击「Continue with ChatGPT」，在系统浏览器里授权 PaperMap 使用套餐。返回设置页，选择账号可用的模型，再选「推理强度」并保存。档位随模型变化，也可以保留「模型默认」。适用资格和额度见 [OpenAI 说明](https://developers.openai.com/siwc/quickstart)。换电脑时重新授权。
   - **Claude 订阅**：先安装[官方 Claude Code](https://code.claude.com/docs/en/quickstart)。选择「Claude 订阅（Claude Code）」，用本机官方程序登录个人订阅，选定模型和推理强度后保存。两家的强度设置各自保存。论文文本由 Claude Code 发送给 Anthropic；套餐及额外用量以账号设置为准。
   - 选 **DeepSeek 官方**：只要粘贴 Key（在 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 创建），选模型即可。
   - 或选 **自定义**：填 Base URL、API Key、协议（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages / Gemini API）和模型名。点「获取模型列表」可以直接选。适合中转服务、其他厂商或本地模型。
   - 点「**测试连接**」，看到 ✓ 再保存。
2. **选入口文件夹**：打开「设置 → 文献库 → 导入与文件夹」。默认是 `~/PaperMap/inbox`。可以改成：
   - Zotero 的存储目录（通常是 `~/Zotero/storage`）——PaperMap **只读不改**，不会移动或重命名任何文件，不会破坏 Zotero 的链接；
   - 浏览器的下载文件夹，或任何你放论文的地方。可以填多个。
3. **放 PDF**：把论文放进入口文件夹（子文件夹也会被扫描）。几十秒后首页就会出现，卡片生成后可以在「知识地图」「学科」里看到。

一次放很多篇（默认 5 篇以上）时，PaperMap 会先**估算费用**，在首页等你点「开始处理」；中途关掉也没关系，下次会从断点继续。

---

## 日常使用

| 页面 | 做什么 |
|---|---|
| 论文 | 所有论文和处理状态，点开看卡片。「继续阅读」可以回到最近读过的论文和上次停留的位置 |
| 证据笔记 | 按研究问题汇集不同论文的摘录、页码和自己的判断，可以定位原文或导出 Markdown |
| 知识地图 | 交互式网络图：蓝 = 方法，橙 = 主题，灰 = 论文；勾选「只看学科」看学科之间的联系 |
| 学科 | 每个学科的 hub 页：概述、代表文献、常用工具、方法进展、争议、交叉；「立即整合」让模型重写概述和争议 |
| 手册 | 学习手册：**A 部分**只写你库里有 DOI 出处的内容；**B 部分**是模型补充的背景知识（标明需核实） |
| 工具 | 被多篇论文用到的软件包，各自一页 |
| 待审核 | 模型拿不准的分类、DOI 对不上的文件、提议的新学科（建立 / 合并 / 拒绝） |
| 分类表 | 切换预设或直接编辑分类表；改完可以一键重新分类 |
| 费用 | 本月花费、按用途和日期的统计、再处理 10 篇大约多少钱 |
| 设置 | 模型服务、价格、预算、语言、文件夹、排除规则…… |

**你的笔记不会被覆盖**：卡片里「与我相关的点」、学科页和手册里「我的笔记」这几块，程序永远不会改动。

在 PDF 里选中一段话，点「摘到证据笔记」，选择或新建一个研究问题。可以标为「支持」「相反」或「待核对」，也可以写下自己的解释。在左栏打开「证据笔记」，点「定位原文」就会回到对应页面，并标出摘录的位置。每条摘录对应一页，跨页的段落可以分两次摘录。

在「设置 → AI 服务 → 翻译」选择划词翻译的默认服务。小牛文本 API 需要 App ID 和 API Key，智谱的免费模型 GLM-4.7-Flash 需要模型标准 API Key，腾讯混元翻译需要腾讯云 TokenHub 广州地域的 API Key。设置页提供各家的官方入口。论文详情中的「翻译全文」也可以单独选服务。

**费用**：以 DeepSeek 为例，一篇 Nature 长文（卡片 + 分类）大约 2 万输入 + 4 千输出 token，按官方闲时价不到 1 美分。设置里可以设每月预算，用完自动暂停。自定义服务请在「价格」里照账单填写单价、倍率，以及是否高峰翻倍，费用统计才准。

---

## 插件与本地接口

在「设置 → 应用 → 插件与接口」安装读书会导出模板或 Python 事件查看器，也可以创建令牌，授权本机工具检索论文和订阅事件。[插件开发指南与示例](src/papermap/extension_examples/GUIDE.md)

## 开机自动运行

```bash
papermap autostart install      # 登录后自动在后台运行（macOS / Windows / Linux）
papermap autostart status
papermap autostart uninstall
```

打包版（应用程序）：直接在菜单栏 / 托盘图标的菜单里勾选「登录时自动启动」即可。之后随时在浏览器打开 <http://127.0.0.1:8765>，或点菜单栏图标「打开 PaperMap」。要退出，点图标菜单里的「退出 PaperMap」（或设置页底部的按钮）。

---

## 数据放在哪里

默认在主目录下的 `PaperMap/`：

```
PaperMap/
├── config.yaml        设置（包括你在网页里填的 Key，只存本机）
├── papermap.db        处理状态和索引
├── taxonomy.yaml      你编辑过的分类表（如果改过）
├── logs/
└── knowledge/         知识库本体：可以直接作为 Obsidian 仓库打开
    ├── papers/        论文卡片
    ├── fields/        学科页
    ├── handbooks/     学习手册
    ├── concepts/      工具页
    └── _pending/      待定的新学科
```

**备份 / 换电脑**：备份知识库和状态数据库。配置文件可能包含 API Key。换机时跳过保存 ChatGPT 登录凭据的 `chatgpt/` 目录，在新设备上重新登录。设置里可以打开「用 git 自动记录知识库的每次改动」，任何一次改动都能回滚（需要电脑上装了 git）。

---

## 常见问题

- **扫描页怎么摘录？** 在 Mac 上打开扫描页，PaperMap 会用系统的 Vision 在本机识别文字。遇到图文混合页，也可以点阅读器里的「识别本页」。识别后的文字可以选中、摘录和搜索；摘录会注明「本机 OCR」，方便对照扫描原图。
- **没有 DOI 的论文**（学位论文、会议摘要）：照常处理，卡片标注「无 DOI」，靠文件内容和标题去重。
- **补充材料 PDF**：按文件名和首页特征识别，跳过，不花钱。
- **DOI 对不上**：会进「待审核」，你可以手动填正确的 DOI 或按无 DOI 处理。
- **端口被占用**：设置文件里改 `web.port`。
- **网络断了 / 服务商限流**：自动重试；处理不了的论文会留到下次，不会丢。

---

## 命令行（可选）

```
papermap                 首次设置 + 运行 + 打开浏览器
papermap init            在命令行里交互式生成配置
papermap scan [--yes]    扫描一次入口文件夹并处理
papermap run [--open]    常驻监听 + 网页
papermap serve           只开网页
papermap classify [--stale|--all]   分类（改分类表后用 --stale 重新分类）
papermap pending         列出 / 建立 / 合并 / 拒绝待定新学科
papermap consolidate     整合到期的学科页
papermap handbook [slug] 生成学习手册
papermap reprocess       重新生成卡片（例如换了 prompt 版本）
papermap status          库概况
papermap autostart ...   开机自启
```

## 开发

```bash
uv venv && uv pip install -e ".[dev,build]"
pytest                          # 测试
python packaging/build.py       # 打包到 dist/（需在目标系统上构建）
```

GitHub Actions（`.github/workflows/build.yml`）会在 Windows 和 macOS 上跑测试并打包。
