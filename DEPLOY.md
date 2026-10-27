# River Gateway 服务器部署手册

> 这份手册写给**执行部署的 AI 代理**，人也可以照着做。按章节顺序执行，每一步都写了“预期结果”。结果和预期不符就去第 11 节排查；同一步连续失败两次，就停下来把命令和输出报告给人，不要自行改代码，也不要跳过步骤。

## 0. 执行规则（必须遵守）

1. **身份与目录**：在服务器上以 root 执行（先 `sudo -i`）。项目目录固定为 `/opt/river-gateway`，除非特别说明，下面的命令都在这个目录里执行。
2. **不泄露机密**：`RIVER_API_KEY`、`GATEWAY_ADMIN_TOKEN`、`.env` 的内容，以及任何 `rv_` 或 `rgw_` 开头的字符串，一律不要打印、回显，也不要写进日志、报告或版本库。需要确认时只看长度和前 3 个字符（4.2 节有现成命令）。
3. **不删数据**：禁止执行 `docker compose down -v`、`docker volume rm`、`docker volume prune`、`docker system prune --volumes`。网关的全部数据（网关密钥、River 账号、用量记录）都在 Docker 卷 `river-gateway_gateway-data` 里。
4. **不改代码**：只改 `.env`、`config.yaml`、反向代理配置和 crontab。`src/`、`Dockerfile`、`docker-compose.yml` 保持原样；唯一的例外见第 11 节“端口被占用”。
5. **不直接暴露 8080**：网关只监听 `127.0.0.1:8080`。需要从公网访问时，按 8B 加反向代理和 HTTPS。
6. **不产生费用**：验证只用免费接口（`/healthz`、`river-gateway check`、`/v1/models`）。人没有明确要求时，不要发对话请求。
7. **缺信息就问**：第 1 节列出的信息缺了就向人要，不要自己编（尤其是域名、代理地址、镜像加速地址、邮箱）。

## 1. 开始前向人确认的信息

| 信息 | 是否必需 | 说明 |
| --- | --- | --- |
| 服务器登录方式 | 必需 | IP、用户名、SSH 密钥或密码，用户需要有 sudo 权限 |
| `RIVER_API_KEY` | 必需 | 在 River Console → API Keys 创建，以 `rv_` 开头。也可以让人自己在服务器上输入，这样 key 不经过你（见 4.2） |
| 访问方式 | 必需 | **A 私有**（默认，个人使用推荐）：网关只在服务器本机监听，人通过 SSH 隧道访问；**B 公网 HTTPS**：通过域名访问 |
| 域名 | 方式 B 必需 | 已经把 A 记录解析到服务器的公网 IP。服务器在中国大陆时，域名还需要先完成 ICP 备案，否则云厂商会拦截 80/443 端口 |
| 证书相关的同意 | 方式 B 必需 | HTTPS 证书来自 Let's Encrypt，申请即表示同意其服务条款，需要人确认；用 Nginx 方案时还要一个联系邮箱 |
| 允许访问控制台的公网 IP | 方式 B 可选 | 填了以后只有这些 IP 能打开控制台和管理接口，推荐填写 |
| 旧数据备份文件 | 可选 | 从旧网关迁移数据时用，见第 7 节 |
| 网络限制 | 可选 | 服务器访问 pypi.org、Docker Hub 或 api.river.ai 有困难时，需要 PyPI 镜像、镜像加速地址或代理地址 |

## 2. 服务器要求与预检

要求：Ubuntu 22.04 / 24.04 或 Debian 12（x86_64 或 arm64），至少 1 核 CPU、1 GB 内存、5 GB 可用磁盘。

```bash
. /etc/os-release && echo "$PRETTY_NAME $(uname -m)"
nproc; free -h | sed -n 2p; df -h / | tail -1
curl -sS -o /dev/null -w "api.river.ai -> %{http_code}\n" --max-time 15 https://api.river.ai/v1/models
curl -sS -o /dev/null -w "pypi.org -> %{http_code}\n" --max-time 15 https://pypi.org/simple/pip/
curl -sS -o /dev/null -w "docker hub -> %{http_code}\n" --max-time 15 https://registry-1.docker.io/v2/
ss -ltn | grep -E ':(80|443|8080) ' || echo "ports 80/443/8080 free"
```

