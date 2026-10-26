# 多平台商品价格监控

为内部团队维护平台、采集账号与规则，按小时采集全站商品，查看精确价格、走势、销售状态与站内告警。平台按亿乐、易客分类型，分别提供 `xiaohong66.com` 与 `fans8.top` PC 版采集适配器，不同平台与账号的数据分别保存。

后端为 Python / FastAPI / Celery，数据库为 PostgreSQL，Redis 用于任务队列、锁与共享限速。前端为 Vue 3 / TypeScript / ECharts。API、采集 Worker 与调度器各自运行。

## Docker 部署

需要 Docker Engine、Docker Compose v2 和首次生成密钥用的 Python 3。部署环境使用 Linux 容器。

```powershell
python scripts/init_env.py
docker compose up -d --build --wait --wait-timeout 240
docker compose ps
```

也可执行 `./scripts/start.ps1`；Linux 执行 `bash scripts/start.sh`。密钥初始化只运行一次，已有 `.env` 会拒绝覆盖。初始管理员账号和随机密码写入 `.env`；默认用户名 `admin`。

默认访问 `http://127.0.0.1:28671`。若需局域网访问，在 `.env` 配置 `WEB_BIND` 和 `WEB_PORT`。HTTPS 反向代理部署时将 `COOKIE_SECURE=true`。数据库和 Redis 未向宿主机开放端口。PostgreSQL 与 Redis 数据位于 Compose 持久卷。

```powershell
docker compose logs --tail 100 api worker beat
docker compose stop
docker compose up -d
```

`stop` 保留数据。删除持久卷会丢失数据库与队列数据，应先完成备份。调度器只运行一个实例；Worker 可扩容，平台锁和共享限速仍以平台为单位生效。

## 接入平台

1. 登录后台，新增平台，选择平台类型及对应采集版本，设置唯一账号标识。Fans8 归入易客系列；易客标准版本暂存配置，待提供站点资料验证后启用采集。
2. 按页面提示导入该平台账号的会话信息并执行会话检查。会话加密保存；失效后暂停采集，重新连接后恢复。
3. 配置采集周期、分类、分页与字段规则，运行测试预览，确认样本单价、展示数量和单位后发布规则。
4. 手动运行首个采集批次，在任务页面确认完整性，再开启周期采集。

已有会话、已发布规则或商品历史的连接会锁定类型与版本，新版本应新增连接。未使用的连接可以更换版本，并生成对应版本的新草稿。类型与版本、规则校验和 Worker 分派保持绑定，待验证版本会在连接、测试、发布、手动采集和调度入口被拦截。

管理员可在本机执行 `python scripts/connect_platform.py --platform-id 对应平台编号`，在新开的 Chrome/Edge 窗口手动登录主账号。工具根据版本进入登录页并加密保存会话，无需把密码发送到聊天中。Fans8 使用自身的 Bearer 会话与 GET 商品接口；`Bs` 表示下单倍数，不用于乘算价格。源站未明确计价单位时会标记待核对。

Fans8 的目录会在采集中新增或删除商品。已实测 `PageCount=5000` 可一次返回约 4900 个商品，默认规则因此使用单页 5000 条、60 秒超时，减少跨页变动；超过单页容量时仍严格校验每页总数和唯一编号。发生变化或缺页时保留不完整批次，不把未采商品判为下架。游客登录已实测可读商品，游客身份 `Id=0`；使用游客时应新增独立连接和账号标识，保留与主账号的价格上下文。

商品编号关联历史，更名不会产生新商品。价格使用精确小数，API 以字符串返回；基础单价和展示数量分别保存。停售、缺失、异常单位有独立状态，停售零价不会按正常降价处理。未采到的商品不会因不完整批次被批量判为下架。

跨平台统一商品由管理员确认映射，仅比较币种、单位、规格和价格口径一致的商品。首次采集建立基线，之后记录价格与销售状态变化。旧批次不会覆盖较新的当前价格。

## 本机 Windows 开发

项目提供 `scripts/native.ps1` 管理独立的本项目进程，不安装系统服务。默认使用已安装的 PostgreSQL16 二进制、现有 Redis 可执行文件，独立数据目录为 `.runtime`，端口为 PostgreSQL `55432`、Redis `56379`、API `28671`、前端 `28672`。可通过脚本参数指定二进制路径。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend/requirements-dev.txt
Push-Location frontend
npm.cmd ci
Pop-Location
./scripts/native.ps1 -Action services
./scripts/native.ps1 -Action start
./scripts/native.ps1 -Action status
```

本机模式会生成包含独立数据库连接的 `.env`，不会覆盖已有文件；若已有 Docker 配置，需要自行填写本机 `DATABASE_URL` 与 `REDIS_URL`。数据库初始化在独立目录进行，保留现有系统服务。

访问 `http://127.0.0.1:28671` 使用已构建网站，或访问 `http://127.0.0.1:28672` 使用前端开发预览，开发代理连接 API。两种部署方式使用相同默认网站端口，原生模式与 Docker 模式请择一启动。日志在 `.runtime/logs`。代码更新后执行 `./scripts/native.ps1 -Action restart-app` 重启 API、Worker、调度器与前端，保留数据库、Redis 和其他独立辅助进程。停止时执行 `./scripts/native.ps1 -Action stop`，只停止记录的且路径与创建时间匹配的本项目进程，保留所有数据。Windows Celery 使用 `solo` 模式做开发验证；部署使用 Compose 的 Linux Worker。

