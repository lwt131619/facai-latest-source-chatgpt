# CI Linux 可移植性审计报告（ubuntu-latest 预测）

审计日期：2026-10-16
审计对象：`facai-codex-latest` @ bd3dcd3（79954b5 sed 修复之后）
工作流：`.github/workflows/production-build.yml`，ubuntu-latest + Python 3.12，`python -m pytest -q --junitxml=dist/pytest.xml`，随后 `python scripts/package.py`

## 基线判断（影响排名逻辑的关键事实）

486fed8 那次 CI 失败**只有** `test_deploy_config.py::test_validate_config_script_passes` 一个用例（sed 语法 bug），且 pytest 单个失败不会中止整个套件 —— 即**其余全部用例（含浏览器测试、并发测试）已在 ubuntu-latest 上跑过且通过**。当前 HEAD 相对 486fed8 的增量只有：79954b5（validate_config.sh 一行 sed 修复）和 bd3dcd3（纯文档）。

结论：新 CI 上**确定性**的平台差异 bug 概率很低；剩余风险集中在**非确定性/环境敏感**用例（网络、时序、字体度量、端口竞态）。以下排名按"下一次运行翻车概率"排序。

## Ubuntu 上最可能失败的 Top 5（排除已修复的 nginx -t / sed）

### 1. test_mission_control_flow —— 全套件唯一未被桩住的真实 DNS 调用

- **位置**：`tests/test_mission_control_flow.py:24-27`（PUT `/api/v1/registries/models/deepseek-flash`，endpoint=`https://api.deepseek.com`）；触发链 `facai/api.py:2798` → `facai/secretbox.py:120`（`socket.getaddrinfo(parts.hostname, None)`）
- **失败机理**：conftest 的 DNS 桩 `hermetic_model_registration_dns`（`tests/conftest.py:124-145`）只对 `test_customer_surface`、`test_tenant_isolation_registries`、`test_user_models` 三个模块生效（conftest.py:132-134 白名单），**`test_mission_control_flow` 不在名单里**。它在 ubuntu CI 上会发起真实的 `api.deepseek.com` DNS 解析：任何一次解析失败/超时 → `SecretBoxError` → 接口返回 422 → `assert ...status_code == 200` 直接失败。另外 `getaddrinfo` 无超时参数，解析卡住会拖慢甚至挂起该用例。这是全套件里唯一无条件依赖外网的单元测试。
- **修法一句话**：把 `'test_mission_control_flow'` 加进 `tests/conftest.py:132` 的模块白名单集合即可（一行改动，零行为影响）。

### 2. test_runtime_capacity —— 租约/续约时序窗口在慢速 runner 上会破

- **位置**：`tests/test_runtime_capacity.py:136-137`（`sleep=1.5, lease_seconds=0.4`）、`:156-167`（20 秒轮询窗口 + `probes["fleet-worker-1"].total == 0` 断言）、`:184`（`time.sleep(0.5)` 对 lease 0.3）、`:251`（`sleep=0.3`）
- **失败机理**：`test_lease_renewal_keeps_a_slow_job_unstolen` 要求慢任务（1.5s）期间 0.4s 的租约被及时续约；GitHub 标准 runner（2-4 核，远慢于本机）上线程调度抖动一旦超过 0.4s 窗口，peer 的 recovery 就会把任务偷走重跑 → `worker-1 total == 0` 断言失败。同理 `test_crashed_worker_job_is_recovered_by_a_peer` 依赖 0.5s > 0.3s 的固定 sleep。486fed8 上它们恰好过了，但这是典型的 CI 时序 flake。
- **修法一句话**：把租约余量放大 3-5 倍（如 `lease_seconds=2.0` 配 `sleep=1.5`），或用事件/条件变量替代固定 sleep。

### 3. 浏览器布局断言依赖 CJK 字体度量；三处 launch 漏了 --no-sandbox

