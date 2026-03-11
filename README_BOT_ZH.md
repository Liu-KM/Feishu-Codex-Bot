# 飞书 Codex Bot

这个项目运行的是一个飞书机器人：它通过飞书 WebSocket 事件接收文本消息，调用本机 `codex exec`，为每个飞书会话维护独立的 Codex session，并把文本、文件或图片回发到原会话。

## 这个版本现在能做什么

- 私聊里默认可以直接提问，也可以手动加 `/codex`
- 群聊里可以通过 `@bot` 或 `/codex` 触发
- 每个飞书 chat 都会单独保存一个 Codex session id，默认落盘到 `.feishu_codex_sessions.json`
- 可以为每个 chat 单独设置默认模型和推理强度
- 如果 Codex 最终回复里带上 `ATTACH: /绝对路径`，bot 可以自动把该文件或图片上传回飞书

## 当前目录里的关键文件

- `feishu_codex_bot.py`：主程序
- `.env.example`：环境变量模板
- `requirements.txt`：部署时使用的 Python 依赖列表
- `scripts/deploy.sh`：新机器初始化脚本
- `VERSION`：当前 starter 版本号
- `CHANGELOG.md`：变更记录
- `systemd/feishu-codex-bot.service.example`：服务配置模板
- `README_BOT.md`：英文说明
- `oapi-sdk-python/`：本项目使用的飞书 Python SDK submodule

## 运行前要求

- Python 3.10+
- 本机已经安装 `codex` CLI，且命令在 `PATH` 中
- 已创建飞书应用并启用机器人能力
- 飞书开放平台已启用 WebSocket / 长连接事件投递

## 克隆方式

```bash
git clone --recurse-submodules <你的私有仓库地址>
```

如果已经先 clone 了主仓库，但没带 submodule，再执行：

```bash
git submodule update --init --recursive
```

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install setuptools wheel requests requests_toolbelt pycryptodome websockets httpx
pip install --no-build-isolation ./oapi-sdk-python
```

`./oapi-sdk-python` 这个本地 SDK 目录用 `--no-build-isolation` 安装通常更稳定，不建议直接走 `pip install -e`。

## 环境变量配置

```bash
cp .env.example .env
```

至少要配置：

- `APP_ID`
- `APP_SECRET`
- `CODEX_WORKSPACE`

常用可选项：

- `CODEX_MODEL`：新 chat 默认模型
- `CODEX_REASONING_EFFORT`：默认推理强度，可选 `low`、`medium`、`high`、`xhigh`
- `CODEX_TIMEOUT_SECONDS`：Codex 子进程超时
- `CODEX_SANDBOX`：传给 `codex exec --sandbox`
- `BOT_TRIGGER_PREFIX`：默认 `/codex`
- `ALLOW_P2P_WITHOUT_PREFIX`：为 `true` 时，私聊可直接触发
- `ALLOWED_OPEN_IDS`：允许访问 bot 的飞书 `open_id` 白名单，逗号分隔
- `BOT_OPEN_ID`：群聊里用于更严格的 `@bot` 识别
- `AUTO_SEND_ATTACHMENTS`：为 `true` 时自动上传 `ATTACH:` 指定的附件
- `SESSION_STORE_PATH`：session 存储路径；默认在 `CODEX_WORKSPACE` 下生成 `.feishu_codex_sessions.json`
- `BOT_LOG_LEVEL`：默认 `INFO`

注意：附件路径最终必须解析到 `CODEX_WORKSPACE` 之内的现有文件，否则会被拒绝。

## 启动方式

```bash
source .venv/bin/activate
python feishu_codex_bot.py
```

启动后会输出当前工作目录和 session 存储文件路径，然后建立飞书 WebSocket 长连接。

## 快速部署

现在仓库里已经包含一个最小部署脚本：

```bash
./scripts/deploy.sh
```

它会创建 `.venv`、安装 Python 依赖、安装本地飞书 SDK，并在缺少 `.env` 时自动从 `.env.example` 生成。

## 飞书开放平台配置

在飞书开放平台里需要完成这些配置：

1. 创建企业自建应用
2. 给应用启用 bot
3. 开启事件订阅，并选择 WebSocket / 长连接模式
4. 订阅事件 `im.message.receive_v1`
5. 给应用开通发送消息、上传文件、上传图片所需的 IM 权限
6. 安装或发布到租户内，让目标用户可以和 bot 对话

控制台文案可能会变化，但核心能力就是上面这几项。

## 消息触发规则

- 私聊：
  - 当 `ALLOW_P2P_WITHOUT_PREFIX=true` 时，直接发文本即可
  - `/codex <你的请求>` 也可以
- 群聊：
  - `@bot <你的请求>`
  - `/codex <你的请求>`

当前只接受“文本消息”作为输入。用户发图片、文件、卡片之类的入站消息时，bot 会直接回复当前不支持。

## 内建命令

- `/help`：查看帮助
- `/ping`：检查 bot 是否在线
- `/status`：查看当前 chat id、Codex session id、默认模型、默认强度、工作目录
- `/sessions`：列出 bot 已记录的 session
- `/session`：查看当前会话状态
- `/session name <名字>`：给当前会话设置可读名称
- `/reset`：清空当前会话绑定的 Codex session id
- `/model`：查看当前默认模型
- `/model <name>`：设置当前会话默认模型
- `/effort`：查看当前默认推理强度
- `/effort <low|medium|high|xhigh>`：设置当前会话默认推理强度
- `/ask --model <name> --effort <level> <request>`：只对这一条请求临时覆盖模型或强度
- `/send <路径>`：把 `CODEX_WORKSPACE` 内的文件或图片发回飞书

同一个飞书 chat 在同一时刻只允许跑一个 Codex 任务；如果前一个任务还没结束，bot 会直接回复当前 session 正忙。

## 附件回传机制

如果希望 Codex 产出的文件直接发回飞书，可以让它在最终回复末尾输出：

```text
ATTACH: /home/liu/feishu_codex/outputs/report.md
ATTACH: /home/liu/feishu_codex/outputs/diagram.png
```

处理规则：

- 只接受已经存在的文件
- 路径必须位于 `CODEX_WORKSPACE` 之内
- `.png`、`.jpg`、`.jpeg`、`.gif`、`.bmp`、`.webp` 会按图片发送
- 其他文件走飞书文件上传接口

## 运行时行为

- bot 调用的是 `codex exec --json`
- 如果当前 chat 已有保存的 session id，会自动走 `codex exec ... resume <session_id>`
- 飞书里只会收到 Codex 最终那条 agent message，不会流式展示中间步骤
- 长文本会先切块，再分多条回发

## 当前限制

- 入站消息仅支持文本
- 不会把 Codex 的中间进度实时推回飞书
- session 存储是本地 JSON，不是共享数据库
- 附件发送范围严格限制在 `CODEX_WORKSPACE` 内

## 版本管理

- `VERSION` 保存当前 starter 版本号
- `CHANGELOG.md` 记录仓库级别变更
- `.gitignore` 已排除本地凭据、日志、虚拟环境和 session 状态文件
- `.gitmodules` 记录飞书 SDK submodule 信息

## 官方参考

- 飞书 Python SDK 安装：
  - https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/preparations-before-development
- 飞书 Python SDK 事件处理：
  - https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/handle-events
- 仓库内 SDK README：
  - `oapi-sdk-python/README.md`