## 验收与持续观察

先完成一个真实平台的成功采集，再开始持续观察。以下命令从 `.env` 读取后台登录账号，通过登录 Cookie 查询健康与运维统计，不修改商品或触发采集：

```powershell
python scripts/acceptance_watch.py --once
python scripts/acceptance_watch.py --duration-hours 48 --interval-seconds 300
```

本机开发使用 `--url http://127.0.0.1:28671`。可通过 `--env-file` 或 `MONITOR_ADMIN_PASSWORD` 环境变量传入登录密码，避免将密码写在命令行。

观察数据写入 `var/acceptance/*.jsonl`，包含真实 UTC 时间、健康状态、各批次计数、平台最近成功时间及最终结果。每次运行使用独立文件，不能把多次中断观察拼成连续 48 小时。`--once` 只生成一次样本，永远不会认定 48 小时通过。Ctrl+C 会记录 `interrupted`，请求或采集异常会进入失败结果。

`backend/requirements.txt` 固定已验证的应用主依赖，Docker 从此文件安装；测试依赖单独列在 `backend/requirements-dev.txt`。这些文件没有冻结工作站上的其他工具包。

通过标准包含：实际覆盖 48 小时、健康采样无失败、每个启用平台按周期产生成功批次、无新增失败或不完整批次、无过期会话或陈旧成功时间。观察脚本只验证服务侧证据；采集价格与登录页面样本的一致性仍需按真实平台核验。

记录结束后还需执行独立逐批复核，不能只凭观察器的汇总标志宣布通过：

```powershell
python scripts/verify_acceptance.py var/acceptance/对应记录文件.jsonl
```

复核器另写 `.review.json`，保留原始记录；它检查依赖健康、API 运行中断、每批商品数量及耗时、各平台实际采集时间缺口和完整 48 小时覆盖。返回 `0` 才表示通过，`2` 表示仍在观察，`1` 表示未通过或记录不完整。本机已运行的记录无需因增加复核器而重启。

```powershell
python -m unittest discover -s scripts/tests -v
.venv\Scripts\python -m pytest backend/tests
Push-Location frontend
npm.cmd run build
Pop-Location
```

脚本单元测试中的时间数据仅验证验收判定逻辑，不代表完成真实 48 小时运行。

## 采集代理与系统设置

管理员在“系统设置”配置隧道代理和境外代理，在“平台管理”逐个平台选择直连、隧道或境外线路。既有连接默认直连；隧道代理预留为停用状态。代理地址支持 HTTP/HTTPS 与账号密码认证，加密保存在数据库，页面与接口只返回“已配置/未配置”和启停状态。

代理地址留空表示保留已保存的地址，“清除”会删除凭据并停用该代理。使用已停用或尚未配置代理的平台会明确报错，不自动回退直连。路由同时用于会话检查、规则预览和周期采集；每个批次启动时读取一次配置。代理连接故障与源站 HTTP 502 等错误会在任务记录中分别说明。

代理只改变服务器采集出口，无法保证源站不会超时或返回错误。独立登录窗口使用操作者本机网络；选用代理的平台，其候选会话交由服务器按所选线路校验，无需向登录助手下发代理凭据。本机无法打开源站时，可在能访问的浏览器完成登录后通过后台导入会话。代理密码、Token 和真实地址不写入仓库、版本包或审计消息。

## 备份与密钥

## 在线升级

系统更新页面只允许管理员操作。更新来源是当前 GitHub 仓库的正式 Release，发布资产必须包含 `price-monitor-{version}.tar.gz` 和同名 `.sha256` 校验文件。服务只负责检测、下载、校验和创建更新任务，真正替换代码与重启由宿主机更新器完成。

非敏感配置位于 `config/update.yaml`，GitHub Token 和代理只通过环境变量提供：

```powershell
$env:UPDATE_GITHUB_TOKEN = '从密码管理器读取的 Token'
$env:UPDATE_PROXY = '可选的本机代理地址'
```

