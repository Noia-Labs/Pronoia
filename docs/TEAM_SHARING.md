# Pronoia 团队分享与容器部署

这套配置用于把 Pronoia 作为一个单容器网站交给开发同事运行。前端在构建阶段编译，运行时由 FastAPI 同源托管；密钥、SQLite、行情数据、回测结果和日志不会进入镜像。

> 分享版内置一个全站 HTTP Basic 团队账号，但还没有逐用户权限、租户隔离、限流和计费保护。它适合受控团队内测；不要把容器的 `8000` 端口直接暴露到公网。公开域名前仍须使用 HTTPS，并建议在反向代理层增加 SSO/访问网关。

## 0. ZIP 分享版直接启动

同事解压 ZIP 后双击 `Pronoia 启动器.command`。首次运行会交互填写模型 API URL、隐藏输入 API Key、填写模型名称，并把它们安全保存在本机 `.env`；可使用 DeepSeek、火山方舟等模型服务的兼容接口。以后双击会直接复用配置，需要换接口时双击 `Pronoia API 重设.command`。详细步骤见根目录 `SHARE_README.md`。

API URL 必须使用 HTTPS，只有 `localhost` 或 `127.0.0.1` 的本机模型服务允许 HTTP。分享 ZIP 不包含 `.env`、发送者数据或历史结果。

## 1. 哪些内容进入 GitHub

建议先使用 GitHub 私有仓库邀请同事。即使以后公开源码，也只提交程序、测试和脱敏样例，不提交以下内容：

- `.env`、`.env.share`、API Key、证书或其他凭据；
- `backend/fever.db*` 或任何 SQLite 文件；
- `data/`、私有 `backtesting/` 数据、回测结果和轨迹；
- `frontend/node_modules/`、`frontend/dist/`、运行日志和缓存。

`.gitignore` 负责阻止这些文件进入 Git，`.dockerignore` 负责阻止它们进入 Docker 构建上下文；两者不能互相替代。提交前仍应检查：

```bash
git status --short
git ls-files \
  | grep -Ev '(^|/)\.env(\.share)?\.example$' \
  | grep -E '(^|/)(\.env($|\.)|data/|backtesting/)|\.db($|-)|\.log$'
```

第二条命令没有输出才是理想结果。不要使用 `git add -f` 绕过忽略规则。如果任何密钥曾经进入提交历史，应先撤销并更换密钥，再清理 Git 历史。

## 2. 本机或团队服务器启动

需要 Docker Engine 和 Docker Compose v2。先创建只保存在部署机器上的运行时配置：

```bash
cp .env.share.example .env.share
chmod 600 .env.share
```

编辑 `.env.share`，先填写 `PRONOIA_SHARE_PASSWORD`（至少 12 个字符），再填写一个可用的模型配置。默认使用 `ARK_API_*`；只有 `MAAS_API_URL`、`MAAS_API_KEY`、`MAAS_MODEL` 三项都填写时才会优先使用 MAAS。账号和 API Key 只在运行时注入，不会进入镜像。

构建并启动：

```bash
docker compose --env-file .env.share -f docker-compose.share.yml build
docker compose --env-file .env.share -f docker-compose.share.yml up -d
docker compose --env-file .env.share -f docker-compose.share.yml ps
curl http://127.0.0.1:8000/api/health
curl -u 'pronoia:<你的分享密码>' http://127.0.0.1:8000/api/skills
```

查看日志与停止服务：

```bash
docker compose --env-file .env.share -f docker-compose.share.yml logs -f --tail=200
docker compose --env-file .env.share -f docker-compose.share.yml down
```

`down` 不会删除命名卷；只有显式使用 `down -v` 才会删除持久数据，团队服务器上不要随意执行。

## 3. 存储结构与导入数据

Compose 创建 `pronoia-state` 命名卷并挂载到 `/var/lib/pronoia`：

| 容器路径 | 环境变量 | 内容 |
|---|---|---|
| `/var/lib/pronoia/fever.db` | `FEVER_DB_PATH` | 案例、Run、Arena 和数据集元数据 |
| `/var/lib/pronoia/data` | `FEVER_DATA_DIR` | 行情快照、回测结果、交易 CSV 和轨迹 |

新环境可以从空卷开始。历史行情体积较大或包含授权限制时，应通过私有对象存储、服务器磁盘或离线传输提供，不能上传到源码仓库，也不能在 `Dockerfile` 中 `COPY`。

