# PaperMap 插件与本地 API

在「设置 → 应用 → 插件与接口」展开「试用内置示例」。安装「读书会导出模板」并启用，在论文列表选择论文，打开导出菜单，选择「读书会提纲」。此示例直接使用已有卡片，在本机生成 Markdown。缺少的卡片字段保留为空。

安装「Python 事件查看器」，阅读声明后选择「信任并启用」，再点「运行」。结果中的 `status` 应为 `ready`。两个示例均可在离线环境运行。Python 示例只返回事件编号和摘要，没有第三方依赖。

## 编写配置插件

把 `plugin.yaml` 放在与插件 `id` 同名的文件夹中，选「安装插件文件夹」，或复制到数据目录的 `plugins/` 下并刷新列表。首次安装后需在设置里启用。修改已安装包的内容后，重新检查并启用新版本。默认插件目录是 `~/PaperMap/plugins/`。

下面是可以直接放进 `paper-label/plugin.yaml` 的最小示例。

```yaml
api_version: 1
id: paper-label
name: 论文标签
version: "1.0"
contributions:
  exports:
    - id: short-label
      name: 简短题录
      extension: txt
      template: '${first_author} (${year}). ${title}. DOI ${doi}'
```

每项贡献都有独立的 `id` 和显示名称 `name`。下表列出 `contributions` 可用的四个列表。

| 列表 | 定义与使用位置 |
|---|---|
| `providers` | 填 `base_url`、`default_model`，可选 `protocol` 和 `group`。协议可选 `openai`、`responses`、`anthropic`、`gemini`。计费组可选 `api` 或 `subscription`。模型菜单的 `models` 以模型名为键，值可含 `label` 和每百万 token 的美元单价 `in`、`hit`、`out`。启用后在「模型与账号」选服务，密钥由使用者填写。订阅分组用于计费展示，账号授权取决于服务商。远程地址使用 HTTPS，本机地址也支持 HTTP。 |
| `translations` | `provider` 可填 `main` 或同一插件中的服务商 ID。`mode` 可选 `translate`、`polish`、`custom`，自定义模式需提供 `prompt`。启用后可在翻译服务中选择。 |
| `skills` | `prompt_file` 指向包内相对路径的 UTF-8 Markdown 提示词，可选 `description`。运行技能时会按所选模型设置处理论文。 |
| `exports` | `extension` 支持 `md`、`txt`、`bib`、`ris`。`template` 可引用 `id`、`title`、`first_author`、`journal`、`year`、`doi`、`research_question`、`one_sentence`、`key_findings`、`limitations`。每篇论文渲染一份文本，多选时依次拼接。 |

## 从外部程序调用本地 API

在「插件与接口」创建访问令牌并复制。默认基址是 `http://127.0.0.1:8765/api/v1`，请以设置页显示的端口为准。每次请求携带 `Authorization: Bearer <令牌>`。同一令牌可以读取文献、搜索、导入和订阅事件，供获准访问本机文献库的工具使用。重新生成令牌会替换旧令牌，撤销会停用访问。PaperMap 仅保存令牌摘要。

| 请求 | 结果 |
|---|---|
| `GET /papers?after=0&limit=50` | 返回 `items` 和 `next_cursor`，可按论文 ID 继续翻页。 |
| `GET /papers/{id}`、`GET /papers/{id}/card` | 读取题录或已生成的结构化卡片。卡片尚未生成时返回 404，详情为 `card_not_ready`。 |
| `GET /search?q=Xenium&limit=20` | 查询本地全文索引，返回 `items`。每项含 `paper_id`、`kind`、`snippet` 等。属于某页的命中以 1 为起点标出 `page`。 |
| `POST /import?filename=article.pdf` | 使用 `Content-Type: application/pdf` 发送 PDF 原始字节，最大 256 MiB。HTTP 202 表示已接收入库，后续沿用应用的识别、去重和模型处理设置。 |
| `GET /events?after=0&limit=50` | 读取按事件 ID 递增的 `items`。处理成功后保存 `next_cursor`，下次作为 `after` 传入。 |
| `GET /events/stream?after=0` | 实时 SSE，媒体类型 `text/event-stream`。每次连接默认持续约 30 秒，保持订阅需要客户端重新连接。续读时携带 `Last-Event-ID` 或更新 `after`。 |

