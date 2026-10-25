# 多平台商品价格监控

为内部团队维护平台、采集账号与规则，按小时采集全站商品，查看精确价格、走势、销售状态与站内告警。首个适配器为 `xiaohong66.com`，不同平台与账号的数据分别保存。

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

1. 登录后台，新增平台，选择相应适配器，设置唯一账号标识。
2. 按页面提示导入该平台账号的会话信息并执行会话检查。会话加密保存；失效后暂停采集，重新连接后恢复。
3. 配置采集周期、分类、分页与字段规则，运行测试预览，确认样本单价、展示数量和单位后发布规则。
4. 手动运行首个采集批次，在任务页面确认完整性，再开启周期采集。

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

```powershell
python -m unittest discover -s scripts/tests -v
.venv\Scripts\python -m pytest backend/tests
Push-Location frontend
npm.cmd run build
Pop-Location
```

脚本单元测试中的时间数据仅验证验收判定逻辑，不代表完成真实 48 小时运行。

## 备份与密钥

Docker 部署可执行：

```powershell
python scripts/backup.py
```

当前本机模式使用 `python scripts/backup.py --mode native`。该模式从 `.env` 读取项目数据库连接，只向本机 PostgreSQL 执行 `pg_dump`；完成后用 `pg_restore --list` 检查备份目录，不恢复或修改原数据库。2026-10-25 已使用真实数据生成并验证一次备份。

备份以 PostgreSQL 自定义格式写入 `backups`，成功完成后才发布最终文件并计算 SHA256。恢复前应另行验证备份可读性，并停止写入任务。该脚本不自动清除数据库或恢复覆盖数据。

请单独妥善备份 `.env`。数据库恢复后仍需要原 `ENCRYPTION_KEY` 解密平台会话；更换初始管理员环境密码不会直接修改已有数据库用户密码。日志、备份、会话、`.env` 和 `.runtime` 均排除在版本控制及容器构建上下文之外。

## 当前验证边界

2026-10-25 已在本机 `28671` 运行第一个平台的真实采集：两个完整批次分别核对到 `4101/4101` 个商品，共 `8202` 条价格观测。样本商品 `9585` 当前源站页面、接口和数据库价格均为 `0.007447`；该价格会随平台变化，不作为固定报价。桌面与移动端详情页、价格趋势和停售筛选已使用真实数据检查。

登录连接工具会在本机将候选会话加密保存为 `.runtime/pending-session-platform-1.enc`，验证失败也保留。排除接口或网络问题后，可执行 `python scripts/connect_platform.py --platform-id 1 --resume-pending` 恢复连接，无需重新启动登录浏览器；上传成功后删除候选文件。凭据和密文不进入 Git。

实际验证以当前命令输出、运行日志和验收 JSONL 为准。本机缺少 Docker CLI，因此容器镜像构建与 Compose 启动尚未执行。真实 48 小时验收只有观察脚本记录完整时长且 `full_48h_verified=true` 才算通过。