- **位置**：
  - 溢出断言（`scrollWidth <= innerWidth + 1` / `scrollWidth - clientWidth == 0`）：`tests/test_console_layout.py:87,221,226-227`；`tests/test_mobile_ui.py:74,123,153`；`tests/test_ui_theme.py:110,132,163`；`tests/test_setup_browser.py:83`；`tests/test_trust_browser.py:74,109`
  - 无 `--no-sandbox` 的 launch：`tests/test_ui_theme.py:19-21`、`tests/test_billing.py:233`、`tests/test_daily_progress.py:221-223`（其余四个浏览器文件都传了 `args=['--no-sandbox']`）
- **失败机理**：ubuntu 上中文渲染靠 `playwright install --with-deps chromium` 附带的 unifont/wqy 等字体，字宽与 Windows 字体不同；runner 镜像或依赖清单一变（比如 CJK 字体缺失、回退到宽度不同的字形），紧凑布局就会溢出 1-2px，十多处像素级溢出断言连锁失败。漏 `--no-sandbox` 的三处在非 root 的 GH 托管 runner 上暂能跑，但任何 root/受限沙箱环境（自建 runner、容器）会直接 launch 失败（虽有 try/except 会转 skip，但等于静默失去覆盖）。
- **修法一句话**：三处 launch 统一补 `args=['--no-sandbox']`，并在 CI 预装 `fonts-noto-cjk`（或把溢出容差从 1px 放宽）。

### 4. conftest._free_port() 端口 TOCTOU 竞态

- **位置**：`tests/conftest.py:64-69`（bind(:0) → close → uvicorn 再绑定同一端口）；同型模式 `tests/test_setup_browser.py:25-26`、`tests/test_channel_conversion_claim.py:295`
- **失败机理**：Linux 内核对临时端口复用更激进（`ip_local_port_range`），close 与 uvicorn bind 之间端口被其他进程抢走 → uvicorn "address already in use" → `conftest.py:87` 抛 "uvicorn did not start"，一次性报废所有依赖 `console_server`/`protected_server` 的浏览器用例（几十个）。概率不高但一炸一片。
- **修法一句话**：让 uvicorn 直接绑 port=0 后从 `server.servers[0].sockets[0].getsockname()` 读实际端口（或绑定失败时换端口重试一次）。

### 5. test_eventbus 并发写 SQLite 在慢速 runner 上可能锁超时

- **位置**：`tests/test_eventbus.py:68-96`（8 线程 × 24 次 `publish`，Barrier 同步）；连接配置 `facai/store.py:458-465`（`sqlite3.connect(timeout=10)` + WAL + `synchronous=NORMAL`）
- **失败机理**：8 个写线程在 2 核 runner 上排队，WAL 写锁串行；一旦累积等待超过 10s busy timeout（CI 磁盘慢 + CPU 饥饿时可能），抛 `sqlite3.OperationalError: database is locked` → flake。同类的 `test_runtime_capacity` 多 Scheduler 共享一个 Store 也走这条路，但压力小得多。
- **修法一句话**：把该测试（或 Store.connect）的 busy timeout 加大到 30s，或并发线程从 8 降到 4。

## 已核查、判定安全的项（不再列为风险）

