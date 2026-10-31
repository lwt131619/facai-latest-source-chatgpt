"""每日技能保鲜循环测试（facai.ops.skill_refresh）。

2026-10-13 操作员指令：技能收录必须每天演习、不许堆着腐烂。全部离线
（MockTransport），钉死四件事：重收录幂等且篡改必拦、演习能点名孤儿技能、
爬取差分失败不拖垮整份报告、退出码可挂告警。
"""

import httpx

from facai.ops.skill_refresh import CURATED_SOURCES, run_daily
from pathlib import Path
import shutil

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR = REPO_ROOT / "vendor" / "skills"

CLEAN_PAGE = ("<html><head><title>产品页</title></head><body>"
              "<p>某无代码平台支持从描述生成应用并一键部署。</p></body></html>")


def test_timer_report_path_is_inside_facai_writable_data():
    unit = (REPO_ROOT / 'deploy/facai-skill-refresh.service').read_text()
    assert '--out /opt/facai-cloud/data/skill-refresh/latest.json' in unit
    assert '--out /opt/facai-cloud/docs/' not in unit


def _transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=CLEAN_PAGE,
                          headers={"content-type": "text/html"})


def _run(tmp_path, client):
    data_dir = tmp_path / "d"
    return run_daily(data_dir, tmp_path / "report.json",
                     sources=["https://x.example/p"], http_client=client)


class TestDailyLoop:
    def test_full_loop_reingests_crawls_and_exercises(self, tmp_path):
        client = httpx.Client(transport=httpx.MockTransport(_transport))
        # 把 vendor/skills 摆到 data 目录旁边，模拟仓库布局
        (tmp_path / "vendor").mkdir()
        shutil.copytree(VENDOR, tmp_path / "vendor" / "skills")
        # run_daily 的 vendor 解析：data_dir.parent/vendor/skills
        data_dir = tmp_path / "d"
        report = run_daily(data_dir, tmp_path / "r.json",
                           sources=["https://x.example/p"],
                           http_client=client)
        assert report["_exit"] == 0
        assert len(report["reingest"]["admitted"]) >= 7
        assert report["reingest"]["blocked"] == []
        assert report["exercise"]["exercised"] >= 7
        assert report["exercise"]["orphans"] == []
        assert report["invariant"]["holds"] is True
        assert report["crawl"]["registered"] == 1
        assert (tmp_path / "r.json").exists()

    def test_second_run_is_idempotent(self, tmp_path):
        client = httpx.Client(transport=httpx.MockTransport(_transport))
        (tmp_path / "vendor").mkdir()
        shutil.copytree(VENDOR, tmp_path / "vendor" / "skills")
        first = run_daily(tmp_path / "d", None, sources=["https://x.example/p"],
                          http_client=client)
        second = run_daily(tmp_path / "d", None, sources=["https://x.example/p"],
                           http_client=client)
        assert second["reingest"]["admitted"] == []
        assert second["reingest"]["unchanged"] == first["reingest"]["admitted"]
        assert second["crawl"]["registered"] == 0          # 内容未变全部跳过
        assert second["_exit"] == 0

    def test_refresh_preserves_owner_disable_and_tombstone(self, tmp_path):
        from facai.store import Store

        (tmp_path / "vendor").mkdir()
        shutil.copytree(VENDOR, tmp_path / "vendor" / "skills")
        data_dir = tmp_path / "d"
        first = run_daily(data_dir, None, sources=[])
        skill_a, skill_b = first["reingest"]["admitted"][:2]
        store = Store(data_dir)
        live = store.registry_list("skills", owner_id="admin")
        store.registry_register("skills", skill_a, {**live[skill_a], "enabled": False}, owner_id="admin")
        store.registry_delete("skills", skill_b, owner_id="admin", deleted_by="admin")
        report = run_daily(data_dir, None, sources=[])
        current = store.registry_list("skills", owner_id="admin")
        assert current[skill_a]["enabled"] is False
        assert skill_b not in current
        assert store.registry_tombstone("skills", skill_b, owner_id="admin") is not None
        assert skill_b in report["reingest"]["owner_removed"]
        assert skill_b not in report["reingest"]["admitted"]
        assert skill_a in report["exercise"]["disabled"]
        assert all(row["skill"] != skill_a for row in report["exercise"]["rows"])

    def test_changed_vendor_source_is_reported_without_overwriting(self, tmp_path):
        from facai.store import Store

        (tmp_path / "vendor").mkdir()
        shutil.copytree(VENDOR, tmp_path / "vendor" / "skills")
        data_dir = tmp_path / "d"
        first = run_daily(data_dir, None, sources=[])
        skill_id = first["reingest"]["admitted"][0]
        store = Store(data_dir)
        original = store.registry_list("skills", owner_id="admin")[skill_id]
        md = tmp_path / "vendor" / "skills" / skill_id / "SKILL.md"
        md.write_text(md.read_text() + "\nAdditional source revision.\n", encoding="utf-8")
        report = run_daily(data_dir, None, sources=[])
        assert report["_exit"] == 1
        assert report["reingest"]["changed"][0]["skill"] == skill_id
        assert store.registry_list("skills", owner_id="admin")[skill_id] == original

    def test_orphan_skill_gets_flagged(self, tmp_path, monkeypatch):
        import facai.ops.skill_refresh as sr
        client = httpx.Client(transport=httpx.MockTransport(_transport))
        (tmp_path / "vendor").mkdir()
        shutil.copytree(VENDOR, tmp_path / "vendor" / "skills")
        # 繁体生僻字组合：保证与任何技能描述（简体+英文术语）零 bigram 重叠
        monkeypatch.setattr(sr, "EXERCISE_PROBES", {
            "無關探針": "乸嘅燚亐丂燚乸"})
        report = sr.run_daily(tmp_path / "d", None,
                              sources=["https://x.example/p"],
                              http_client=client)
        assert len(report["exercise"]["orphans"]) >= 7     # 全员点名：探针与技能全错配
        assert report["_exit"] == 1                        # 孤儿即告警退出

    def test_network_outage_does_not_kill_the_patrol(self, tmp_path):
        def boom(request):
            raise httpx.ConnectError("offline")
        client = httpx.Client(transport=httpx.MockTransport(boom))
        (tmp_path / "vendor").mkdir()
        shutil.copytree(VENDOR, tmp_path / "vendor" / "skills")
        report = run_daily(tmp_path / "d", None, sources=list(CURATED_SOURCES),
                           http_client=client)
        # 逐源兜底：8 个源全部失败记为 skipped 行，巡检与演习照常完成
        assert report["crawl"]["skipped"] == len(CURATED_SOURCES)
        assert report["crawl"]["registered"] == 0
        assert report["exercise"]["exercised"] >= 7
        assert report["_exit"] == 0