如果希望直接管理宿主机目录，可把 Compose 中的卷替换为明确的绝对路径：

```yaml
volumes:
  - /srv/pronoia/state:/var/lib/pronoia
```

首次使用前创建目录并赋予容器用户权限：

```bash
sudo install -d -o 10001 -g 10001 /srv/pronoia/state/data
```

把市场数据放入 `/srv/pronoia/state/data`，把数据库放在 `/srv/pronoia/state/fever.db`。迁移 SQLite 时必须先停止源服务，并把主库及存在的 `-wal`、`-shm` 文件作为同一个快照一起迁移；更稳妥的方式是先执行 SQLite 在线备份或 checkpoint。迁移完成后保持所有文件归 `10001:10001` 所有。

旧版 `backtesting/` 事件集由启动扫描器从 `/app/backtesting` 读取。如果需要这些私有事件集，请在 Compose 中另加只读挂载，不要复制进仓库或镜像：

```yaml
volumes:
  - /srv/pronoia/state:/var/lib/pronoia
  - /srv/pronoia/backtesting:/app/backtesting:ro
```

现有审计数据约 60 MiB；长区间分钟回测单次可产生约 34 MiB。团队起步建议 10 GiB 持久盘，大量分钟回测建议 20 GiB 以上，并建立归档、配额和清理策略。数据库和数据卷要做独立备份，重新构建镜像不等于备份。

## 4. 端口、域名与 HTTPS

`.env.share` 中：

- `PORT` 是容器内监听端口；
- `PRONOIA_HOST_PORT` 是宿主机端口；
- `PRONOIA_BIND_ADDRESS=127.0.0.1` 默认只允许本机反向代理访问。

团队局域网临时访问可以在可信网络中改为 `0.0.0.0`，同时启用防火墙白名单。HTTP Basic 在纯 HTTP 上不会加密传输账号密码，所以只应在你信任的局域网中这样做。公网部署应保持 loopback 绑定，在同一服务器使用 Caddy、Nginx 或受管网关把 `https://your-domain.example` 反向代理到 `http://127.0.0.1:8000`。

自定义域名必须满足：

1. 有效 DNS 记录和自动续期的 TLS 证书；
2. HTTP 强制跳转 HTTPS，浏览器只访问 HTTPS；
3. 在代理层增加 SSO、访问网关或至少密码认证；
4. 对创建回测、Agent 调用、导出和管理接口设置请求体限制、速率限制和审计日志；
5. 只信任必要的代理来源，不直接开放容器端口。

如果前端和 API 被拆到不同域名，再把准确的 HTTPS 前端源地址写入 `PRONOIA_CORS_ORIGINS`；不要使用 `*`。当前单容器部署为同源访问，通常无需配置 CORS。

## 5. 运行边界与建议规格

容器以 UID/GID `10001` 非 root 用户运行，删除 Linux capabilities、启用只读根文件系统，仅 `/tmp` 和持久卷可写。模型密钥只通过 `.env.share` 在运行时注入，不是 Docker build argument，也不会出现在镜像层中。

默认只有一个 Uvicorn worker。这是有意设计：当前 SQLite 连接和运行任务包含进程内状态，不适合直接增加 worker 或水平扩容。多人并发增大后，应先把任务队列和数据库迁移为可共享服务，再增加实例。

当前项目目录约 263 MiB，其中约 192 MiB 是可重建的 `node_modules`。干净仓库只有约 5 MiB；生产镜像预计约 450–650 MiB。建议规格：

- 小团队演示：2 vCPU、4 GiB RAM、10 GiB 持久盘；
- 并发回测或较多分钟数据：4 vCPU、8 GiB RAM、20 GiB 以上持久盘；
- 构建阶段预留至少 2 GiB 临时磁盘，4 GiB 更稳妥。

## 6. 更新与回滚

拉取新代码后重新构建；命名卷不会随镜像更新而丢失：

```bash
docker compose --env-file .env.share -f docker-compose.share.yml build --pull
docker compose --env-file .env.share -f docker-compose.share.yml up -d
```

上线前先备份 SQLite 和数据目录。正式环境应使用不可变版本标签，而不是只保留 `latest`；回滚时切回上一镜像标签，数据库结构若已迁移则仍需配套的数据库回滚方案。