预期：
- 系统是上面列出的版本之一。
- `api.river.ai -> 401`：能连通。返回 401 是因为请求没带 key，属于正常。
- `pypi.org -> 200`，`docker hub -> 401`。
- 8080 端口空闲；方式 B 还要求 80 和 443 空闲。

不符合预期时：
- `api.river.ai` 连不上：按 4.4 配置代理（代理地址向人要）。
- `pypi.org` 连不上或很慢：按 4.4 设置 PyPI 镜像。
- Docker Hub 连不上：按第 3 节末尾配置镜像加速（加速地址向人要）。
- 8080 被占用：见第 11 节“端口被占用”。

## 3. 安装 Docker

先检查是否已经装好：

```bash
docker compose version
```

如果输出 `Docker Compose version v2.x` 或更高版本，直接跳到第 4 节。否则安装 Docker 官方软件源里的版本：

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version && docker compose version
```

预期：最后两行分别输出 Docker 版本和 `Docker Compose version v2...`。

- `download.docker.com` 连不上（常见于中国大陆）：把上面两处 `https://download.docker.com` 换成 `https://mirrors.aliyun.com/docker-ce`，再执行一遍。
- 拉取镜像时连不上 Docker Hub：向人要镜像加速地址，写入 `/etc/docker/daemon.json` 后重启 Docker。如果这个文件已经存在，要把 `registry-mirrors` 合并进去，不要覆盖原有内容。

  ```bash
  cat > /etc/docker/daemon.json <<'EOF'
  { "registry-mirrors": ["<人提供的加速地址>"] }
  EOF
  systemctl restart docker
  ```

## 4. 放置项目并配置

### 4.1 上传项目代码

在开发机上打包。下面是 Windows PowerShell 的写法，项目目前在 `F:\Program\Project\AI\river-gateway`。

```powershell
cd F:\Program\Project\AI
C:\Windows\System32\tar.exe -czf river-gateway.tar.gz --exclude=.venv --exclude=.env --exclude=config.yaml --exclude=data --exclude=backups --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache --exclude="*.egg-info" --exclude=.claude --exclude="*.db" river-gateway
scp river-gateway.tar.gz root@<服务器IP>:/tmp/
```

这里必须写 Windows 自带 tar 的完整路径。如果 PATH 里有 Git 自带的 Unix 工具，单写 `tar` 会调用到 GNU tar，它遇到带盘符的路径会报 `Cannot connect to C: resolve failed`。Linux/macOS 上直接用 `tar`，参数完全相同。

压缩包里不含任何机密：`.env`、`config.yaml`、数据库和备份都已排除。

在服务器上解压：

```bash
tar -xzf /tmp/river-gateway.tar.gz -C /opt
cd /opt/river-gateway
ls -a
```

预期：能看到 `.env.example  DEPLOY.md  Dockerfile  README.md  config.example.yaml  deploy  docker-compose.yml  pyproject.toml  scripts  src  tests`，并且**没有** `.env`、`config.yaml`、`data`。

把脚本和代理模板统一转成 Unix 换行，防止在 Windows 上编辑过的文件带着 CRLF 过来，导致 bash 报错：

```bash
find /opt/river-gateway/scripts /opt/river-gateway/deploy -type f -exec sed -i 's/\r$//' {} +
grep -rlI $'\r' /opt/river-gateway/scripts /opt/river-gateway/deploy || echo "line endings OK"
```

预期：`line endings OK`。

### 4.2 创建 `.env`（机密）

先生成文件和随机的管理令牌。文件已存在时这一步不做任何事，重复执行不会改掉已有的令牌。

```bash
cd /opt/river-gateway
[ -f .env ] || ( umask 077 && printf 'RIVER_API_KEY=\nGATEWAY_ADMIN_TOKEN=%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > .env )
```

再填入 River API key，下面两种方式二选一：

- **人自己在服务器上输入（推荐，key 不经过你）**：让人执行下面这行，粘贴 key 后回车（输入不会显示）：

  ```bash
  read -rsp 'RIVER_API_KEY: ' K; echo; sed -i "s|^RIVER_API_KEY=.*|RIVER_API_KEY=${K}|" .env; unset K
  ```

- **人把 key 交给你**：用下面这行写入，之后不要在任何地方重复这个值：

  ```bash
  K='<人提供的 rv_ 开头的 key>'; sed -i "s|^RIVER_API_KEY=.*|RIVER_API_KEY=${K}|" .env; unset K
  ```

