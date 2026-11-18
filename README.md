# wechat-notifier · 个人微信通知

独立运行在 macOS Apple Silicon 上的个人微信通知服务。业务系统通过本机 HTTP 提交任务，应用按唯一群 ID 或联系人 ID 绑定，持久化排队、去重、暂停和记录发送结果。

默认通过 [WeChat-ChatBot-Mac](https://github.com/Gusion1017/WeChat-ChatBot-Mac) 的 Go / Frida 原生桥调用微信内部文字发送通道。发送路径不使用窗口坐标、鼠标、键盘或剪贴板。

`业务系统 → HTTP 入队 → SQLite 队列 → Go / Frida 桥 → 群聊或私聊`

## 功能与边界

- 群聊和个人目标分别搜索。绑定时核对唯一 ID 和微信账号，同名结果需要手动选择。
- 手工通知及 Bearer Token 接口；管理员密钥与外部应用提交密钥分离。
- SQLite 持久化任务、事件编号去重、发送间隔、队列上限、过期、分页和人工确认。
- 发送超时或结果未知会暂停，不自动重发；明确的发送前失败才允许有限重试。
- 默认暂停发送，只监听回环地址，不开放 CORS。
- 当前仅支持文字通知，不转发入站聊天、不接入外部 AI 或自动回复。

这是非官方客户端 Hook 集成，依赖精确微信版本、登录状态与发送上下文。微信升级需要重新核对兼容；微信本身重启后，需要手动发送一条普通文字建立 R3 上下文。不能据此承诺任意版本、锁屏或窗口关闭状态均可用。

代码保留旧窗口自动化实现用于兼容与研究，默认后端为原生桥，不会自动回退到前台操作。旧视觉绑定不能直接转为原生绑定。

## 快速预演

需要 Python 3.11+。预演不连接或操作真实微信，可先检查界面和接口流程。

```sh
git clone https://github.com/guwangxian-ai/wechat-notifier.git
cd wechat-notifier
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m notifier --dry-run --port 18791 --data-dir .state/rehearsal --open
```

打开后搜索并绑定一个预演目标，再提交任务、开启队列预演。预演结果记为 `simulated`。

真实服务入口：

```sh
.venv/bin/python -m notifier --open
```

管理地址为 `http://127.0.0.1:18790/`。`--open` 使用本机管理入口打开浏览器；页面读取后会从地址栏移除密钥。请勿分享带密钥的完整入口。

## 绑定个人通知

打开“通知目标”，选择“个人”。勾选本次查询授权后，可在运行微信的 Mac 上按昵称、备注或微信 ID 查找联系人。选择对应结果，核对唯一微信 ID，再保存目标标识。给自己发通知时可直接查找“文件传输助手”，无需读取联系人目录。

写通知时选择该收件人，接口调用时使用它的 `target_code`。个人通知需要更新后的原生桥，发送状态仍按任务记录与通知编号核对。

## 原生桥构建与部署

固定上游 commit：`9094de7859618a8124f9d3162e63c87b0c8c837a`。

桥提供以下精确客户端配置：

| 微信版本 | 构建 |
| --- | --- |
| 4.1.15.55 | 270135 |
| 4.2.0.5 | 270341 |
| 4.2.0.9 | 270345 |
| 4.2.0.12 | 270348 |

实际后台群发送已在 4.2.0.12 / 270348 环境验证；其他配置仍应在自己的环境独立核对。App Store 4.1.15.35 / 270115 不在支持表内。

先阅读固定版本的上游构建脚本和本仓库适配，再执行：

```sh
mkdir -p .state/upstream artifacts
git clone https://github.com/Gusion1017/WeChat-ChatBot-Mac.git .state/upstream/WeChat-ChatBot-Mac
git -C .state/upstream/WeChat-ChatBot-Mac checkout --detach 9094de7859618a8124f9d3162e63c87b0c8c837a
.venv/bin/python scripts/prepare_native_bridge.py
GOMAXPROCS=2 GOFLAGS=-p=2 BRIDGE_CACHE_DIR="$PWD/.state/upstream/build-cache" bash .state/upstream/WeChat-ChatBot-Mac/scripts/build_bridge.sh
.venv/bin/python scripts/package_native_bridge.py
```

构建产物与对应源码分别输出到 `artifacts/native-bridge-runtime.tar.gz` 和 `artifacts/native-bridge-source.tar.gz`。工具链与缓存保存在项目内，不全局安装。构建配置为 Go 1.25.14、Frida core / Gadget 17.8.0。

真实部署还需要：

1. 本人明确同意修改客户端、备份本机账号数据，以及仅在本机捕获目录解密密钥；这些授权记录保存在私有 `.state/native-consent.json`，启动网页不会自动授予。
2. 在核对过的官方客户端副本安装带独立鉴权的 Gadget，保留原签名权限并重新签名；本仓库不分发微信安装包或修改后的客户端。
3. 将自建 runtime 解包到 `.state/native-runtime`，配置本机桥地址与独立 Token；桥、Gadget 均只监听回环地址。
4. 启动 `scripts/run_native_bridge.py`，完成登录和首次人工初始化，再在通知界面查找、核对并绑定群。
5. 连接及目标群核对通过后，提交一条有明确授权的通知并开启发送。

`stage_native_client.py`、`secure_native_candidate.py`、`install_native_client.py` 和 `initialize_native_directory.py` 是带版本、哈希和授权校验的维护工具，面向已核对的安装计划，不能当成任意微信版本的一键安装器。缺少原始程序、备份或当前账号证据时会停止。

首次缺少群目录密钥时，`initialize_native_directory.py start|resume|finish` 支持登录前初始化和交回受管服务。账号路径、密钥和发送上下文不会出现在公开仓库或 API 响应中。

原生发送不需要旧视觉模式的辅助功能或屏幕录制权限。自动更新设置是单独的授权项；即使关闭后读回成功，也不能保证微信永远不再更改它。更新客户端前仍须核对支持版本。

## 外部应用接口

在管理界面“连接与接口”获取提交密钥。该密钥可提交任务、读取可用目标标识和查询自己提交的任务。

```http
POST /api/v1/notifications
Authorization: Bearer YOUR_INGEST_KEY
Content-Type: application/json

{
  "event_id": "lead:platform:stable-user-id",
  "target_code": "leads",
  "title": "新增线索",
  "body": "用户、地区、需求原话和公开来源链接",
  "expires_in_seconds": 86400
}
```

首次提交返回 HTTP 202、`task_id` 和 `status_url`，仅表示入队。相同来源、事件编号与目标组合重复提交相同内容返回 HTTP 200 和原任务；不同内容返回 409。调用超时后应使用相同事件编号和正文重试。

- `GET /api/v1/targets`：可用目标标识。
- `GET /api/v1/tasks/{task_id}`：查询任务。
- `GET /api/v1/tasks?status=pending&page=1&limit=25`：分页查询，最多 100 条。
- 鉴权错误返回 401/403，配置或输入错误返回 422，队列已满返回 429。
- 请求体最多 32 KiB，需 JSON Content-Length，不接受流式请求体。
- 标准库客户端示例：`scripts/submit_notification.py`。

调用方负责判断用户是否符合业务条件，并以稳定业务 ID 生成事件编号。通知服务不会自行采集用户或调用模型。

## 发送状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 待发送，可暂停、取消，默认 24 小时过期 |
| `preparing` | 核对桥、账号、收件人 ID 和发送上下文 |
| `dispatching` | 已持久记录即将发送；此后异常按不确定结果处理 |
| `submitted` | 原生桥报告已提交微信，不等于成员收到 |
| `confirmed_manually` | 用户在对应会话核对通知编号后确认 |
| `unknown` | 发送结果未知，自动暂停，核对后人工处理 |
| `failed` | 发送前校验失败或有限重试失败 |
| `simulated` | 预演，不操作微信 |

旧视觉后端还可能记录 `observed`，表示本地消息区识别到通知编号；同样不等于微信服务端回执或已读。每条消息会附唯一通知编号，便于核对。

## 本机后台运行

```sh
.venv/bin/python scripts/manage_service.py install
.venv/bin/python scripts/manage_service.py status
.venv/bin/python scripts/manage_service.py stop
.venv/bin/python scripts/manage_service.py start
```

原生桥使用独立的 `scripts/manage_native_service.py` 管理。服务有进程锁，禁止多 worker 共用同一状态目录；升级只替换代码，保留运行中的 `.state`。

凭证、账号绑定、SQLite、目录密钥、初始化种子及备份只保存在本机私有目录。日志轮转，不记录消息正文或凭证。运行数据、真实群名、群 ID、实机截图和部署回执不属于公开源码。

## 开发验证

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q notifier scripts
node --check notifier/static/app.js
```

准备上游源码后，可额外执行：

```sh
node tests/native_contact_hooks.js .state/upstream/WeChat-ChatBot-Mac/bridge/script.js
```

Python 测试、Node Hook 保护检查和构建检查使用合成数据，不操作真实微信；它们不替代具体版本的实机验收。

## 许可与来源

本项目按 [GPL-3.0](LICENSE) 发布。原生桥的上游版本、修改范围、许可证和构建依赖见 [THIRD_PARTY.md](THIRD_PARTY.md)。分发原生桥二进制时，应同时保留对应源码和许可证。

本仓库只发布通知应用及桥适配源码，不包含个人微信程序、真实账号数据、运行密钥、用户聊天或机器备份。
