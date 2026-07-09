# xui-manager-panel-backend

xui-manager-panel 的独立后端仓库，提供 API、订阅链接、用户、工单、充值卡、签到和面板同步能力。

前端仓库：<https://github.com/HaydenSmith1121/xui-manager-panel-frontend>

## 推荐架构

- 后端服务运行在服务器本机 `127.0.0.1:25889`。
- 前端仓库部署 Nginx 静态站点，并把 `/api/` 和 `/sub/` 反向代理到后端。
- 浏览器只访问前端域名，登录 Cookie 使用同源模式，部署和维护最稳定。

## 一键部署 - 推荐同域模式

部署后端：

~~~bash
export ADMIN_EMAIL=admin@admin.com
export ADMIN_PASSWORD=请改成强密码
export LISTEN_HOST=127.0.0.1
export LISTEN_PORT=25889
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-backend/main/deploy/install.sh)
~~~

再部署前端代理到该后端：

~~~bash
export FRONTEND_SERVER_NAME=_
export FRONTEND_LISTEN_PORT=80
export BACKEND_UPSTREAM=http://127.0.0.1:25889
export ENABLE_BACKEND_PROXY=1
export API_BASE_URL=
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-frontend/main/deploy/install.sh)
~~~

## 一键部署 - 前后端不同域名

如果后端单独暴露为 `https://api.example.com`，前端是 `https://front.example.com`：

~~~bash
export ADMIN_EMAIL=admin@admin.com
export ADMIN_PASSWORD=请改成强密码
export LISTEN_HOST=0.0.0.0
export LISTEN_PORT=25889
export FRONTEND_ORIGIN=https://front.example.com
export SESSION_COOKIE_SAMESITE=None
export SESSION_COOKIE_SECURE=true
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-backend/main/deploy/install.sh)
~~~

也可以允许多个前端来源：

~~~bash
export CORS_ALLOWED_ORIGINS=https://front-a.example.com,https://front-b.example.com
~~~

跨域登录依赖浏览器 Cookie 策略，正式环境建议使用 HTTPS。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_NAME` | `xui-manager-panel-backend` | systemd 服务名 |
| `APP_DIR` | `/opt/xui-manager-panel-backend` | 后端代码目录 |
| `DATA_DIR` | `/opt/xui-manager-panel-data` | SQLite 数据与运行数据目录 |
| `ENV_DIR` | `/etc/xui-manager-panel-backend` | 环境配置目录 |
| `ENV_FILE` | `${ENV_DIR}/xui-manager.env` | systemd 读取的环境文件 |
| `REPO_URL` | 本仓库地址 | 后端 Git 仓库地址 |
| `BRANCH` | `main` | 部署分支 |
| `LISTEN_HOST` | `0.0.0.0` | 后端监听地址；同域代理推荐 `127.0.0.1` |
| `LISTEN_PORT` | `25889` | 后端监听端口 |
| `ADMIN_EMAIL` | `admin@example.com` | 初始管理员邮箱 |
| `ADMIN_PASSWORD` | 自动生成 | 初始管理员密码；生产环境请显式设置 |
| `FRONTEND_ORIGIN` | 空 | 单个允许跨域前端来源 |
| `CORS_ALLOWED_ORIGINS` | 空 | 多个允许来源，逗号分隔 |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Cookie SameSite 策略；跨域用 `None` |
| `SESSION_COOKIE_SECURE` | `false` | 是否仅通过 HTTPS 发送 Cookie |

注意：后端使用 `LISTEN_PORT`，不是 `PORT`。

## 升级

~~~bash
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-backend/main/deploy/upgrade.sh)
~~~

升级脚本会先备份 `${DATA_DIR}/app.db`，再拉取最新代码、执行 Python 语法检查并重启服务。

## 卸载

停止并移除 systemd 服务，但保留代码、数据和配置：

~~~bash
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-backend/main/deploy/uninstall.sh)
~~~

同时删除后端代码目录：

~~~bash
export PURGE_APP=1
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-backend/main/deploy/uninstall.sh)
~~~

同时删除数据和配置：

~~~bash
export PURGE_DATA=1
bash <(curl -fsSL https://raw.githubusercontent.com/HaydenSmith1121/xui-manager-panel-backend/main/deploy/uninstall.sh)
~~~

删除数据不可恢复，请先手动备份 `/opt/xui-manager-panel-data/app.db`。

## 从旧一体仓库迁移

旧仓库默认可能部署在 `/opt/xui-manager-panel` 和 `/etc/xui-manager-panel`。新后端默认使用：

- 代码目录：`/opt/xui-manager-panel-backend`
- 数据目录：`/opt/xui-manager-panel-data`
- 配置目录：`/etc/xui-manager-panel-backend`

迁移建议：

1. 停止旧服务：`systemctl stop xui-manager-panel`。
2. 备份旧数据库和配置。
3. 如果旧数据库已经在 `/opt/xui-manager-panel-data/app.db`，新服务可直接复用。
4. 部署新后端并确认登录、工单、充值卡、签到和订阅功能。
5. 部署新前端并配置反向代理。

## 常用运维命令

~~~bash
systemctl status xui-manager-panel-backend --no-pager
journalctl -u xui-manager-panel-backend -f
curl http://127.0.0.1:25889/api/health || true
~~~

## 重置管理员密码

部署脚本只在首次创建环境文件时写入 `ADMIN_EMAIL` 和 `ADMIN_PASSWORD`。如果需要重置管理员密码，请先停止服务，备份数据库，再使用项目内管理工具或直接按当前数据库结构谨慎更新。

## 本地开发

~~~bash
export XUI_MANAGER_DATA=.local-data
export LISTEN_HOST=127.0.0.1
export LISTEN_PORT=25889
python -m xui_manager.app
~~~

## 测试

~~~bash
python -m unittest discover -s tests
~~~