检查（不会输出机密）：

```bash
stat -c '%a' .env
awk -F= '/^RIVER_API_KEY=/{print "RIVER_API_KEY length=" length($2) " prefix=" substr($2,1,3)} /^GATEWAY_ADMIN_TOKEN=/{print "GATEWAY_ADMIN_TOKEN length=" length($2)}' .env
```

预期：`600`；`RIVER_API_KEY length=` 后面是 40 以上的数字、`prefix=rv_`；`GATEWAY_ADMIN_TOKEN length=48`。

`GATEWAY_ADMIN_TOKEN` 就是控制台的登录密码。不要把它写进报告，而是告诉人自己用 `grep ^GATEWAY_ADMIN_TOKEN= /opt/river-gateway/.env` 查看。

### 4.3 创建 `config.yaml`

```bash
[ -f config.yaml ] || cp config.example.yaml config.yaml
chmod 644 config.yaml
```

默认配置不需要改。权限必须是 644：容器里的网关以非 root 用户读取这个文件。

可选调整：如果浏览器里的网页应用要直接调用网关，就在 `config.yaml` 的 `server.cors_origins` 里加上这些应用的地址。

### 4.4 网络设置（仅在第 2 节预检失败时做）

pypi.org 连不上或很慢时，让构建使用 PyPI 镜像：

```bash
echo 'PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple' >> .env
```

连不上 api.river.ai 时，给网关配置代理（地址向人要）：

```bash
cat >> .env <<'EOF'
HTTPS_PROXY=http://<代理主机>:<端口>
NO_PROXY=localhost,127.0.0.1
EOF
```

代理地址必须是容器内部能访问到的地址，不能写 `127.0.0.1`（在容器里那指的是容器自己）。如果代理运行在这台服务器上，写 `http://host.docker.internal:<端口>`，并确认代理没有只监听 127.0.0.1。

## 5. 构建并启动

```bash
cd /opt/river-gateway
docker compose up -d --build
for i in $(seq 1 40); do s=$(docker inspect -f '{{.State.Health.Status}}' river-gateway 2>/dev/null); [ "$s" = healthy ] && break; sleep 3; done; echo "health: $s"
```

预期：构建以 `Built` 结束，随后输出 `health: healthy`。首次构建需要下载 Python 镜像和依赖，一般 1～5 分钟。

状态不是 `healthy` 时，先看日志 `docker compose logs --tail 80`，再对照第 11 节排查。

## 6. 验证（全部通过才算部署成功）

```bash
cd /opt/river-gateway
docker compose ps --format '{{.Name}} {{.Status}} {{.Ports}}'
curl -s http://127.0.0.1:8080/healthz; echo
docker compose exec -T gateway river-gateway check
curl -s -o /dev/null -w 'console -> %{http_code}\n' http://127.0.0.1:8080/ui/
curl -s -o /dev/null -w 'admin api -> %{http_code}\n' -H "Authorization: Bearer $(sed -n 's/^GATEWAY_ADMIN_TOKEN=//p' .env)" http://127.0.0.1:8080/admin/status
docker compose exec -T gateway river-gateway accounts list
```

| 检查项 | 预期 |
| --- | --- |
| `ps` | `river-gateway Up ... (healthy) 127.0.0.1:8080->8080/tcp` |
| `healthz` | `{"status":"ok","version":"0.1.0"}` |
| `check` | 有 `[ OK ] upstream river (https://api.river.ai/v1): key accepted, N models` 和 `[ OK ] pricing`；没有 `[FAIL]`，也没有 `[WARN] admin_token` |
| 控制台 | `console -> 200` |
| 管理接口 | `admin api -> 200` |
| `accounts list` | 有一行以 `*    acc_config` 开头（`.env` 里的 River key 是当前账号） |

再做一次端到端检查，这一步不花钱：建一个临时网关密钥，通过网关拉取模型列表，然后删掉这个密钥。

```bash
OUT=$(docker compose exec -T gateway river-gateway keys create --name deploy-check --expires-days 1)
KEY_ID=$(printf '%s\n' "$OUT" | sed -n 's/^Created key \(key_[0-9a-f]*\).*/\1/p')
SECRET=$(printf '%s\n' "$OUT" | sed -n 's/^ *secret: //p')
curl -s -H "Authorization: Bearer $SECRET" http://127.0.0.1:8080/v1/models | head -c 300; echo
docker compose exec -T gateway river-gateway keys delete "$KEY_ID"
unset OUT SECRET KEY_ID
```

