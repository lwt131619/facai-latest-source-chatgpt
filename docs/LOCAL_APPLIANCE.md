# 本地交付入口（开发中）

Windows 双击 `start.bat`；macOS/Linux 执行 `./start.sh`。需要 Python 3.10+，
首次安装依赖需要联网。启动器创建隔离的 `.venv`，启动原有 FACAI API 与内置调度器，
健康检查通过后打开 http://127.0.0.1:8080。Ctrl+C 停止服务，data/ 保留历史。
可用 `--port 8081` 或 `--no-browser`。这不是内置 Python 的离线安装包。

新安装且未指定环境凭据时，启动器生成私密的本机初始化信息，并自动打开 /setup 一次性链接。
在浏览器设置Owner用户名和密码，保存单次恢复密钥，然后登录配置模型。
初始化后local-owner.json只保存密码/恢复密钥哈希；重启仍使用您设置的账号。
旧版随机密码文件继续兼容；显式FACAI_ADMIN_PASSWORD或FACAI_USERS_JSON优先，既有部署不强制改账号。
Unix文件模式600；Windows请保护data目录ACL。首次初始化使用正常自动打开浏览器启动；
--no-browser不打印一次性秘密，适合已配置账号的环境。
启动器不自动读取.env，避免隐式加载其他服务凭据。

设置向导提供真实Python/SQLite/Storage检测和可选Rust/Docker/执行CLI存在性检测，
复用加密模型登记、显式连接测试；没有模型/CLI时保持未就绪，不编造执行结果。
详情见SETUP_WIZARD.md。启动器不自动安装OpenCode/Goose，也不绕过权限或预算。

Docker：复制 .env.example 为 .env，设置强 FACAI_ADMIN_PASSWORD，然后
`docker compose up --build -d`。浏览器手动打开上述地址，用户名 admin。
使用单个服务，因为当前源码内置 SQLite 和调度线程；不复制出五个互相争抢作业的服务。
容器不含外部 agent CLI，模型环境配置也需独立配置。停止用 `docker compose down`；
不要加 `-v`（会删除数据库卷）。健康检查只证明 API 可响应。

手机跨设备访问需要单独部署 HTTPS 反向代理，参照 DEPLOYMENT.md；默认仅本机访问。
Electron 源码本地模式：进入 `packaging/app`，先 `npm install`，运行
`npm run start:local`。窗口会启动当前源码树的 Python/FACAI 服务，确认本机
127.0.0.1:8080 就绪后打开 Owner 首次向导；已有 Owner 则打开控制台。
关闭应用会停止由本次窗口启动的服务。8080 被占用、Python 丢失或源码缺失时会
显示错误，不会连接其他进程。需要指定源码树或 Python 时分别设置
`FACAI_SOURCE_DIR` / `FACAI_DESKTOP_PYTHON`。显式指定的 Python 须已安装 requirements.txt；
未指定时使用源码树 `.venv` 并在首次启动安装依赖。用户数据仍由 FACAI_DATA_DIR 管理。
Electron 安装包配置已加入 FACAI 服务源码、静态资源、必要 vendor 元数据和启动器。
打包后默认将随包源码复制到用户数据目录的版本化 runtime，再从该目录启动；Owner
数据库与配置保存在独立的 userData/data 中，升级源码不覆盖用户数据。第一次启动仍需
系统 Python 3.10+ 和联网安装 requirements.txt，安装包不内嵌 Python/依赖。
可显式设置 FACAI_DESKTOP_MODE=remote 保留原有远程站点行为。当前已在 Linux 上用
模拟安装资源启动真实 API 并测试隔离数据目录；Electron 原生安装包构建、Windows/macOS
窗口与升级生命周期仍未实测，不能认作正式离线发行版或 Production v1.0 验收。

开发包：`python scripts/package.py` 生成 dist/FACAI-v1.0-development.zip，
包含源文件 SHA-256 清单和压缩包校验文件。使用显式文件清单，不打包 data/、
.env、Git 历史、虚拟环境和依赖缓存。包状态为 DEVELOPMENT_NOT_PRODUCTION。
GitHub Actions 回归与开发包上传流程已编写，远端执行尚未验证。

当前开发包已融合前一版运行源码，并纳入AGENTS/完整工程方向、测试与必要vendor元数据。
接手先读AGENTS.md，完整Git历史使用另附FACAI-resume.bundle；别把开发ZIP当成Git仓库。