Windows 本机模式运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/update_host.ps1 -Mode native
```

Linux/Docker 部署运行：

```bash
bash scripts/update_host.sh --mode compose
```

宝塔面板使用 Docker 管理器时，按 Linux/Docker 模式运行 updater。API 容器用户和 updater 用户必须一致，并且该用户需要执行 Docker Compose 的权限：

```bash
export APP_UID=$(id -u www)
export APP_GID=$(id -g www)
mkdir -p data/update data/backups
chown -R "$APP_UID:$APP_GID" data/update data/backups
usermod -aG docker www
```

如果使用宝塔 Supervisor，可将 updater 配置为常驻进程：

```ini
[program:price-monitor-updater]
command=/bin/bash /www/wwwroot/price-monitor/scripts/update_host.sh
directory=/www/wwwroot/price-monitor
user=www
autostart=true
autorestart=true
stopasgroup=true
killasgroup=true
```

当前宿主机 updater 的 `native` 模式用于 Windows 本机进程；Linux 宝塔的自动应用链路使用 Docker Compose。宝塔直接托管 Uvicorn、Celery 和 Nginx 的非 Docker 组合需要把停止和启动命令接入宿主机服务管理器后再启用自动应用。

Docker 部署需让 API 容器用户与宿主机更新器用户使用相同的 `APP_UID`、`APP_GID`，并提前创建可写目录：

```bash
mkdir -p data/update data/backups
chown -R "$APP_UID:$APP_GID" data/update data/backups
```

更新器只替换 Release 清单中的受管文件，保留 `.env`、数据库、Redis、平台会话、历史价格、运行目录和备份。更新失败会恢复最近备份；Docker 更新不挂载 Docker Socket。

Docker 部署可执行：

```powershell
python scripts/backup.py
```

当前本机模式使用 `python scripts/backup.py --mode native`。该模式从 `.env` 读取项目数据库连接，只向本机 PostgreSQL 执行 `pg_dump`；完成后用 `pg_restore --list` 检查备份目录，不恢复或修改原数据库。2026-10-25 已使用真实数据生成并验证一次备份。

备份以 PostgreSQL 自定义格式写入 `backups`，成功完成后才发布最终文件并计算 SHA256。恢复前应另行验证备份可读性，并停止写入任务。该脚本不自动清除数据库或恢复覆盖数据。

请单独妥善备份 `.env`。数据库恢复后仍需要原 `ENCRYPTION_KEY` 解密平台会话；更换初始管理员环境密码不会直接修改已有数据库用户密码。日志、备份、会话、`.env` 和 `.runtime` 均排除在版本控制及容器构建上下文之外。

## 当前验证边界

2026-10-25 已在本机 `28671` 运行第一个平台的真实采集：最初两个完整批次分别核对到 `4101/4101` 个商品，共 `8202` 条价格观测。样本商品 `9585` 在该次核对时，源站页面、接口和数据库价格均为 `0.007447`；随后又核对到真实涨价至 `0.008`（页面显示 `0.008000`），历史与事件连续保存。这些是核对时的报价，不作为固定价格。桌面与移动端详情页、价格趋势和停售筛选已使用真实数据检查。

2026-10-26 已连接 Fans8 主账号，成功批次 `8` 完整采集 `4881/4881` 个商品，耗时约 `38.524` 秒。此前三个动态目录不完整批次保留。商品库保留 `4884` 个历史商品记录，与当批数量不同；不会删除未出现在当前目录中的旧记录。另以独立游客页面核对了 `4344`、`7350`、`7331` 三个样本，价格与主账号入库结果一致，未验证其余商品是否同价。两个平台均已启用小时采集，新的共同连续观察从北京时间 `2026-10-26 00:07:18` 开始，旧窗口不拼接。

同日已在隔离的 `price_monitor_test`、Redis DB15 和专用队列中验证真实 Celery Worker 强制中断与恢复。租约自然过期后，原批次编号保留、执行次数为 2，重复派发返回跳过且观测总数保持 4 条；旧批次的测试报价 10 未覆盖较新测试报价 20。此演练使用固定测试商品，未请求源站，生产进程创建时间未变化。

登录连接工具会在本机将候选会话加密保存为 `.runtime/pending-session-platform-1.enc`，验证失败也保留。排除接口或网络问题后，可执行 `python scripts/connect_platform.py --platform-id 1 --resume-pending` 恢复连接，无需重新启动登录浏览器；上传成功后删除候选文件。凭据和密文不进入 Git。

实际验证以当前命令输出、运行日志和验收 JSONL 为准。本机缺少 Docker CLI，因此容器镜像构建与 Compose 启动尚未执行。真实 48 小时验收必须记录完整时长，并由独立复核器返回 `0`、`status=verified` 后才算通过。
