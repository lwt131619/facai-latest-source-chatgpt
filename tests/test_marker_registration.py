"""MODEL-001 ⑤：``live`` marker 注册纪律。

钉死两件事：``pytest --markers`` 能看到 live（验收者可发现这条通道）；
默认跑法（``-m "not live"``，即 CI 与常规本地的口径）不收集任何 live 用例——
真实上游连接只在显式 ``-m live`` 时执行，常规套件永不触网。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pytest(*args: str) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    return out.stdout + out.stderr


def test_live_marker_is_registered_and_visible_via_pytest_markers():
    listing = _pytest("--markers")
    assert "@pytest.mark.live" in listing
    assert "-m live" in listing or "-m 'live'" in listing


def test_live_tests_are_deselected_by_default():
    """默认口径（-m "not live"）下 live 文件收集结果必须为空——真实连接
    用例不会被任何常规跑法意外带入（conftest 断网桩也不因此被绕过）。"""
    out = _pytest("tests/test_model_live.py", "--collect-only", "-q", "-m", "not live")
    assert "no tests collected" in out and "1 deselected" in out
    assert "test_manual_probe_reaches_real_upstream" not in out


def test_live_tests_are_collected_under_expensive_marker():
    out = _pytest("tests/test_model_live.py", "--collect-only", "-q", "-m", "live")
    # 无 key 的环境里收集仍可见（skip 发生在运行期，不是收集期）。
    assert "test_manual_probe_reaches_real_upstream" in out
