from datetime import date
from pathlib import Path

from life_dashboard.server import (
    DashboardConfig,
    build_case_pool,
    build_dashboard,
    encode_http_body,
    pwa_manifest,
)
from life_dashboard.view import (
    load_life_wheel,
    load_month,
    load_task_ledger,
    render_page,
)


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


def test_legacy_bullet_narratives_publish_ring_and_fact_compaction(tmp_path: Path) -> None:
    month = tmp_path / "2026-11"
    month.mkdir()
    (month / "2026-11-14.md").write_text(
        """# 2026-11-14

## Anchor lines
- 09:00 · first anchor
- 10:00 · second anchor

## § Narratives
- **First archived narrative.** Body.
- **Second archived narrative.**: Body.

### § Modern narrative
- **Bold body text is not another narrative.**

## 身体（日内事实）
- 完整饭：breakfast
- 完整饭：lunch
- PT：休息
- Cardio：20 minutes
- Publish：public release

## Billing
- 14:10 · 0.3 h · Review · Discussed edits.
""",
        encoding="utf-8",
    )
    record = load_month(month, "2026-11")[0]
    assert record.narrative_titles == [
        "First archived narrative.",
        "Second archived narrative.",
        "Modern narrative",
    ]
    assert record.body_html.count('id="narrative-') == 3
    assert record.complete_meals == "2"
    assert record.billing_hours == 0.3
    assert record.publish == "public release"
    assert {fact["label"] for fact in record.life_facts} == {"饭 2", "有氧"}
    page = render_page([record], "2026-11")
    assert ".day-number.published" in page
    assert "const published = r.publish ? ' published' : '';" in page


def test_month_navigation_and_gzip() -> None:
    records = load_month(SAMPLE / "daily-log/2026-11", "2026-11")
    page = render_page(
        records,
        "2026-11",
        available_months=["2026-10", "2026-11", "2026-12"],
    )
    assert 'href="?month=2026-10"' in page
    assert 'href="?month=2026-12"' in page
    body = page.encode()
    compressed, encoding = encode_http_body(body, "text/html; charset=utf-8", "br, gzip")
    assert encoding == "gzip"
    assert len(compressed) < len(body)