下例用 Python 标准库从本机读取论文和最近待处理的事件。输入令牌时终端隐藏字符。翻页上限为 100 条。

```python
from getpass import getpass
import json
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8765/api/v1"
headers = {"Authorization": "Bearer " + getpass("PaperMap 令牌: ")}

def get(path):
    with urlopen(Request(base + path, headers=headers), timeout=10) as response:
        return json.load(response)

print(get("/papers?limit=3")["items"])
for event in get("/events?after=0&limit=3")["items"]:
    print(event["id"], event["type"], event["data"])
```

`paper.ready` 表示论文卡片已可读取，`data.paper_id` 是论文编号。`field.proposed` 表示候选学科达到待确认阈值，载荷含 `field_id` 和 `slug`。事件含 `id`、`type`、`created_at`、`data`。在同一个库里，用事件 `id` 去重。SSE 的 `data:` 行是同一格式的 JSON。回放限于启用此版本事件记录后已经产生的事件。建立集成时可先用 `/papers` 获取已有论文。

## 从 Claude Code 查询 PaperMap 证据

打开「设置 → 应用 → 插件与接口 → 通过 MCP 查询文献」，创建并复制只读令牌。PaperMap 运行期间，支持 Streamable HTTP 的本机客户端可以连接设置中显示的地址。鉴权使用 HTTP 请求头 `Authorization: Bearer <只读令牌>`。客户端会获得所请求的卡片、原文摘录和笔记，请选择你信任的助手。

Claude Code 用户可以将设置页的配置合并到项目的 `.mcp.json`。其中 `${PAPERMAP_MCP_TOKEN}` 引用环境变量，配置文件里只保留这个占位符。例如，在 macOS 默认的 zsh 终端中运行以下命令，粘贴令牌、回车，再启动 Claude Code。

```zsh
read -rs 'PAPERMAP_MCP_TOKEN?PaperMap MCP token: '
export PAPERMAP_MCP_TOKEN
claude
```

在 Claude Code 的 `/mcp` 中查看 `papermap` 的状态。可以试问「列出我的研究问题。对其中一个问题，找出标记为相反的原文，附上论文、页码和回到 PDF 的链接。」首次使用可以先在 PaperMap 的「证据笔记」中为问题添加一条摘录。

| 工具 | 返回内容 |
| --- | --- |
| `search_library` | 本机文献库中的检索命中 |
| `get_paper_card` | 指定论文的卡片信息 |
| `list_research_questions` | 可查询的研究问题及其编号 |
| `get_question_evidence` | 问题下的证据，包括原句、立场和 PDF 定位链接，可筛选反证 |

MCP 授权与上面的本地 API 授权分别创建、分别撤销。在设置页更换只读令牌后，用新值更新环境变量并重新启动客户端。[Claude Code 的 MCP 配置说明](https://code.claude.com/docs/en/mcp)

## 受信任的 Python 插件

在清单中添加 `python: handler.py`，并在该文件中定义 `handle(event)`，返回可序列化为 JSON 的值。`permissions` 是展示给使用者的权限声明，操作系统授予运行账户的文件和网络权限仍然适用。仅启用经过你审阅、来源可信的代码。每次调用在独立的临时包副本中运行，限时 10 秒，输入和输出各限 1 MiB。有持久状态的插件应选择自己的存储位置。发布时使用普通文件，插件包最多 8 MiB、256 个文件和目录。

在 `events` 中列出 `paper.ready` 或 `field.proposed` 以订阅事件。「运行」按钮单独传入 `{"type":"plugin.run","data":{}}`。处理函数可按 `event["type"]` 选择行为。

首次启用或信任更新版本后，自动回调从当时的最新事件往后接收。应用重启时会从该插件上次成功的位置继续。失败会暂停该插件的后续回调，在「插件与接口」重试后从失败处继续。若处理会写文件或发送通知，先记录已处理的事件 ID，以便崩溃重试时识别重复交付。暂停期间要补取的历史事件可通过本地 API 读取。
