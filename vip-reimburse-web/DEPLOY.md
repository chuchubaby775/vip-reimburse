# 部署到云服务器

这个项目包含 Python 后台、SQLite 数据库、私有票据和本地 OCR，不能用 GitHub Pages 等纯静态托管。GitHub 只保存源码，推荐私有仓库。以下配置适用于带 Docker Compose 的 Linux 服务器，建议至少 2 核 CPU、4 GB 内存，并预留 OCR 镜像、模型和附件空间；实际资源需求以上线试运行为准。

## 需要准备

- 大会认可的服务器和可用的 SSH 访问方式。
- 域名及 DNS 管理权限。域名解析至该服务器，开放 80 和 443 端口；不要公开 8765 端口。
- 中国内地服务器使用域名提供网站服务时，需按云服务商要求办理备案。已有公司服务器及已备案域名通常更方便。
- 如用 GitHub 同步代码，提供目标仓库地址；私有代码访问通过已登录的 GitHub 或 SSH 授权完成，不要在聊天中粘贴密码、Token 或私钥。

## 部署步骤

只上传源码，不复制本机 `data/`、`work/`、原始票据、导出材料、`.env`、`.runtime/` 和虚拟环境。`.dockerignore` 使用白名单，构建上下文不会包含本机数据。首次上线创建独立空数据库；现有资料若需迁移，另行明确目标与范围。

在服务器的项目目录中：

```sh
cp .env.example .env
chmod 600 .env
```

编辑服务器上的 `.env`，设置 `APP_DOMAIN` 为真实域名（不带协议或路径），设置至少 12 位独立 `ADMIN_PASSWORD`。邀请码默认 `01AI2026VIP`，可按会务安排更改。

```sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

构建需要联网安装官方 Python 依赖和下载 PaddleOCR 模型，首次可能较慢。模型下载或推理检查失败会停止构建，修复后重试；不会把未启用图片 OCR 的版本当成完整上线。镜像固定安装项目声明的 Paddle 版本。Linux 不使用本机 Mac 的 Apple Vision。

Caddy 自动申请 HTTPS 证书，域名必须已解析且 80/443 可达。浏览器访问 `https://你的域名/` 和 `/admin`。`COOKIE_SECURE=1` 已启用；应用仅通过容器内网连接代理。`TRUST_PROXY_HEADERS=1` 仅用于此隔离代理配置，Caddy 会覆盖真实客户端 IP 头；如果直接运行应用或公开应用端口，应关闭该选项。

## 持久化与更新

`guest_data` 卷存放数据库、票据及哈希配置；`ocr_models` 保留模型缓存；TLS 卷保存证书。重新构建或 `docker compose down` 不删除这些卷。**不要使用 `docker compose down -v`，它会删除数据卷。** 新服务器初次上线不带任何本机嘉宾资料。

更新前备份私有数据，保留旧版源码或镜像以便回退，再重建并启动。备份需包含整个 `guest_data` 卷，建议停应用后制作一致备份，并由大会负责人保管；确认备份可恢复后再更新。不能只备份 SQLite 而丢掉票据文件，也不要把备份提交 GitHub。

## 发布验收

用虚构资料验证微信内访问、邀请码、上传图片 OCR 和 PDF、草稿恢复、提交、手机号查询、后台审核、回收站及 Excel/ZIP。健康检查 `/healthz` 只检查进程与数据库连接，不替代 OCR 或完整流程验收。正式上线后再生成指向 HTTPS 正式地址的二维码。

当前工作区已准备部署文件；尚未在目标 Linux 服务器构建镜像或公开发布。需要目标服务器、域名和访问授权后才能完成真实部署。
