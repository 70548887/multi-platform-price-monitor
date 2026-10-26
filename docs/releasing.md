# 正式版本发布规范

## 版本与更新记录

- 使用 SemVer 正式版本 `X.Y.Z`；新增兼容功能增加次版本，兼容修复增加修订版本，不兼容接口或数据变更增加主版本。
- 同步修改 `backend/app/version.py` 的 `VERSION`、`frontend/package.json` 的 `version`、`frontend/package-lock.json` 顶层及根包版本。
- 在 `CHANGELOG.md` 最前方增加 `[X.Y.Z] - YYYY-MM-DD`，填写概要、新增、修复、升级说明和回滚说明；没有该类变更时明确写“无”。
- 只创建准确的 `vX.Y.Z` 标签，禁止预发布后缀、前导零和重用旧标签。已发布资产不得覆盖；修复后发布新版本。

## 发布前验证

先提交要发布的代码并确认工作树干净，再执行：

```bash
python scripts/validate_release.py --tag v1.2.0
python -m pytest backend/tests scripts/tests -q
npm ci --prefix frontend
npm run build --prefix frontend
python scripts/package_release.py --version v1.2.0
python scripts/validate_release.py --tag v1.2.0 --assets dist/releases --notes-output dist/releases/release-notes.md
```

发布包必须包含前端构建结果、应用代码、`release-manifest.json`；清单的产品标识、schema、版本、提交 SHA 和每个文件的 SHA256 必须一致。运行数据、账号会话、真实 `.env`、代理凭据、数据库、备份和日志不得打包。

## GitHub 流程

```bash
git tag -a v1.2.0 -m "Release v1.2.0"
git push origin v1.2.0
```

GitHub Actions 对标签和源码版本执行门禁，再运行隔离测试、前端构建和 Linux 上独立 Docker 安装的实际升级及回滚演练。发布任务下载已测试产物后，独立复核包字节、清单与提交 SHA，生成中文发布说明，并创建草稿 Release。资产上传后再次下载并核对，确认完整才切为正式 Release；失败时保留草稿，后台不会把草稿当作可升级版本。

正式 Release 标题为 `PriceScope vX.Y.Z`，资产恰为 `price-monitor-X.Y.Z.tar.gz` 和 `price-monitor-X.Y.Z.tar.gz.sha256`。发布说明包含更新记录、升级与回滚步骤、源码提交以及 SHA256；说明文件是流程产物，不作为可执行升级包。

## 部署验收边界

自动化测试通过仅证明当前覆盖场景。正式推广前，在独立安装中验证一次完整的下载、应用、重启、健康检查及失败回滚，核对 `.env`、平台会话、数据库和价格历史保持连续。Windows 本机与 Linux Docker 分别留存实际验收结果。

## Linux 和宝塔的 updater 运行环境

宿主机 updater 的 Python 与 API 容器隔离，容器内已安装依赖不代表宿主机可直接导入。用与 `APP_UID` / `APP_GID` 相同的部署用户准备独立虚拟环境：

```bash
python3 -m venv data/update/host-env
data/update/host-env/bin/python -m pip install -r scripts/requirements-updater.txt
bash scripts/update_host.sh --mode compose
```

该用户需能够写入应用代码、`data/update` 与 `data/backups`，并有执行 Docker Compose 的权限。按服务器实际用户的 UID/GID 填写 `.env`，不要直接套用示例值。Release 新安装先运行 `python3 scripts/init_env.py`；已有 `.env` 时可运行 `python3 scripts/init_env.py --prepare-config-only`，只创建缺失的 `config/update.yaml`，保留原配置。

systemd 模板位于 `deploy/price-monitor-updater.service.example`。宝塔 Supervisor 同样配置：工作目录为项目根，运行用户为部署用户，命令为 `/bin/bash /实际项目路径/scripts/update_host.sh --mode compose`，启用自动重启；不要把 updater 放进会被自身更新停止的 API 容器。
