"""Content assertions for the mobile surface (no browser needed, runs everywhere).

mobile.html was upgraded from an honest static storyboard to a real client:
login/register, one-sentence ordering via ``POST /api/v1/missions/auto``,
real progress polling of ``GET /api/v1/missions/{id}`` (with the SSE stream
as an accelerator) and real channel binding through ``/api/v1/my/channels/*``.
Following the string-level style of ``tests/test_console_layout.py``, these
tests pin that wiring inside the page: real request path literals, the CSRF
header wiring, four channel forms driving the real endpoints, the honest
「先登录」 gates — and, because the page is a public static surface, no
credential-shaped strings and no out-of-allowlist external hosts.
"""

import re
from pathlib import Path

import pytest

MOBILE = Path(__file__).resolve().parents[1] / "facai" / "static" / "mobile.html"
SW = Path(__file__).resolve().parents[1] / "facai" / "static" / "sw.js"
MANIFEST = Path(__file__).resolve().parents[1] / "facai" / "static" / "mobile.webmanifest"

# 外链白名单：与 tests/test_customer_surface.py 的静态面扫描同口径。
ALLOWED_HOSTS = {"whstapp.lol", "api.deepseek.com", "github.com", "www.w3.org",
                 "discord.com", "api.slack.com", "developers.line.biz"}


@pytest.fixture(scope="module")
def html():
    return MOBILE.read_text(encoding="utf-8")


def test_mobile_wires_the_real_request_path_literals(html):
    """真实请求路径必须以字面量出现在页面里（可审计、可 grep）。"""
    for literal in (
        "/api/v1/auth/session",
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/missions/auto",
        "/api/v1/my/channels/telegram",
        "/api/v1/my/channels/line",
        "/api/v1/my/channels/discord",
        "/api/v1/my/channels/slack",
    ):
        assert literal in html, f"mobile 页缺少真实请求路径字面量：{literal}"
    # 详情 / 事件 / SSE 按 mission id 动态组装，但方法与路径段必须可见
    for fragment in ("'GET', '/api/v1/missions/'", "'/events'",
                     "'/stream'", "EventSource"):
        assert fragment in html, f"mobile 页缺少请求组装片段：{fragment}"


def test_mobile_login_and_logout_are_wired(html):
    """登录视图 + 注册双模式 + 退出登录；弱密码规则与 /guide 第二节同口径。"""
    assert 'id="login-form"' in html
    assert 'id="login-username"' in html
    assert 'id="login-password"' in html
    assert 'id="me-logout-btn"' in html
    assert "注册并登录" in html
    # 弱密码规则提示（auth.is_weak_password 的客户口径）
    assert "至少 8 位" in html
    assert "常见弱口令" in html


def test_mobile_attaches_the_csrf_token_to_writes(html):
    """登录响应里的 csrf_token 存内存，之后所有写请求带 X-CSRF-Token。"""
    assert "csrf_token" in html, "登录/注册响应的 csrf_token 必须落内存"
    assert "X-CSRF-Token" in html, "写请求必须带 X-CSRF-Token 头"
    assert "credentials: 'same-origin'" in html, "同源 cookie 会话自动携带"
    # 只有非 GET 请求带 CSRF 头（与 facai/api.py SAFE_METHODS 语义一致）
    assert "method !== 'GET'" in html


def test_mobile_channel_forms_drive_the_real_endpoints(html):
    """四个渠道表单全部走真实 /my/channels/*，旧的假 toast 占位必须清掉。"""
    for channel in ("telegram", "line", "discord", "slack"):
        assert f'data-channel-form="{channel}"' in html, channel
        assert f'data-channel-bound="{channel}"' in html, channel
        assert f'data-unbind-channel="{channel}"' in html, channel
        assert f'data-channel-error="{channel}"' in html, channel
        # PUT + DELETE 都打真实端点（CHANNEL_PATHS 字面量）
        assert f"'/api/v1/my/channels/{channel}'" in html, channel
    # 旧占位文案（原型演示：绑定待接 PUT ...）必须消失
    assert "绑定待接" not in html
    assert "静态演示" not in html
    # 凭据框保持密码框：token 类输入不以明文出现在屏幕上
    assert html.count('type="password"') >= 4


def test_mobile_unauthenticated_is_honest(html):
    """未登录口径：写操作给「先登录」提示，不展示演示数据、不打假成功。"""
    assert "请先登录，登录后即可真下单" in html
    assert "请先登录，再绑定你自己的渠道" in html
    assert "未登录：不展示演示数据" in html
    # 保留 Desktop 分界文案
    assert "Desktop 总控台" in html
    assert "复杂控制在电脑总控台完成" in html or "复杂控制与配置在电脑总控台完成" in html


def test_mobile_monitor_is_poll_driven_not_faked(html):
    """监控视图按后端真实 progress 渲染：4 秒轮询 + 手动刷新，无假进度。"""
    assert "setInterval" in html, "任务监控应有定时轮询"
    assert "4000" in html, "轮询周期 4 秒"
    assert "strokeDashoffset" in html, "进度环偏移由后端数值驱动"
    assert "不造假进度" in html


def test_mobile_surface_leaks_no_credential_shapes(html):
    """公开静态面：无凭据形状、无后端内部细节字样（test_customer_surface 同口径）。"""
    assert "pbkdf2" not in html.lower()
    assert "FACAI_ADMIN_PASSWORD" not in html
    assert "os.getenv" not in html
    assert "secretbox" not in html
    assert not re.search(r"sk-[A-Za-z0-9_-]{10,}", html), "出现 sk- 形状密钥"
    assert not re.search(r"facai_[0-9a-f]{8,}_[A-Za-z0-9_-]{12,}", html), \
        "出现 facai_ 形状密钥"
    assert not re.search(r"xoxb-[A-Za-z0-9-]{10,}", html), "出现 Slack token 形状"


def test_mobile_external_hosts_stay_on_allowlist(html):
    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", html))
    assert hosts <= ALLOWED_HOSTS, f"mobile 页出现白名单外域名：{hosts - ALLOWED_HOSTS}"


def test_mobile_files_tab_wires_the_real_artifacts_center(html):
    """文件 Tab 必须真的调产物中心端点（不再是占位 stub）。"""
    assert "apiRequest('GET', '/api/v1/artifacts')" in html, "文件 Tab 要接 GET /api/v1/artifacts"
    assert "产物中心待接" not in html, "旧的待接占位文案必须清掉"


def test_mobile_pwa_install_chain_is_wired(html):
    """手机页自身的安装链：独立 manifest + https/secure-context 门控的 SW 注册。"""
    assert '<link rel="manifest" href="/static/mobile.webmanifest">' in html, \
        "手机页挂独立 mobile.webmanifest，不再复用指向桌面台的根 manifest"
    assert "'serviceWorker' in navigator" in html and "navigator.serviceWorker.register" in html, \
        "手机页必须自己注册 /sw.js（不能只靠 / 与 /login 顺带覆盖）"
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert '"start_url": "/static/mobile.html"' in manifest or "'start_url': '/static/mobile.html'" in manifest, \
        "独立 manifest 的 start_url 必须指向手机页自身"
    sw = SW.read_text(encoding='utf-8')
    assert '"/static/mobile.html"' in sw, "SHELL 必须含手机页，否则已装 SW 的设备离线打不开手机页"
    assert '"facai-shell-v2"' in sw, "SHELL 变更必须伴随 CACHE 版本升级（activate 才会清 v1）"
    assert '"/api/"' in sw and "respondWith" in sw, "/api/ 一律直穿不缓存，仅外壳回退缓存"