预期：先输出以 `{"object":"list","data":[{"id":"fast"` 开头的 JSON，再输出 `Deleted key key_...`。

## 7. （可选）从旧网关导入数据

**在旧机器上**（旧网关所在的机器，例如人的 Windows 电脑）进入项目目录，导出备份：
- Linux / macOS / PowerShell 7.4+：`docker compose exec -T gateway river-gateway backup --output - > gateway-backup.db`
- Windows PowerShell 5.1（它的 `>` 会损坏二进制文件，不能用上面那条）：先执行 `docker compose exec gateway river-gateway backup`，再执行 `docker compose cp gateway:/app/data/backups ./backups`，取 `backups` 里最新的 `gateway-*.db`。

然后上传：`scp gateway-backup.db root@<服务器IP>:/tmp/gateway-backup.db`

**在服务器上**导入，并删除上传的文件：

```bash
cd /opt/river-gateway
docker compose exec -T gateway river-gateway restore - --yes < /tmp/gateway-backup.db
docker compose exec -T gateway river-gateway keys list
docker compose exec -T gateway river-gateway accounts list
rm -f /tmp/gateway-backup.db
```

预期：
- 输出 `Restored /app/data/gateway.db (integrity ok; rows: ...)`，随后能列出旧网关的密钥和 River 账号。
- 旧的网关密钥继续有效，应用不用换 key。
- `acc_config` 这个账号使用的是本服务器 `.env` 里的 key。
- 上传的备份里有 River key，所以用完就删。

## 8. 开放访问

### 8A 私有访问（默认）

服务器上不用做任何配置。人在自己电脑上开一个 SSH 隧道（Windows 10+ 自带 `ssh`）：

```powershell
ssh -N -L 18080:127.0.0.1:8080 root@<服务器IP>
```

隧道窗口保持打开期间：
- 控制台：浏览器打开 `http://127.0.0.1:18080/`，用 `GATEWAY_ADMIN_TOKEN` 登录。
- 应用：Base URL 填 `http://127.0.0.1:18080/v1`，API Key 填在控制台“密钥管理”里创建的 `rgw_…` 网关密钥。

本地端口用 18080，是为了不和人电脑上可能还在运行的本地网关（8080）冲突。

### 8B 公网 HTTPS（推荐用 Caddy，自动申请和续期证书）

前提：域名已解析到本机；云厂商的安全组或防火墙放行了 TCP 80 和 443 入站；人已确认同意 Let's Encrypt 的条款（见第 1 节）。

安装 Caddy：

```bash
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy
```

用项目里的模板生成配置。模板已经设置好：屏蔽 `/metrics`、流式输出不缓冲、超时放宽到 River 的上限。

```bash
DOMAIN=gateway.example.com     # ← 换成人提供的域名
sed "s/gateway.example.com/${DOMAIN}/" /opt/river-gateway/deploy/Caddyfile > /etc/caddy/Caddyfile
```

可选，推荐：只允许人提供的 IP 打开控制台、管理接口和 API 文档。

```bash
MYIP='203.0.113.7'             # ← 换成人提供的公网 IP，多个用空格分隔
sed -i -e 's/^\t# @console {/\t@console {/' -e 's/^\t# \tpath/\t\tpath/' -e "s/^\t# \tnot remote_ip 203.0.113.7/\t\tnot remote_ip ${MYIP}/" -e 's/^\t# }/\t}/' -e 's/^\t# respond @console 403/\trespond @console 403/' /etc/caddy/Caddyfile
```

校验并生效：

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
sleep 15
curl -s "https://${DOMAIN}/healthz"; echo
curl -s -o /dev/null -w 'metrics -> %{http_code}\n' "https://${DOMAIN}/metrics"
```

预期：`Valid configuration`；通过 HTTPS 返回 healthz 的 JSON；`metrics -> 404`。HTTPS 不通时，看 `journalctl -u caddy --no-pager -n 50`：常见原因是域名没解析到本机、80 端口被拦截，或者域名没有 ICP 备案。

应用里的 Base URL 填 `https://<域名>/v1`。

**改用 Nginx 时**（服务器上已经在用 Nginx）：

