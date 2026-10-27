# River Gateway

基于 [River 官方文档](https://docs.river.ai) 实现的 **OpenAI 兼容推理网关**。应用和团队成员只拿网关签发的密钥（`rgw_...`），River 的 API key 只保存在网关服务器上；网关负责鉴权、限流、预算、模型路由与回退、按 River 规范重试，并按 River 公开价格统计每个请求的成本。

```
客户端（OpenAI SDK / curl / 你的应用）
    │  Authorization: Bearer rgw_...         ← 网关密钥，可限额、可停用、可设预算
    ▼
River Gateway：鉴权 → 模型权限/预算 → 请求校验 → 限流 → 路由（别名/专属部署/回退）→ 重试 → 用量记账
    │  Authorization: Bearer rv_...          ← River key，只在服务器上
    ▼
https://api.river.ai/v1        或        专属部署的 deployment.base_url
```

支持的接口：`POST /v1/chat/completions`（对话、图片、工具调用、流式）、`POST /v1/responses`（Responses API）、`GET /v1/models`，以及 `/ui/` 管理控制台、`/admin/*` 管理 API、`/metrics`、`/healthz`。

## 功能

| 功能 | 说明 |
| --- | --- |
| 网关密钥 | SQLite 中只存 SHA-256 哈希；可限定模型（支持 `deepseek-ai/*` 通配）、每分钟请求数、并发数、美元预算、过期时间 |
| 模型路由 | 透传任意 River 模型 ID；别名（如 `fast`）；按路由设置默认参数；把模型指向专属部署 |
| 回退 | 429 / 5xx / 连接失败且重试用尽后，按顺序切换到 `fallbacks` 中的模型 |
| 重试 | 按 River 文档：429/503 带抖动退避并遵守 `Retry-After`；500/502/504 有限次重试；4xx 不重试；流开始后不重试（River 的流不能续传） |
| 流式代理 | SSE 逐事件转发；自动请求流式 usage 并对客户端隐藏多出来的 usage 块；客户端断开时立即关闭上游连接，停止 River 继续生成 |
| 提前拦截 | 超过 50 MiB 的请求体、非 JPEG/PNG 图片、Responses 的 `store:true` / `background` / `previous_response_id` / `conversation`，在发给 River 前就返回 400/413，不浪费钱 |
| 成本统计 | 定时拉取 `console.river.ai/api/public/pricing`；缓存输入按缓存价、推理 token 按输出价计入；按密钥/模型/上游/天汇总 |
| River 账号池 | 录入多个 River 账号（各自的 API key），手动选择当前用哪个，切换后下一个请求立即生效；显示每个账号的估算剩余额度，River 返回 402 时自动标记余额不足 |
| 管理控制台 | 内置在网关里的网页（`/ui/`）：总览、密钥管理、River 账号、请求日志、Playground、状态页；无需构建、不依赖外部 CDN |
| 可观测性 | 每个请求一行访问日志；Prometheus 指标；响应头返回 `x-gateway-request-id`。River 给了 `x-request-id` 就透传；没给时（实测成功响应都没有），用量日志改记响应 id（`chatcmpl-…` / `resp_…`），联系 River 支持时可以引用 |

## 用 Docker 启动（推荐）

> 部署到 Linux 服务器（包括让 AI 代理照着部署）请看 [DEPLOY.md](DEPLOY.md)：里面有逐步命令、每步的预期结果、HTTPS 反向代理模板（`deploy/`）、定时备份、升级回滚和故障排查。

本机用 Docker Desktop 运行时，在项目目录里：

1. **准备配置**（只需一次）：把 `.env.example` 复制成 `.env`，填入 `RIVER_API_KEY` 和 `GATEWAY_ADMIN_TOKEN`；再把 `config.example.yaml` 复制成 `config.yaml`。
2. **构建并启动**：

   ```powershell
   docker compose up -d --build
   ```

3. **打开** `http://127.0.0.1:8080/`，用 `GATEWAY_ADMIN_TOKEN` 登录管理控制台，在“密钥管理”里创建网关密钥，然后应用把 OpenAI SDK 的 `base_url` 设为 `http://127.0.0.1:8080/v1` 即可。

| 做什么 | 命令 |
| --- | --- |
| 看日志 | `docker compose logs -f` |
| 检查 River key 和连接 | `docker compose exec gateway river-gateway check` |
| 用命令行管理密钥 | `docker compose exec gateway river-gateway keys create --name my-app` |
| 查看用量 | `docker compose exec gateway river-gateway usage --by key` |
| 备份数据库（不停服） | `docker compose exec gateway river-gateway backup` |
| 改了 `config.yaml` | `docker compose restart` |
| 改了 `.env` | `docker compose up -d`（会用新的环境变量重建容器） |
| 更新代码后 | `docker compose up -d --build` |
| 停止 | `docker compose down`（数据保留） |
| 停止并删除所有数据 | `docker compose down -v` |

- **端口**：默认只绑定 `127.0.0.1`，只有本机能访问。要给局域网或服务器上的其他机器用，先把 `GATEWAY_ADMIN_TOKEN` 换成足够长的随机字符串，再把 `docker-compose.yml` 里的 `"127.0.0.1:8080:8080"` 改成 `"8080:8080"`。
- **数据**：密钥、River 账号和用量记录在 Docker 卷 `river-gateway_gateway-data` 里的 SQLite 数据库中，删除或重建容器都不会丢。它和本地直接运行时用的 `data/gateway.db` 是两份独立的数据。备份和恢复见下一节。
- **时区**：`docker-compose.yml` 里的 `TZ: CST-8`（UTC+8）决定日志时间和 `usage --by day` 怎么分天；控制台图表始终按浏览器的时区分。
- **镜像**：基于 `python:3.12-slim`，以非 root 用户运行，自带健康检查（`docker ps` 里能看到 healthy）；只改代码时重新构建只需几秒。

## 备份与恢复（部署到服务器时必看）

**要备份什么：**
- **数据库**：网关密钥（哈希）、River 账号（包括它们的 API key 原文）、用量记录、当前账号等设置。用下面的命令备份。
- **`.env` 和 `config.yaml`**：River key、管理令牌、路由配置。它们不在数据库里，部署时自己另存一份，比如放进密码管理器。

**备份不用停服。** `river-gateway backup` 用 SQLite 的在线备份接口，在网关继续处理请求的同时复制出一份一致的快照，写完会做完整性检查，确认无误才保留。备份文件里有 River API key，要像 `.env` 一样保管：命令在 Linux 上会把文件权限设为 600，`backups/` 也已加入 `.gitignore`。

### 手动备份

```bash
docker compose exec gateway river-gateway backup         # 存到容器数据卷里的 /app/data/backups/，自动只保留最近 14 份（--keep 调整）
docker compose exec gateway river-gateway backup --list  # 列出已有备份
docker compose exec -T gateway river-gateway backup --output - > gateway-backup.db   # 直接存到宿主机当前目录
```

只放在数据卷里的备份，在服务器硬盘损坏时会和数据库一起丢失，所以要把备份放到宿主机上，最好再同步到其他机器或对象存储。上面第三条命令会把备份直接写到宿主机，适用于 Linux / macOS 的 shell 和 PowerShell 7.4 及以上。旧版 Windows PowerShell 的 `>` 会损坏二进制文件，在那里改用 `docker compose cp gateway:/app/data/backups ./backups` 把整个备份目录复制出来。

### 每天自动备份（Linux 服务器）

`scripts/server-backup.sh` 会把备份流式写到宿主机的 `backups/`：先写临时文件，成功后再改名，权限设为 600，并删除 30 天前的备份（这两个数字都可以通过参数修改）。把项目放在 `/opt/river-gateway` 时，用 `crontab -e` 加一行：

```bash
0 3 * * * bash /opt/river-gateway/scripts/server-backup.sh >> /opt/river-gateway/backups/backup.log 2>&1
```

每天 03:00 执行，运行日志写在 `backups/backup.log`。cron 的环境变量很少：如果找不到 `docker`，就在脚本里写 `docker` 的完整路径（用 `which docker` 查）。想把备份再同步到别处，可以在这一行后面接 `&& rclone copy ...` 或 `rsync ...`。

### 恢复

```bash
# 用数据卷里的备份恢复（文件名用 backup --list 查）
docker compose exec gateway river-gateway restore /app/data/backups/gateway-20261027-112415.db

# 用宿主机上的备份文件恢复（通过标准输入传进容器）
docker compose exec -T gateway river-gateway restore - --yes < backups/gateway-20261027-030000.db
```

恢复前会先检查备份文件：完整性检查通不过，或者不是网关的数据库，就直接拒绝。然后把当前数据库另存为 `pre-restore-时间.db`（不会被自动清理），再替换成备份的内容。正在运行的网关从下一个请求起就会使用恢复后的数据，不需要重启。旧版本网关做的备份恢复后，会自动升级到当前的表结构。恢复错了，用那份 `pre-restore-*.db` 再恢复一次即可。

### 迁移到新服务器

1. 旧服务器上：`docker compose exec -T gateway river-gateway backup --output - > gateway-backup.db`，再把它和 `.env`、`config.yaml` 一起拷到新服务器。
2. 新服务器上：把项目代码、`.env`、`config.yaml` 放好，运行 `docker compose up -d --build`。
3. 导入数据：`docker compose exec -T gateway river-gateway restore - --yes < gateway-backup.db`
4. 用 `docker compose exec gateway river-gateway check` 确认连接正常，再在控制台看看密钥和账号都在。

不用 Docker 时，命令一样，只是不加 `docker compose exec gateway` 这个前缀：`river-gateway backup` 会把备份存到数据库旁边的 `data/backups/`。

## 快速开始（Windows PowerShell，不用 Docker）

项目目录里已经建好了 `.venv` 虚拟环境并以可编辑模式装好了依赖。新机器上的安装方式：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

1. **配置**：复制模板，然后在 `.env` 里填入 River API key（在 [River Console](https://console.river.ai/keys) 创建）和管理令牌。

   ```powershell
   copy config.example.yaml config.yaml
   copy .env.example .env
   ```

2. **检查连接**：确认 River key 有效，并能拿到模型列表和价格。

   ```powershell
   river-gateway check
   ```

3. **创建网关密钥**：密钥明文只显示这一次。

   ```powershell
   river-gateway keys create --name my-app --budget 20
   ```

4. **启动**：

   ```powershell
   river-gateway serve
   ```

   浏览器打开 `http://127.0.0.1:8080/`（会跳转到 `/ui/`），用 `admin_token` 登录管理控制台。

5. **调用**：把 OpenAI SDK 的 `base_url` 指向网关即可。

   ```python
   from openai import OpenAI

   client = OpenAI(api_key="rgw_...", base_url="http://127.0.0.1:8080/v1", timeout=600.0)
   with client.chat.completions.create(
       model="fast",  # 别名，或任意 River 模型 ID，如 deepseek-ai/DeepSeek-V4.1-Flash
       messages=[{"role": "user", "content": "用一句话打个招呼"}],
       stream=True,
   ) as stream:
       for chunk in stream:
           if chunk.choices:
               print(chunk.choices[0].delta.content or "", end="", flush=True)
   ```

   ```bash
   curl http://127.0.0.1:8080/v1/chat/completions \
     -H "Authorization: Bearer rgw_..." -H "Content-Type: application/json" \
     -d '{"model": "deepseek-ai/DeepSeek-V4.1-Flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 256}'
   ```

其他请求参数（如 `reasoning_effort`、`response_format`、`tools`，以及 River 扩展参数 `ignore_eos`）原样透传；在 OpenAI SDK 中，`ignore_eos` 要通过 `extra_body={"ignore_eos": True}` 传入。

## 配置

完整带注释的示例见 [config.example.yaml](config.example.yaml)。查找顺序：`-c/--config` 参数 → 环境变量 `RIVER_GATEWAY_CONFIG` → 当前目录的 `config.yaml` → 内置默认（直连 River Cloud，读取 `RIVER_API_KEY`）。配置中可用 `${VAR}` / `${VAR:-默认值}` 引用环境变量。

| 段 | 关键项 |
| --- | --- |
| `server` | `host`、`port`、`max_body_bytes`（默认 50 MiB）、`cors_origins`、`metrics`、`log_level` |
| `upstreams` | 每个上游的 `base_url`、`api_key`、`read_timeout`（默认 900 秒）、`max_concurrency`、`queue_timeout`、`inject_stream_usage` |
| `default_upstream` | 未配置路由的模型名转发到哪个上游；设为空则只允许 `models` 里的模型 |
| `models` | 路由：`name`、`upstream`、`target`、`fallbacks`、`chat_defaults`、`responses_defaults`、`price`、`hidden` |
| `retry` | `max_retries`、`backoff_base`、`backoff_max`、`max_retry_after` |
| `pricing` | 价格来源 `url`、`refresh_seconds`、手动覆盖 `overrides` |
| `validation` | 允许的图片类型、是否拦截 Responses 不支持的参数、是否默认 `store:false` |
| `key_defaults` | 新密钥默认的 `rpm`、`max_concurrency` |

### 接入专属部署（训练后的模型）

用 `river-client` 创建部署后（见 River 文档 [Deploy and serve](https://docs.river.ai/guides/deployments/)），二选一：

- **单独上游**：新增一个上游，`base_url` 填 `deployment.base_url`（已包含 `/v1`，原样填写），`api_key` 用有部署权限的团队 key；再加一条路由，`upstream` 指向它，`target` 填 `deployment.model`。
- **走公共入口**：按文档，`deployment.model`（即 `deployment.id`）在全局 `/v1` 上也可用，所以只加一条路由 `target: <deployment.id>` 即可。

专属部署按 GPU 小时计费，建议在路由上设置 `price: {input: 0, output: 0}`，避免被当成按 token 计费的模型。

## 管理控制台（/ui）

网关自带一个网页控制台，打开 `http://<网关地址>/ui/`，用配置里的 `admin_token`（默认读取 `.env` 的 `GATEWAY_ADMIN_TOKEN`）登录。控制台是纯静态页面，所有数据都来自下面的管理 API。

| 页面 | 内容 |
| --- | --- |
| 总览 | 今天 / 近 7 天 / 近 30 天的请求数、成功率、输入输出 Token（含缓存、推理）、估算费用、平均耗时；按小时或按天的趋势图（可切换请求数 / 费用 / Token，带表格视图）；按实际模型、按密钥的分布 |
| 密钥管理 | 列表、搜索、新建（明文只显示一次，并附 OpenAI SDK 用法）、修改可用模型 / 限速 / 并发 / 预算 / 有效期、停用、清零已用金额、删除；预算进度条到 80% 变黄，用尽变红 |
| River 账号 | 账号池：每个账号一张卡片，显示估算剩余额度、连接检测、余额不足标记和累计用量；可添加（先验证 key）、设为当前使用、测试连接、记录余额、编辑、删除。侧边栏一直显示当前账号 |
| 请求日志 | 最新请求在前，可按密钥筛选、只看异常、每 5 秒自动刷新、加载更多；点开一行能看到实际调用的模型、尝试次数、Token、费用、首包时间和请求 ID |
| Playground | 用网关密钥调用 `/v1/chat/completions`，和真实客户端走同一条路径（鉴权、限流、路由、回退、计费都生效）；支持流式、思考过程、中途停止，每条回复显示实际模型、尝试次数、首字时间、Token 和估算费用 |
| 状态 | 版本、运行时长、进行中的请求、配置告警（例如管理令牌太弱）、上游连接测试、模型路由、价格表（可立即刷新） |

说明：

- **登录态**：管理令牌默认只存在当前标签页（sessionStorage）；勾选"记住"后存进 localStorage。
- **Playground 密钥**：可以点按钮自动创建一个名为 `playground`、7 天后过期的网关密钥，只保存在当前浏览器；也可以粘贴已有的密钥。
- **安全**：页面带严格的 Content-Security-Policy（只允许本站脚本、禁止被嵌入 iframe），所有数据都按纯文本渲染。
- **关闭控制台**：在配置里设置 `server.ui: false`。
- **时区**：图表按浏览器所在时区分天、分小时。

不想动真实 River 账号也能先看看效果：`python scripts/dev_server.py` 会启动一个连着本地模拟 River 的网关，自带演示密钥和两周的模拟用量，然后打开 `http://127.0.0.1:8090/ui/`，管理令牌是 `river-dev-admin-token`。

## River 账号池

网关可以保存多个 River 账号，并由你手动选择当前用哪一个。所有发往账号池上游（默认是 `default_upstream`，即 `river`）的请求都用当前账号的 API key，切换后下一个请求立即生效，无需重启。每个请求都会记录是哪个账号发出的。

**为什么是录入 API key，而不是登录 River 账号：** River 没有开放“用账号密码登录换取凭证”的接口，程序化访问只能用 Console 的 [API Keys](https://console.river.ai/keys) 页面创建的 key。如果靠模拟网页登录来拿凭证，网关就得保存账号密码，还要逆向 Console 未公开的接口：这样做容易失效，很可能违反 River 的服务条款，而且密码一旦泄露，丢的是整个账号。API key 则可以在 Console 里单独作废。所以每个账号只需在 Console 建一个 key，录入一次即可。

**剩余额度怎么算：** River 也没有查询余额的接口（文档的说法是遇到 402 时去 Console 看余额），所以网关显示的是估算值：

- 剩余额度 ≈ 你从 Console 录入的余额 − 录入之后网关经这个账号产生的费用（按 River 公开价格计算）。
- 不包括在网关之外用同一个账号产生的费用（其他程序、训练任务等），所以实际余额只会更少。隔一段时间对照 Console 重新录入一次即可。
- River 返回 402（余额不足）时，账号会被自动标记，状态页和侧边栏都会提醒；该账号的请求再次成功或重新录入余额后，标记会清除。网关不会自动切换到其他账号，由你决定换哪个。

**怎么用：** 在控制台的“River 账号”页操作，或者用命令行（Docker 里在前面加 `docker compose exec gateway`）：

```powershell
river-gateway accounts list                                  # 列出账号、估算余额，* 表示当前使用
river-gateway accounts add --name 团队B --balance 20 --use   # 会提示输入 API key（不回显），验证通过后添加并切换
river-gateway accounts use acc_xxxx                          # 切换当前账号
river-gateway accounts balance acc_xxxx 18.5                 # 记录 Console 上看到的余额
river-gateway accounts check                                 # 逐个测试账号 key（调用免费的 GET /models）
river-gateway accounts remove acc_xxxx                       # 删除（不能删除当前使用的账号）
river-gateway usage --by account                             # 按账号汇总用量
```

- `.env` 里的 `RIVER_API_KEY` 会作为第一个账号（“配置文件中的 key”），默认使用它；要更换它请改 `.env`。
- 其他上游（例如专属部署）继续用各自配置的 key，不受账号切换影响；可以用配置项 `accounts.upstream` 指定账号池作用于哪个上游。
- 账号的 API key 以原文存在 SQLite 数据库里（网关要用它们发请求），管理 API 和控制台只显示 `rv_xxxx…abcd` 这样的片段。数据库文件和它的备份要像 `.env` 一样保管好。

## 密钥管理

命令行直接读写数据库，网关运行中也能立即生效：

```powershell
river-gateway keys create --name team-a --models "deepseek-ai/*,fast" --rpm 120 --concurrency 4 --budget 50 --expires-days 90
river-gateway keys list
river-gateway keys update key_xxx --rpm 0 --reset-spend   # rpm 0 = 不限；预算已用金额清零
river-gateway keys disable key_xxx
river-gateway keys delete key_xxx
river-gateway usage --days 30 --by key                   # --by model|target|upstream|key|account|day|hour
river-gateway backup                                     # 在线备份数据库（详见“备份与恢复”）
```

## 管理 API

请求头带 `Authorization: Bearer <admin_token>`。没配置 `admin_token` 时管理 API 关闭（返回 404）。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /admin/keys` · `POST /admin/keys` | 列出 / 创建密钥（创建时返回一次 `secret`） |
| `GET` · `PATCH` · `DELETE /admin/keys/{id}` | 查看 / 修改（限额、启停、预算、清零 `spent_usd`）/ 删除 |
| `GET /admin/usage?group_by=model&since=2026-10-01&key_id=` | 用量汇总；`group_by` 可选 `model`、`target`、`upstream`、`key`、`day`、`hour`；`since`/`until` 接受 Unix 秒或 ISO 8601；`tz_offset`（相对 UTC 的分钟数）决定按哪个时区分天、分小时 |
| `GET /admin/usage/recent?limit=50&before_id=&errors_only=true` | 最近的请求明细（含密钥名），用 `before_id` 往前翻页 |
| `GET /admin/models` | 所有可用的模型名：路由（包括隐藏的）及其目标、上游模型 |
| `GET /admin/upstreams/check` | 逐个测试上游连接（调用免费的 `GET /models`，账号池上游用当前账号的 key），返回状态码、耗时、模型数 |
| `GET /admin/accounts` · `POST /admin/accounts` | 列出账号（含估算余额、检测结果、余额不足标记、累计用量）/ 添加账号（`name`、`api_key`、可选 `balance_usd`、`note`；默认先验证 key） |
| `PATCH` · `DELETE /admin/accounts/{id}` | 改名称、备注、key，或记录余额（`balance_usd`）/ 删除 |
| `POST /admin/accounts/{id}/activate` · `POST /admin/accounts/{id}/check` | 设为当前使用 / 测试这个账号的 key |
| `GET /admin/pricing` · `POST /admin/pricing/refresh` | 当前价格表 / 立即刷新 |
| `GET /admin/status` | 上游、路由、密钥数、进行中的请求、新密钥默认限额、配置告警（不包含任何密钥） |

```bash
curl -X POST http://127.0.0.1:8080/admin/keys -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "team-b", "allowed_models": ["fast"], "budget_usd": 10}'
```

启动后访问 `http://127.0.0.1:8080/docs` 可以看到交互式接口文档；点右上角 **Authorize** 填入管理令牌，就能直接在页面上调用管理接口。

## 错误与响应头

网关自身产生的错误使用和 River 一致的 OpenAI 格式：`{"error": {"message", "type", "param", "code"}}`。River 返回的错误原样透传（状态码、正文、`x-request-id`、`Retry-After`）。

| 状态码 | 来源与含义 |
| --- | --- |
| 401 | 网关密钥缺失、无效、已停用或已过期（`code` 区分），或 River key 无效（透传） |
| 402 | 网关密钥预算用尽（`budget_exceeded`），或 River 余额不足（透传） |
| 403 | 该网关密钥无权使用此模型（`model_not_allowed`） |
| 413 | 请求体超过 `max_body_bytes` |
| 429 | 密钥 RPM 或并发超限（带 `Retry-After`），或 River 限流且 `Retry-After` 过长 |
| 502 / 503 / 504 | 上游不可达 / 上游排队超时或过载 / 上游超时 |

`chat/completions` 和 `responses` 的每个响应都会带 `x-gateway-request-id`；转发到上游的请求还会带 `x-gateway-upstream`、`x-gateway-model`（实际使用的模型，回退后会变）、`x-gateway-attempts`。流在中途断开时，网关会发出一个 `error` 事件、不发 `[DONE]`，OpenAI SDK 会据此抛出 `APIError`。

## 可观测性

- `GET /healthz`：存活检查。
- `GET /metrics`：`river_gateway_requests_total`、`river_gateway_request_duration_seconds`、`river_gateway_time_to_first_event_seconds`、`river_gateway_tokens_total`、`river_gateway_cost_usd_total`、`river_gateway_upstream_retries_total`、`river_gateway_fallbacks_total`、`river_gateway_inflight_requests`。
- 访问日志：每个请求一行，包含密钥名、请求模型 → 实际模型@上游、状态码、尝试次数、token、成本、耗时、首事件延迟和 River 的 request id。不会记录请求内容和任何 key。

## 与 River 文档的对应

| River 文档中的说明 | 网关行为 |
| --- | --- |
| API key 放在服务器上，不要进浏览器和源码（Inference quickstart） | 客户端只持有网关密钥；River key 只出现在网关配置里 |
| `chat/completions`、`responses`、`models` 三个端点（Inference quickstart） | 同名端点，OpenAI SDK 只改 `base_url` 即可 |
| 请求体最大 50 MiB；图片只支持 JPEG、PNG（Limits、Images） | 转发前拦截，返回 413 / 400 |
| 429/503 退避加抖动并遵守 `Retry-After`；500/502/504 有限重试（Limits） | 内置重试策略，等待时间过长时改走回退 |
| 流不能续传，重试是新的计费请求（Limits） | 流开始后不重试；中途失败时发 error 事件；只在请求未送达时重试连接错误 |
| prefill 最长 10 分钟，非流式响应最长 15 分钟（Limits） | 默认读超时 900 秒 |
| Responses 不支持 `store:true`、`background`、`previous_response_id`、`conversation`（API controls） | 直接返回 400，并默认补上 `store:false` |
| 价格以 JSON 公布，缓存输入按缓存价计费，推理 token 计入输出（Models and pricing） | 自动拉取价格表，按同样口径估算成本 |
| 联系支持时提供 `x-request-id`（如有）（Limits） | 有就透传并记录；没有时在用量日志里记录响应 id |
| 专属部署提供 OpenAI 兼容 `base_url`（Deploy and serve） | 可配置为上游，与公共模型统一路由 |

## 局限

- **只代理推理**：River 的训练（SFT、RL、蒸馏）和部署管理走 `river-client` 的 gRPC 接口，没有公开的 HTTP 协议，网关不代理这部分，请直接使用 `river-client`。
- **单实例限流**：RPM、并发和上游排队的状态在进程内存中，请运行单个网关进程；多实例需要把 `RateLimiter` 换成 Redis 等共享存储。密钥和用量在 SQLite 中，可多个进程共享读写。
- **成本是估算**：按 River 公开价格计算，以 River Console 账单为准。流式对话的 usage 依赖 `stream_options.include_usage`，River 文档没提这个参数，但 2026-10-26 实测 River 接受它。如果以后 River 拒绝它，网关会自动停止添加，之后流式对话就不再计入成本。
- **真实环境验证（2026-10-26）**：`scripts/live_check.py` 通过网关对真实 `api.river.ai` 做了 Chat Completions 和 Responses 各一次流式、一次非流式调用（`fast` → DeepSeek-V4.1-Flash），全部成功，token 和费用都记录正确；一次检查约花 $0.00004。回退、重试和限流在真实环境里不好触发，这几项靠模拟 River 的测试覆盖。

## 开发

```powershell
pytest                        # 74 个单元/集成测试，使用 tests/fake_river.py 模拟 River
python scripts/smoke_test.py  # 用 uvicorn 起真实进程，用官方 openai SDK 端到端验证（模拟 River）
python scripts/live_check.py  # 对真实 River 做流式/非流式调用（用你的 key，临时数据库，约 $0.00004）
python scripts/dev_server.py  # 连本地模拟 River 的网关 + 演示数据，用来看控制台（端口 8090）
```

控制台在浏览器里验证过（2026-10-26，模拟 River）：登录、总览（时间范围、指标切换、悬停提示）、新建密钥并显示明文、Playground 的流式输出 / 回退 / 中途停止、请求日志的筛选和详情、状态页的上游连接测试，以及深色模式和手机宽度下的布局。浏览器控制台没有报错。

```
src/river_gateway/
  app.py         FastAPI 应用工厂与路由
  proxy.py       核心流程：鉴权、限流、转发、流式代理、记账
  upstream.py    上游 HTTP 客户端、重试与退避、Retry-After
  routing.py     别名、专属部署、回退、默认参数
  validation.py  River 不支持请求的提前拦截
  usage.py       usage 解析、SSE 解析、流跟踪
  pricing.py     价格表与成本计算
  store.py       SQLite：密钥与用量
  ratelimit.py   RPM 令牌桶与并发限制
  admin.py       管理 API
  accounts.py    River 账号池：当前账号、余额估算、402 标记
  backup.py      在线备份与恢复（SQLite 备份接口、完整性检查、轮换）
  metrics.py     Prometheus 指标
  config.py      配置加载与校验
  cli.py         命令行
  ui/            管理控制台（index.html、style.css、js/ 下按页面分的原生 JS 模块）
tests/           pytest 测试与模拟 River（fake_river.py）
scripts/         冒烟测试、真实环境检查、控制台演示服务器、服务器定时备份脚本
```

Docker 的用法见上面的“用 Docker 启动”（`Dockerfile` + `docker-compose.yml`）。
