from datetime import date
from pathlib import Path

from life_dashboard.server import DashboardConfig, build_case_pool, build_dashboard, pwa_manifest
from life_dashboard.view import load_life_wheel, load_month, load_task_ledger


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample-vault"


def test_sample_vault_parses_and_renders() -> None:
    records = load_month(SAMPLE / "daily-log/2026-11", "2026-11")
    tasks = load_task_ledger(SAMPLE / "任务台.md")
    wheel = load_life_wheel(SAMPLE / "人生轮.md")
    assert len(records) == 2
    assert [task.lane for task in tasks] == ["now", "next", "waiting"]
    assert wheel is not None and len(wheel.domains) == 7

    body, month = build_dashboard(
        DashboardConfig(vault_root=SAMPLE, month="2026-11"),
        today=date(2026, 12, 1),
    )
    html = body.decode()
    assert month == "2026-11"
    assert "LIFE DASHBOARD · DAILY ARCHIVE" in html
    assert "产品说明页" in html
    assert "访谈开始出现共同主题" in html
    assert "DADDY" not in html


def test_case_pool_and_pwa_are_self_contained() -> None:
    page = build_case_pool(DashboardConfig(vault_root=SAMPLE)).decode()
    assert "DEMO-001" in page
    assert "DEMO-002" in page
    manifest = __import__("json").loads(pwa_manifest())
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "./"