```bash
apt-get install -y nginx certbot python3-certbot-nginx
DOMAIN=gateway.example.com     # ← 换成人提供的域名
sed "s/gateway.example.com/${DOMAIN}/" /opt/river-gateway/deploy/nginx.conf > /etc/nginx/conf.d/river-gateway.conf
nginx -t && systemctl reload nginx
certbot --nginx -d "${DOMAIN}" --redirect -m <人提供的邮箱> --agree-tos --non-interactive
```

`deploy/nginx.conf` 已经把请求体上限设为 50 MB（Nginx 默认只允许 1 MB），并关闭了缓冲、把超时设为 900 秒。要限制 IP，取消文件里 `location ~ ^/(ui|admin|...)` 那一段的注释，把 IP 换成人提供的。

## 9. 定时备份

```bash
cd /opt/river-gateway
bash scripts/server-backup.sh
ls -l backups/
( crontab -l 2>/dev/null | grep -v 'server-backup.sh'; echo '0 3 * * * bash /opt/river-gateway/scripts/server-backup.sh >> /opt/river-gateway/backups/backup.log 2>&1' ) | crontab -
crontab -l | grep server-backup
```

预期：
- 第一次运行输出 `backup written: backups/gateway-日期-时间.db (…K)`。
- `ls` 显示的文件权限是 `-rw-------`。
- `crontab -l` 能看到那一行：每天 03:00 备份，保留 30 天。

这些备份和数据库在同一台服务器上，服务器出故障时会一起丢失。提醒人把 `backups/` 定期同步到其他机器或对象存储（不要自行配置，需要人提供目标和凭据）。备份的原理和恢复方法见 README 的“备份与恢复”。

## 10. 日常运维

以下命令都在 `/opt/river-gateway` 下执行。

| 做什么 | 命令 |
| --- | --- |
| 看状态 | `docker compose ps` |
| 看日志 | `docker compose logs -f --tail 100` |
| 改了 `config.yaml` | `docker compose restart` |
| 改了 `.env` | `docker compose up -d` |
| 立即备份 | `bash scripts/server-backup.sh` |
| 从备份恢复 | `docker compose exec -T gateway river-gateway restore - --yes < backups/<文件名>.db` |
| 检查 River 连接 | `docker compose exec -T gateway river-gateway check` |
| 查看用量 | `docker compose exec -T gateway river-gateway usage --by key` |
| 更换管理令牌 | 见下方“更换管理令牌” |
| 更换 `.env` 里的 River key | 用 4.2 的方式写入新 key，然后 `docker compose up -d` |

服务器重启后，Docker 会开机自启（`systemctl is-enabled docker` 应输出 `enabled`），容器也会自动拉起（`restart: unless-stopped`）。

**更换管理令牌**（执行后告诉人用新令牌重新登录控制台）：

```bash
sed -i "s|^GATEWAY_ADMIN_TOKEN=.*|GATEWAY_ADMIN_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env && docker compose up -d
```

**升级到新版本代码：**

```bash
cd /opt/river-gateway
bash scripts/server-backup.sh                                                       # 1. 先备份数据
tar -czf /opt/river-gateway-prev.tar.gz -C /opt --exclude=river-gateway/backups river-gateway   # 2. 保存旧版本，用于回滚
tar -xzf /tmp/river-gateway.tar.gz -C /opt                                          # 3. 解压新版本（.env、config.yaml、backups/ 不在包里，会原样保留）
find scripts deploy -type f -exec sed -i 's/\r$//' {} +                             # 4. 统一换行符（同 4.1）
docker compose up -d --build                                                        # 5. 重新构建并启动
```

升级后重新执行第 6 节的前四项检查。新版本可能在 `config.example.yaml` 里增加了配置项，它们都有默认值，`config.yaml` 不改也能运行。

**回滚：**

```bash
tar -xzf /opt/river-gateway-prev.tar.gz -C /opt && cd /opt/river-gateway && docker compose up -d --build
```

数据库结构的变化都是只增不删，旧版本代码可以直接使用新数据库。只有回滚后出现数据相关的错误时，才用升级前的那份备份恢复。