1. **validate_config.sh 在 ubuntu 上会整段 SKIP nginx -t**：ubuntu-latest 默认无 nginx，`deploy/validate_config.sh:186` 的 `command -v nginx` 不通过 → 直接 skip，静态检查 + `bash -n` 全过；sed 已是 `-E -e ... -e ...`（GNU sed 语义正确）。`.gitattributes` 强制 `*.sh eol=lf`，Linux checkout 无 CRLF 问题。
2. **`--junitxml=dist/pytest.xml`**：`dist/` 在 .gitignore 里、CI checkout 后不存在，但实测 pytest 9.x 会自动创建缺失的父目录（本地实验验证），不会报错。
3. **`os.name == 'nt'` 分支全部正确带守卫**：`tests/test_local_launcher.py:23,33`、`tests/test_deploy_config.py:43`（find_bash 走 `/bin/bash`）、`tests/test_wechat.py:39`、`facai/onboarding.py:55`、`scripts/start.py:28,71`。POSIX 分支依赖的 0600 权限都由 `os.open(..., 0o600)` 显式创建（`facai/onboarding.py:25`、`facai/channels/wechat.py:183`），任何 umask 下都是 0600。
4. **无编码地雷**：全库搜不到 gbk/cp936/cp1252；测试与源码的文件读写都显式 `encoding='utf-8'`（少数无 encoding 的 write 只写 ASCII JSON）。Linux 默认 UTF-8 locale 无影响。
5. **无 Windows 专属 API**：搜不到 `os.startfile`/`winreg`/`msvcrt`/`windll`/`shell=True`/`cmd /c`；子进程只调 `git` 和 `sys.executable`（`tests/test_broker.py:255,278`），CI 两者都有。`.exe`/`scoop`/`powershell` 引用均为注释或产品文案（`facai/ops/foundry_seed.py:57-61` 的 `D:/hermes-agent` 只是展示元数据）。
6. **`facai/ops/github_digest.py:268-278` 字体候选 Linux 路径在前**，Windows 盘符在后；`test_digest_video_renders_cards_when_ffmpeg_available`（`tests/test_artifacts_center.py:204-224`）有 `which("ffmpeg")`/`_find_font()` 双守卫，ubuntu 无 ffmpeg → 干净 skip。
7. **`test_deploy_config.py::test_deploy_script_is_executable_in_git`**：486fed8 上已通过（唯一失败是 sed 那个），说明 git index 里 `deploy/deploy.sh` 的 100755 模式位正确，无需再查。
8. **deploy-now 系列五个用例**：`ROOT.parent/deploy-now.py` 在 CI checkout 的上级目录不存在 → `tests/test_deploy_config.py:372` 干净 skip（paramiko 也不在 requirements 里，`importorskip` 双保险）。
9. **闭网上游**：conftest 的 autouse `hermetic_upstream` 删光 `*_API_KEY` 并把 `facai.api.complete` 桩成本地失败，模型连通性测试（`test_model_connectivity.py`）全部走 fake complete，无真实出网。
10. **`test_checkpoint_sync.py:17`** 用 `git init -b facai/production-build` 显式指定分支，不依赖 `init.defaultBranch` 默认值（本机是 master，CI 无配置，均不受影响）。
11. **requirements.txt**：fastapi/uvicorn/httpx/pytest/qrcode/cryptography 全部纯 pip 安装、Linux 原生支持，无平台行为分歧；qrcode 不带 pil 的约束与平台无关（`tests/test_wechat.py:44-78` 已专门钉死）。
12. **scripts/package.py**：全部必需文件（docs 三份、packaging/app、.github/workflows 等）在仓库中齐备；SECRET 正则扫描 facai/tests/scripts/deploy/docs/vendor/packaging/bridge 全部无命中；CI 浅克隆下 `git rev-parse HEAD`/`status --porcelain` 正常；`.sh` 在 zip 里统一 0755。
13. **时区**：digest 用 UTC（`_yesterday`），未发现 localtime/tzset 依赖；账单/调度相关时间断言在 486fed8 的 ubuntu 运行中已验证过。

## 一句话结论

鉴于 486fed8 除 sed 外已全绿、增量仅一行修复加文档，新 CI 大概率通过；真正值得提前备好修法的是五处非确定性风险——首推 `test_mission_control_flow` 漏进 DNS 桩白名单（一行即修），其次是 runtime_capacity 的 0.4s 租约时序、浏览器字体像素断言与三处漏 `--no-sandbox` 的 launch、`_free_port()` 端口竞态、eventbus 并发写 SQLite 的锁超时。
