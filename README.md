# xui-manager-panel-backend

独立后端仓库，提供 xui-manager-panel 的 API、订阅链接、用户、工单、充值卡、签到和面板同步能力。

## 运行

    python -m xui_manager.app

常用环境变量：

    XUI_MANAGER_DATA=/opt/xui-manager-panel/data
    LISTEN_HOST=0.0.0.0
    PORT=25888
    ADMIN_EMAIL=admin@admin.com
    ADMIN_PASSWORD=admin123

## 前后端分离配置

同域反向代理部署时，前端请求 /api/... 即可，不需要额外跨域配置。

如果前端和后端是不同域名，需要配置：

    FRONTEND_ORIGIN=https://你的前端域名
    SESSION_COOKIE_SAMESITE=None
    SESSION_COOKIE_SECURE=true

也可以允许多个前端来源：

    CORS_ALLOWED_ORIGINS=https://front-a.example.com,https://front-b.example.com

前端仓库里把 window.XUI_MANAGER_API_BASE_URL 设置为后端地址即可。

## 测试

    python -m unittest discover -s tests