## 11. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 运行脚本报 `$'\r': command not found` 或 `pipefail\r: invalid option name` | 文件带着 Windows 换行（CRLF）。执行 4.1 节里的换行符转换命令后重试 |
| 构建在 `pip install` 一步失败或卡住 | 连不上 PyPI。按 4.4 设置 `PIP_INDEX_URL`，然后重新执行 `docker compose up -d --build` |
| 拉取 `python:3.12-slim` 失败 | 连不上 Docker Hub。按第 3 节末尾配置镜像加速 |
| `check` 显示 `[FAIL] ... HTTP 401` | River key 无效或被作废。让人确认 key，按 4.2 重新写入后执行 `docker compose up -d` |
| `check` 显示连接错误（ConnectError / 超时） | 服务器访问不了 api.river.ai。按 4.4 配置代理 |
| 有 `[WARN] admin_token` | `GATEWAY_ADMIN_TOKEN` 为空或太短。执行第 10 节的“更换管理令牌” |
| 控制台提示“管理 API 未启用” | 同上，`GATEWAY_ADMIN_TOKEN` 为空 |
| 容器一直不是 healthy，日志里有 `Permission denied: '/app/config.yaml'` | 执行 `chmod 644 config.yaml` 后 `docker compose restart` |
| 日志里有 `invalid configuration` | `config.yaml` 写错了。用 `cp config.example.yaml config.yaml` 恢复默认配置，再重新做需要的修改 |
| 端口被占用（启动时报 `port is already allocated`） | 这是规则 4 唯一允许的例外：只把 `docker-compose.yml` 里 `"127.0.0.1:8080:8080"` 冒号前面的 8080 改成空闲端口（如 `"127.0.0.1:18081:8080"`），反向代理配置和 SSH 隧道命令里的 8080 也要一起改 |
| 公网访问时流式输出一次性才到 | 反向代理在缓冲。必须使用 `deploy/` 里的模板（Caddy 的 `flush_interval -1`，Nginx 的 `proxy_buffering off`） |
| 公网访问时长请求约 60 秒后报 502/504 | 反向代理超时太短。必须使用模板里的超时设置 |
| 公网访问时上传图片报 413 | Nginx 的 `client_max_body_size` 没有改成 `50m` |
| HTTPS 证书申请失败 | 域名没解析到本机、80 端口被拦截，或域名没有 ICP 备案。看 `journalctl -u caddy -n 50` 或 certbot 的输出 |
| 网页应用报 CORS 错误 | 在 `config.yaml` 的 `server.cors_origins` 里加上应用的地址，然后 `docker compose restart` |

## 12. 部署完成后向人汇报（模板）

不要在报告里写任何机密。

```text
部署结果：成功（或：失败，卡在第 X 步，原因……）
访问方式：A 私有，SSH 隧道命令 ssh -N -L 18080:127.0.0.1:8080 root@<IP>，然后打开 http://127.0.0.1:18080/
        （或：B 公网，https://<域名>/，控制台限制为 IP …）
管理令牌：在服务器 /opt/river-gateway/.env 的 GATEWAY_ADMIN_TOKEN，用 grep ^GATEWAY_ADMIN_TOKEN= /opt/river-gateway/.env 查看
应用接入：Base URL = <上面的地址>/v1，API Key 在控制台“密钥管理”里创建
验证：healthz 正常；check 全部 OK（N 个模型）；控制台 200；/v1/models 正常
数据：新建数据库（或：已从旧网关导入，网关密钥 N 个、River 账号 N 个）
备份：每天 03:00 备份到 /opt/river-gateway/backups/，保留 30 天；首个备份 <文件名>
需要你处理：把 backups/ 同步到服务器以外的地方；（其他……）
```

## 附录：文件与数据位置

| 内容 | 位置 |
| --- | --- |
| 代码 | `/opt/river-gateway` |
| 机密（River key、管理令牌） | `/opt/river-gateway/.env`（权限 600） |
| 配置（路由、限额、上游） | `/opt/river-gateway/config.yaml` |
| 数据库（网关密钥、River 账号、用量） | Docker 卷 `river-gateway_gateway-data`，容器内路径 `/app/data/gateway.db` |
| 容器内的备份 | `/app/data/backups/`（`river-gateway backup` 生成） |
| 服务器上的备份 | `/opt/river-gateway/backups/`（`scripts/server-backup.sh` 生成，cron 日志 `backup.log`） |
| 反向代理模板 | `/opt/river-gateway/deploy/Caddyfile`、`/opt/river-gateway/deploy/nginx.conf` |
| 网关端口 | `127.0.0.1:8080`（只在本机监听） |
| 回滚用的旧版本 | `/opt/river-gateway-prev.tar.gz`（升级时生成） |
