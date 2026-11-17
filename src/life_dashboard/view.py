#!/usr/bin/env python3
"""Generate a local, read-only life dashboard from canonical Markdown.

The Markdown files remain authoritative.  This script only creates a disposable
view that can be regenerated at any time.  The dashboard combines three views
without merging their meanings: live priorities, a low-frequency life-wheel
snapshot, and the daily-log month archive.
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - production requirements include it
    MarkdownIt = None


DEFAULT_VAULT = Path.home() / "Documents" / "life-dashboard-vault"

SECTION_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
LIST_RE = re.compile(r"(?m)^-\s+(.+?)\s*$")
TIME_PREFIX_RE = re.compile(r"^\d{1,2}:\d{2}\s*[·・]\s*")
LOCATION_RE = re.compile(r"(?:📍|到公司|到家|Wi-Fi first connection)", re.I)
HOURS_RE = re.compile(r"(?<![\d.])([0-9]+(?:\.[0-9]+)?)\s*h\b", re.I)
SLEEP_RE = re.compile(r"(?m)^-\s+(.+?(?:\d+h\d+m|\d+小时\d+分).*?)\s*$")
SLEEP_DURATION_RE = re.compile(r"(?:\d+h\d+m|\d+小时\d+分)")
WAKE_SCORE_RE = re.compile(
    r"醒来(?:感觉|身体)?\s*[：:]?\s*(?:仅\s*)?(?:约\s*)?"
    r"([0-5](?:\.\d+)?(?:\s*[–—~-]\s*[0-5](?:\.\d+)?)?)\s*/\s*5"
)
CHECKED_RE = re.compile(r"(?m)^-\s+\[[xX]\]\s+(.+?)\s*$")
STACK_COUNT_RE = re.compile(r"补剂\s*(\d+)\s*项")
UNKNOWN_VALUES = {"", "—", "-", "unknown", "未知"}
TASK_HEADING_RE = re.compile(r"(?m)^##\s+(TASK-[A-Za-z0-9-]+)\s*[·・]\s*(.+?)\s*$")
CASE_HEADING_RE = re.compile(r"(?m)^##\s+(CASE-[A-Za-z0-9-]+)\s*[·・]\s*(.+?)\s*$")
FIELD_RE = re.compile(r"(?m)^-\s+([A-Za-z_]+)\s*:\s*(.*?)\s*$")
LIFE_SNAPSHOT_RE = re.compile(r"(?m)^###\s+第.+?张新人生轮\s*[·・]\s*(\d{4}-\d{2}-\d{2})\s*$")
MONTH_VIEW_META_RE = re.compile(
    r"<!--\s*month-view:\s*domain=(body|work|life|growth|neutral)"
    r"(?:;\s*axes=((?:in|out)(?:,(?:in|out))*))?"
    r"(?:;\s*primary=(true|false))?\s*-->",
    re.I,
)


@dataclass
class DayRecord:
    iso_date: str
    day: int
    weekday: str
    anchors: list[str]
    narrative_titles: list[str]
    narrative_meta: list[dict[str, object]]
    sleep: str
    supplement_items: list[str]
    supplement_count: int
    billing_hours: float
    wake_score: str
    complete_meals: str
    pt: str
    cardio: str
    weekly_hrv: str
    publish: str
    guardrail: str
    body_html: str
    source_name: str

    @property
    def preview(self) -> list[str]:
        substantive = [a for a in self.anchors if not LOCATION_RE.search(a)]
        return (substantive or self.anchors)[:2]

    @property
    def card_titles(self) -> list[str]:
        """All source-ordered labels for the month card's scrollable title region."""
        if self.narrative_titles:
            return self.narrative_titles
        if self.preview:
            return self.preview[:1]
        return ["仅有基础记录"]

    @property
    def sleep_duration(self) -> str:
        match = SLEEP_DURATION_RE.search(self.sleep)
        return match.group(0) if match else ""

    @property
    def life_facts(self) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        if self.complete_meals:
            meal_count = re.sub(r"\s*顿$", "", self.complete_meals.strip())
            facts.append({"kind": "fuel", "label": f"饭 {meal_count}"})
        if self.pt:
            label = _activity_label("PT", self.pt)
            if label:
                facts.append({"kind": "movement", "label": label, "title": self.pt})
        if self.cardio:
            label = _activity_label("有氧", self.cardio)
            if label:
                facts.append({"kind": "movement", "label": label, "title": self.cardio})
        if self.weekly_hrv:
            facts.append({"kind": "recovery", "label": f"HRV {self.weekly_hrv}"})
        return facts


@dataclass
class TaskRecord:
    task_id: str
    title: str
    lane: str
    status: str
    owner: str
    artifact: str
    next_action: str
    deadline: str
    blocker: str
    created: str
    updated: str
    completed: str


@dataclass
class CaseRecord:
    case_id: str
    label: str
    source_table: str
    state: str
    client_ref: str
    our_ref: str
    matter_status: str
    workflow_stage: str
    task: str
    official_due: str
    soft_due: str
    personnel: str
    action_item: str
    milestone: str
    description: str
    updated: str


@dataclass
class LifeDomain:
    name: str
    score: float
    delta: float
    status: str
    detail_html: str


@dataclass
class LifeWheel:
    snapshot_date: str
    domains: list[LifeDomain]
    common_judgment: str
    growth: str
    self_realization: str
    source_html: str


def split_h2_sections(text: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[start:end].strip()
    return sections


def _known_value(value: str) -> str:
    value = value.strip()
    return "" if value.lower() in UNKNOWN_VALUES else value


def _scalar_value(body: str, label: str) -> str:
    match = re.search(rf"(?m)^-\s+{re.escape(label)}\s*[：:]\s*(.+?)\s*$", body)
    return _known_value(match.group(1)) if match else ""


def _complete_meal_count(body: str) -> str:
    """Count explicit complete-meal facts without interpreting meal composition."""
    values = re.findall(r"(?m)^-\s+完整饭\s*[：:]\s*(.+?)\s*$", body)
    total = 0
    for value in values:
        if not _known_value(value):
            continue
        count_match = re.fullmatch(r"\s*(\d+)\s*顿?\s*", value)
        total += int(count_match.group(1)) if count_match else 1
    return str(total) if total else ""


def _first_section_with_prefix(sections: dict[str, str], prefix: str) -> str:
    return next((body for heading, body in sections.items() if heading.startswith(prefix)), "")


def _activity_label(label: str, value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"休息", "rest", "休"} or "休息" in normalized:
        return ""
    return label


def strip_legacy_eod_status(text: str) -> str:
    """Hide the old status snapshot without rewriting canonical history."""
    match = re.search(r"(?ms)^## EOD snapshot\s*\n(.*?)(?=^## |\Z)", text)
    if not match:
        return text
    body = re.sub(
        r"(?ms)^### 状态\s*\n.*?(?=^### |\Z)", "", match.group(1), count=1
    ).strip()
    replacement = f"## EOD snapshot\n\n{body}\n\n" if body else ""
    return text[: match.start()] + replacement + text[match.end() :]


def _add_narrative_ids(rendered: str) -> str:
    marker = "<h2>§ Narratives</h2>"
    if marker not in rendered:
        return rendered
    before, tail = rendered.split(marker, 1)
    next_h2 = re.search(r"<h2>", tail)
    end = next_h2.start() if next_h2 else len(tail)
    narrative_html = tail[:end]
    counter = 0

    def label_heading(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return f'<h3 id="narrative-{counter}">{match.group(1)}</h3>'

    # Older EOD logs stored each narrative as a bold list item before any H3
    # narratives.  Give those legacy entries the same stable detail anchors,
    # but do not mistake bold bullets inside a later H3 narrative for another
    # narrative.
    first_h3 = narrative_html.find("<h3>")
    prefix_end = first_h3 if first_h3 >= 0 else len(narrative_html)
    prefix, heading_tail = narrative_html[:prefix_end], narrative_html[prefix_end:]

    def label_legacy_bullet(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return match.group(0).replace(
            "<strong>", f'<strong id="narrative-{counter}">', 1
        )

    prefix = re.sub(
        r"(?s)<li>\s*(?:<p>)?<strong>.*?</strong>", label_legacy_bullet, prefix
    )
    heading_tail = re.sub(r"<h3>(.*?)</h3>", label_heading, heading_tail)
    narrative_html = prefix + heading_tail
    return before + marker + narrative_html + tail[end:]


def render_markdown(text: str) -> str:
    text = strip_legacy_eod_status(text)
    text = MONTH_VIEW_META_RE.sub("", text)
    if MarkdownIt is None:
        return f"<pre>{html.escape(text)}</pre>"
    rendered = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").render(text)
    return _add_narrative_ids(rendered)


def _parse_float(value: str) -> float:
    cleaned = value.strip().replace("+", "")
    return float(cleaned) if cleaned else 0.0


def load_task_ledger(path: Path) -> list[TaskRecord]:
    """Load the live shared task surface from one canonical Markdown ledger."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    matches = list(TASK_HEADING_RE.finditer(text))
    tasks: list[TaskRecord] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        fields = {key.lower(): value.strip() for key, value in FIELD_RE.findall(body)}
        lane = fields.get("lane", "backlog").lower()
        status = fields.get("status", "open").lower()
        tasks.append(
            TaskRecord(
                task_id=match.group(1),
                title=match.group(2).strip(),
                lane=lane,
                status=status,
                owner=fields.get("owner", ""),
                artifact=fields.get("artifact", ""),
                next_action=fields.get("next", ""),
                deadline=_known_value(fields.get("deadline", "")),
                blocker=_known_value(fields.get("blocker", "")),
                created=fields.get("created", ""),
                updated=fields.get("updated", ""),
                completed=_known_value(fields.get("completed", "")),
            )
        )
    open_tasks = [task for task in tasks if task.status == "open" and not task.completed]
    invalid = [task.task_id for task in open_tasks if task.lane not in {"now", "next", "waiting", "backlog"}]
    if invalid:
        raise ValueError(f"unknown task lane: {', '.join(invalid)}")
    if sum(task.lane == "now" for task in open_tasks) > 1:
        raise ValueError("task ledger contract violated: NOW contains more than one open task")
    if sum(task.lane == "next" for task in open_tasks) > 2:
        raise ValueError("task ledger contract violated: NEXT contains more than two open tasks")
    return tasks


def load_case_ledger(path: Path) -> list[CaseRecord]:
    """Load operational case metadata; dated baseline rows stay visibly unconfirmed."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    matches = list(CASE_HEADING_RE.finditer(text))
    cases: list[CaseRecord] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        fields = {
            key.lower(): _known_value(value.strip())
            for key, value in FIELD_RE.findall(text[match.end() : end])
        }
        cases.append(
            CaseRecord(
                case_id=match.group(1),
                label=match.group(2).strip(),
                source_table=fields.get("source_table", ""),
                state=fields.get("state", "baseline_unconfirmed"),
                client_ref=fields.get("client_ref", ""),
                our_ref=fields.get("our_ref", ""),
                matter_status=fields.get("matter_status", ""),
                workflow_stage=fields.get("workflow_stage", ""),
                task=fields.get("task", ""),
                official_due=fields.get("official_due", ""),
                soft_due=fields.get("soft_due", ""),
                personnel=fields.get("personnel", ""),
                action_item=fields.get("action_item", ""),
                milestone=fields.get("milestone", ""),
                description=fields.get("description", ""),
                updated=fields.get("updated", ""),
            )
        )
    return cases


def _life_detail_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^###\s+[A-G]\.\s+(.+?)\s*$", text))
    for index, match in enumerate(matches):
        next_h3 = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        next_h2 = re.search(r"(?m)^##\s+", text[match.end() : next_h3])
        end = match.end() + next_h2.start() if next_h2 else next_h3
        source = f"### {match.group(1).strip()}\n" + text[match.end() : end].strip()
        sections[match.group(1).strip()] = render_markdown(source)
    return sections


def load_life_wheel(path: Path) -> LifeWheel | None:
    """Parse the latest scored snapshot and the seven canonical domain cards."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    matches = list(LIFE_SNAPSHOT_RE.finditer(text))
    if not matches:
        return None
    latest = max(matches, key=lambda match: match.group(1))
    next_h2 = re.search(r"(?m)^##\s+", text[latest.end() :])
    end = latest.end() + next_h2.start() if next_h2 else len(text)
    snapshot = text[latest.end() : end]
    details = _life_detail_sections(text)
    domains: list[LifeDomain] = []
    for line in snapshot.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in {"领域", "---"} or set(cells[0]) <= {"-", ":"}:
            continue
        try:
            score = _parse_float(cells[1])
            delta = _parse_float(cells[2])
        except ValueError:
            continue
        name = cells[0]
        domains.append(
            LifeDomain(
                name=name,
                score=score,
                delta=delta,
                status=cells[3],
                detail_html=details.get(name, ""),
            )
        )

    judgment_match = re.search(
        r"(?ms)^\*\*共同判断：\*\*\s*(.+?)(?=^\*\*中心两轴|\Z)", snapshot
    )
    growth_match = re.search(r"(?m)^-\s+个人成长：\*\*(.+?)\*\*", snapshot)
    realization_match = re.search(r"(?m)^-\s+自我实现：\*\*(.+?)\*\*", snapshot)
    return LifeWheel(
        snapshot_date=latest.group(1),
        domains=domains,
        common_judgment=judgment_match.group(1).strip() if judgment_match else "",
        growth=growth_match.group(1).strip() if growth_match else "",
        self_realization=realization_match.group(1).strip() if realization_match else "",
        source_html=render_markdown(text),
    )


def parse_day(path: Path, iso_date_override: str | None = None) -> DayRecord:
    text = path.read_text(encoding="utf-8")
    iso_date = iso_date_override or path.stem
    parsed_date = datetime.strptime(iso_date, "%Y-%m-%d").date()
    sections = split_h2_sections(text)

    anchor_body = sections.get("Anchor lines", "") or _first_section_with_prefix(sections, "最新")
    anchors = [TIME_PREFIX_RE.sub("", item).strip() for item in LIST_RE.findall(anchor_body)]

    narrative_body = sections.get("§ Narratives", "")
    narrative_titles: list[str] = []
    narrative_meta: list[dict[str, object]] = []
    narrative_headings = list(re.finditer(r"(?m)^###\s+(.+?)\s*$", narrative_body))
    # v4 history contains two valid archive shapes.  Legacy entries are bold
    # top-level bullets; newer entries are H3 sections.  Only inspect the
    # prefix before the first H3 so bold bullets inside an H3 body remain body
    # text rather than being double-counted as narratives.
    legacy_prefix_end = narrative_headings[0].start() if narrative_headings else len(narrative_body)
    for match in re.finditer(
        r"(?m)^-\s+\*\*(.+?)\*\*(?:[：:]\s*|\s+|$)",
        narrative_body[:legacy_prefix_end],
    ):
        title = match.group(1).strip().lstrip("§ ").strip()
        narrative_titles.append(title)
        narrative_meta.append(
            {"title": title, "domain": "neutral", "axes": [], "primary": False}
        )
    for index, match in enumerate(narrative_headings):
        raw_title = match.group(1).strip()
        if raw_title.startswith("*("):
            continue
        title = raw_title.lstrip("§ ").strip()
        next_heading = narrative_headings[index + 1].start() if index + 1 < len(narrative_headings) else len(narrative_body)
        metadata_match = MONTH_VIEW_META_RE.search(narrative_body, match.end(), next_heading)
        domain = metadata_match.group(1).lower() if metadata_match else "neutral"
        axes = metadata_match.group(2).lower().split(",") if metadata_match and metadata_match.group(2) else []
        primary = bool(metadata_match and metadata_match.group(3) == "true")
        narrative_titles.append(title)
        narrative_meta.append({"title": title, "domain": domain, "axes": axes, "primary": primary})

    sleep_body = _first_section_with_prefix(sections, "Sleep")
    sleep_match = SLEEP_RE.search(sleep_body)
    sleep = sleep_match.group(1).strip() if sleep_match else ""
    wake_match = WAKE_SCORE_RE.search(sleep_body)
    wake_score = re.sub(r"\s+", "", wake_match.group(1)) if wake_match else ""

    billing_body = _first_section_with_prefix(sections, "Billing")
    billing_hours = sum(float(value) for value in HOURS_RE.findall(billing_body))

    weekday = "一二三四五六日"[parsed_date.weekday()]
    supplement_body = _first_section_with_prefix(sections, "补剂")
    checked_items = CHECKED_RE.findall(supplement_body)
    if not checked_items:
        checked_items = LIST_RE.findall(supplement_body)
    supplement_items = [
        item
        for item in checked_items
        if not item.startswith("早餐") and not STACK_COUNT_RE.search(item)
    ]
    stack_count = sum(
        int(match.group(1))
        for item in checked_items
        for match in [STACK_COUNT_RE.search(item)]
        if match
    )

    body_facts = _first_section_with_prefix(sections, "身体")
    # Publish and guardrail are intentionally sparse, explicit facts.  They are
    # supported without template placeholders so absence never becomes a false
    # negative or a daily checkbox obligation.
    fact_text = "\n".join((body_facts, text))

    return DayRecord(
        iso_date=iso_date,
        day=parsed_date.day,
        weekday=f"周{weekday}",
        anchors=anchors,
        narrative_titles=narrative_titles,
        narrative_meta=narrative_meta,
        sleep=sleep,
        supplement_items=supplement_items,
        supplement_count=len(supplement_items) + stack_count,
        billing_hours=round(billing_hours, 2),
        wake_score=wake_score,
        complete_meals=_complete_meal_count(body_facts),
        pt=_scalar_value(body_facts, "PT"),
        cardio=_scalar_value(body_facts, "Cardio"),
        weekly_hrv=_scalar_value(body_facts, "7d HRV"),
        publish=_scalar_value(fact_text, "Publish"),
        guardrail=_scalar_value(fact_text, "Guardrail"),
        body_html=render_markdown(text),
        source_name=path.name,
    )


def load_month(source_dir: Path, month: str) -> list[DayRecord]:
    datetime.strptime(month, "%Y-%m")
    return [parse_day(path) for path in sorted(source_dir.glob(f"{month}-??.md"))]


def safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def _due_html(label: str, value: str, *, soft: bool = False) -> str:
    if not value:
        return ""
    due_class = "soft-due" if soft else "official-due"
    rendered = f"{label} {html.escape(value)}"
    try:
        passed = datetime.strptime(value, "%Y-%m-%d").date() < date.today()
    except ValueError:
        passed = False
    if passed and soft:
        rendered = f"待重排 · 原 {html.escape(value)}"
        due_class += " stale-soft"
    elif passed:
        due_class += " due-risk"
    return f'<span class="{due_class}">{rendered}</span>'


def _visible_cases(cases: list[CaseRecord]) -> list[CaseRecord]:
    return [case for case in cases if case.state.lower() not in {"inactive", "hidden", "archived"}]


def _live_cases(cases: list[CaseRecord]) -> list[CaseRecord]:
    visible = _visible_cases(cases)
    live = [case for case in visible if case.state.lower() not in {"done", "completed", "closed"}]
    return sorted(
        live,
        key=lambda case: (
            0 if case.state.lower() == "active" else 1,
            case.soft_due or "9999-99-99",
            case.official_due or "9999-99-99",
            case.our_ref or case.client_ref or case.label,
        ),
    )


def _case_row_html(case: CaseRecord, *, compact: bool = False) -> str:
    work = case.task or case.action_item or "待确认下一动作"
    due = _due_html("Soft", case.soft_due, soft=True) + _due_html("官方", case.official_due)
    baseline = '<span class="baseline-chip">11/2 基线 · 待确认</span>' if case.state == "baseline_unconfirmed" else ""
    status = f'<span>{html.escape(case.matter_status)}</span>' if case.matter_status else ""
    stage = f'<span class="stage-chip">{html.escape(case.workflow_stage)}</span>' if case.workflow_stage else ""
    active = '<span class="active-chip">ACTIVE</span>' if case.state.lower() == "active" else ""
    description = "" if compact else (
        f'<p class="case-description">{html.escape(case.description)}</p>'
        if case.description else '<p class="case-description muted">源表未写状态说明</p>'
    )
    milestone = "" if compact or not case.milestone else f'<p class="case-milestone"><b>已走到</b> · {html.escape(case.milestone)}</p>'
    return (
        '<details class="case-row"><summary>'
        f'<span><b>{html.escape(case.our_ref or case.client_ref or case.label)}</b>'
        f'<small>{html.escape(work)}</small></span><span class="case-due">{due}</span>'
        f'</summary><div class="case-body"><div class="case-tags">{active}{stage}{baseline}{status}</div>{milestone}{description}</div></details>'
    )


def _case_pool_html(cases: list[CaseRecord]) -> str:
    visible = _visible_cases(cases)
    live = _live_cases(cases)
    if not visible:
        return ""
    completed = len(visible) - len(live)
    completed_text = f" · 已确认完成 {completed}" if completed else ""
    return (
        '<section class="case-pool"><div class="case-pool-head"><span><b>案件池</b>'
        f'<small>当前待确认／推进 {len(live)}{completed_text} · 主页面只看前三件</small></span>'
        '<a class="case-open" href="cases">全部案件 →</a></div><div class="case-list">'
        + "".join(_case_row_html(case, compact=True) for case in live[:3])
        + "</div></section>"
    )


def render_case_pool_page(cases: list[CaseRecord]) -> str:
    """Render the dedicated read-only case pool from the same canonical ledger."""
    visible = _visible_cases(cases)
    live = _live_cases(cases)
    completed = len(visible) - len(live)
    rows = "".join(_case_row_html(case) for case in live)
    empty = '<p class="empty">当前没有待确认／推进的案件。</p>' if not live else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>案件池 · Life Dashboard</title>
<style>
:root{{--ink:#302b27;--muted:#786f67;--paper:#f7f2ea;--card:#fffaf3;--line:#ded4c8;--moss:#5f714f;--risk:#9c5548}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
main{{width:min(960px,calc(100% - 28px));margin:0 auto;padding:24px 0 56px}}header{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}}
h1{{margin:0 0 6px;font-size:25px}}header p{{margin:0;color:var(--muted);font-size:13px}}a{{color:var(--moss);text-decoration:none;font-weight:700}}
.case-list{{display:grid;gap:10px}}.case-row{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:0 14px}}
.case-row summary{{list-style:none;cursor:pointer;display:flex;justify-content:space-between;gap:16px;align-items:center;padding:14px 0}}.case-row summary::-webkit-details-marker{{display:none}}
.case-row b{{font-size:14px}}.case-row small{{display:block;color:var(--muted);font-size:11px;line-height:1.45;margin-top:4px}}
.case-due{{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;min-width:150px}}.case-due span,.case-tags span{{font-size:10px;padding:4px 7px;border-radius:999px;background:#eee7dd;white-space:nowrap}}
.case-due .stale-soft,.case-due .due-risk{{background:#f6ddd7;color:var(--risk)}}.case-tags{{display:flex;gap:6px;flex-wrap:wrap}}.active-chip{{background:#dfe9d8!important;color:#43583a}}.baseline-chip{{background:#eee7dd!important;color:var(--muted)}}
.case-body{{border-top:1px solid var(--line);padding:12px 0 14px}}.case-description,.case-milestone{{font-size:12px;line-height:1.6;margin:10px 0 0;color:#4b443e}}.case-milestone b{{color:var(--moss)}}.muted,.empty{{color:var(--muted)}}
@media(max-width:640px){{header{{display:block}}header>a{{display:inline-block;margin-top:12px}}.case-row summary{{align-items:flex-start;flex-direction:column}}.case-due{{justify-content:flex-start;min-width:0}}}}
</style></head><body><main><header><div><h1>案件池</h1><p>当前 soft due 优先 · {len(live)} 件待确认／推进 · {completed} 件已确认完成</p></div><a href="./">← 返回总仪表盘</a></header><div class="case-list">{rows}</div>{empty}</main></body></html>"""


def _task_strip_html(tasks: list[TaskRecord], cases: list[CaseRecord] | None = None) -> str:
    lanes = (("now", "NOW", "正在交付"), ("next", "NEXT", "随后两件"), ("waiting", "WAITING", "外部条件"))
    cards: list[str] = []
    open_tasks = [task for task in tasks if task.status == "open" and not task.completed]
    for lane, label, caption in lanes:
        lane_tasks = [task for task in open_tasks if task.lane == lane]
        rendered: list[str] = []
        for task in lane_tasks:
            facts = []
            if task.owner:
                facts.append(f"<span>{html.escape(task.owner)}</span>")
            if task.deadline:
                facts.append(f"<span>DDL {html.escape(task.deadline)}</span>")
            if task.blocker:
                facts.append(f"<span>等 {html.escape(task.blocker)}</span>")
            next_action = (
                f'<p class="task-next"><b>下一刀</b> {html.escape(task.next_action)}</p>'
                if task.next_action
                else ""
            )
            artifact = (
                f'<p class="task-artifact">交付物 · {html.escape(task.artifact)}</p>'
                if task.artifact
                else ""
            )
            rendered.append(
                f'<article class="task-card"><div class="task-id">{html.escape(task.task_id)}</div>'
                f'<h3>{html.escape(task.title)}</h3>{next_action}{artifact}'
                f'<div class="task-meta">{"".join(facts)}</div></article>'
            )
        empty = '<div class="lane-empty">此刻没有</div>' if not rendered else ""
        case_pool = _case_pool_html(cases or []) if lane == "next" else ""
        cards.append(
            f'<section class="task-lane {lane}"><div class="lane-head"><b>{label}</b>'
            f'<span>{caption}</span></div>{"".join(rendered)}{empty}{case_pool}</section>'
        )
    return "".join(cards)


def _life_summary_html(life: LifeWheel | None) -> str:
    if life is None or not life.domains:
        return '<div class="lane-empty">尚未找到人生轮快照</div>'
    count = len(life.domains)
    center, radius, label_radius = 150.0, 86.0, 124.0
    axes: list[str] = []
    points: list[str] = []
    labels: list[str] = []
    rings: list[str] = []
    short_names = {
        "工作与职业": "工作",
        "财务与安全": "财务",
        "身体与健康": "身体",
        "爱与家庭": "家庭",
        "朋友与社交": "朋友",
        "休息与玩": "休息",
        "家与环境": "居所",
    }
    for level in range(1, 6):
        ring_points = []
        for index in range(count):
            angle = -1.57079632679 + 6.28318530718 * index / count
            current = radius * level / 5
            ring_points.append(f"{center + current * math.cos(angle):.1f},{center + current * math.sin(angle):.1f}")
        rings.append(f'<polygon points="{" ".join(ring_points)}" class="wheel-ring"/>')
    for index, domain in enumerate(life.domains):
        angle = -1.57079632679 + 6.28318530718 * index / count
        outer_x = center + radius * math.cos(angle)
        outer_y = center + radius * math.sin(angle)
        axes.append(f'<line x1="{center}" y1="{center}" x2="{outer_x:.1f}" y2="{outer_y:.1f}"/>')
        current = radius * domain.score / 5
        point_x = center + current * math.cos(angle)
        point_y = center + current * math.sin(angle)
        points.append(f"{point_x:.1f},{point_y:.1f}")
        label_x = center + label_radius * math.cos(angle)
        label_y = center + label_radius * math.sin(angle)
        if domain.status.startswith("建设"):
            state_class = "build"
        elif domain.status.startswith("维持"):
            state_class = "maintain"
        elif domain.status.startswith("反思"):
            state_class = "reflect"
        else:
            state_class = "simplify"
        short = short_names.get(domain.name, domain.name[:2])
        labels.append(
            f'<g class="wheel-label {state_class}" transform="translate({label_x:.1f} {label_y:.1f})" '
            f'data-life-index="{index}" tabindex="0" role="button" aria-label="打开{html.escape(domain.name)}详情">'
            f'<text class="wheel-domain" text-anchor="middle" y="4">{html.escape(short)} {domain.score:g}</text></g>'
        )
    return (
        '<div class="wheel-graphic">'
        f'<svg viewBox="0 0 300 300" role="img" aria-label="七瓣人生轮">'
        f'<g class="wheel-rings">{"".join(rings)}</g><g class="wheel-axes">{"".join(axes)}</g>'
        f'<polygon points="{" ".join(points)}" class="wheel-shape"/>{"".join(labels)}'
        f'<g class="wheel-center" data-life-full tabindex="0" role="button" aria-label="打开完整人生轮">'
        f'<text x="150" y="146" text-anchor="middle">人生轮</text><text x="150" y="160" text-anchor="middle">{html.escape(life.snapshot_date[5:])}</text>'
        '</g></svg>'
        '<div class="wheel-legend"><span class="build">建设</span><span class="maintain">维持</span></div>'
        '</div>'
    )


def render_page(
    records: list[DayRecord],
    month: str,
    tasks: list[TaskRecord] | None = None,
    life_wheel: LifeWheel | None = None,
    cases: list[CaseRecord] | None = None,
    available_months: list[str] | None = None,
) -> str:
    year, month_number = map(int, month.split("-"))
    first_weekday, days_in_month = calendar.monthrange(year, month_number)
    payload = []
    for record in records:
        item = asdict(record)
        item["preview"] = record.preview
        item["card_titles"] = record.card_titles
        item["sleep_duration"] = record.sleep_duration
        item["life_facts"] = record.life_facts
        payload.append(item)

    narrative_count = sum(len(r.narrative_titles) for r in records)
    total_hours = sum(r.billing_hours for r in records)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_json = safe_json(payload)
    life_json = safe_json(asdict(life_wheel) if life_wheel else None)
    task_html = _task_strip_html(tasks or [], cases or [])
    life_html = _life_summary_html(life_wheel)
    months = sorted(set(available_months or [month]))
    month_index = months.index(month) if month in months else 0
    previous_month = months[month_index - 1] if month_index > 0 else ""
    next_month = months[month_index + 1] if month_index + 1 < len(months) else ""
    previous_link = (
        f'<a class="month-step" href="?month={previous_month}" aria-label="上一个月">←</a>'
        if previous_month else '<span class="month-step disabled" aria-hidden="true">←</span>'
    )
    next_link = (
        f'<a class="month-step" href="?month={next_month}" aria-label="下一个月">→</a>'
        if next_month else '<span class="month-step disabled" aria-hidden="true">→</span>'
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{year} 年 {month_number} 月 · 日常档案</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #24312b; --muted: #6d786f; --paper: #f6f0e5;
      --card: rgba(255, 253, 248, .92); --line: #ded5c6;
      --moss: #496858; --moss-pale: #e5ebe2; --clay: #b77a66;
      --work-pale: #e3e9e8; --work-ink: #4f605f;
      --life-pale: #f3ddd2; --growth-pale: #f5ebcf; --narrative: #f1ede5;
      --recovery-pale: #f0edf2; --recovery-ink: #5d5962;
      --fuel-pale: #f5e9cb; --fuel-ink: #6f6040;
      --gold: #b48a35; --gold-pale: #f5ebcf; --alert: #9b3f37;
      --shadow: 0 16px 44px rgba(67, 55, 39, .10);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background:
      radial-gradient(circle at 12% 8%, #fffaf0 0, transparent 32rem),
      linear-gradient(145deg, #efe8db, var(--paper) 45%, #e8eee8);
      font-family: ui-serif, "Songti SC", "Noto Serif SC", Georgia, serif;
      min-height: 100vh; }}
    .shell {{ max-width: 1420px; margin: auto; padding: 48px 34px 72px; }}
    header {{ display: grid; grid-template-columns: 1fr auto; gap: 28px;
      align-items: end; margin-bottom: 30px; }}
    .eyebrow {{ color: var(--clay); letter-spacing: .16em; font: 700 12px/1.5 ui-sans-serif, sans-serif; }}
    h1 {{ margin: 7px 0 8px; font-size: clamp(32px, 5vw, 62px); font-weight: 500; letter-spacing: -.04em; }}
    .month-heading {{ display: flex; align-items: center; gap: 13px; }}
    .month-step {{ display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--line);
      border-radius: 999px; color: var(--moss); background: rgba(255,255,255,.56); text-decoration: none;
      font: 700 17px/1 ui-sans-serif, sans-serif; }}
    .month-step:hover {{ border-color: var(--moss); }} .month-step.disabled {{ opacity: .28; }}
    .subtitle {{ color: var(--muted); max-width: 670px; line-height: 1.7; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }}
    .stat {{ padding: 10px 14px; border: 1px solid var(--line); border-radius: 999px;
      background: rgba(255,255,255,.5); font: 600 13px/1 ui-sans-serif, sans-serif; }}
    .overview-section {{ margin: 0 0 24px; padding: 22px; border: 1px solid var(--line); border-radius: 24px;
      background: rgba(255,253,248,.72); box-shadow: 0 8px 28px rgba(67,55,39,.05); }}
    .section-head {{ display: flex; align-items: end; justify-content: space-between; gap: 18px; margin-bottom: 16px; }}
    .section-head h2 {{ margin: 0; font-size: 23px; font-weight: 520; }}
    .section-head p {{ margin: 0; color: var(--muted); font: 12px/1.5 ui-sans-serif, sans-serif; text-align: right; }}
    .now-overview {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(300px, .75fr); gap: 22px; align-items: start; }}
    .priority-side {{ min-width: 0; }}
    .wheel-side {{ min-width: 0; padding-left: 20px; border-left: 1px solid var(--line); }}
    .wheel-side-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 2px; }}
    .wheel-side-head b {{ font-size: 15px; font-weight: 650; }}
    .wheel-side-head span {{ color: var(--muted); font: 650 10px/1.3 ui-sans-serif, sans-serif; }}
    .priority-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .task-lane.now {{ grid-column: 1 / -1; }}
    .task-lane {{ min-width: 0; padding: 13px; border-radius: 17px; border: 1px solid var(--line); background: rgba(255,255,255,.52); }}
    .task-lane.now {{ border-color: rgba(183,111,82,.52); background: rgba(255,248,241,.78); }}
    .lane-head {{ display: flex; justify-content: space-between; gap: 8px; margin-bottom: 10px; color: var(--muted);
      font: 700 10px/1.3 ui-sans-serif, sans-serif; letter-spacing: .06em; }}
    .lane-head b {{ color: var(--clay); letter-spacing: .12em; }}
    .task-card {{ padding: 12px; border-radius: 14px; background: #fffdf8; box-shadow: 0 4px 14px rgba(67,55,39,.06); }}
    .task-card + .task-card {{ margin-top: 8px; }}
    .task-card h3 {{ margin: 3px 0 8px; font-size: 15px; line-height: 1.38; }}
    .task-id {{ color: var(--muted); font: 650 9px/1 ui-sans-serif, sans-serif; letter-spacing: .05em; }}
    .task-next, .task-artifact {{ margin: 6px 0 0; font-size: 12px; line-height: 1.55; }}
    .task-next b {{ color: var(--moss); }}
    .task-artifact {{ color: var(--muted); }}
    .task-meta {{ display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }}
    .task-meta span {{ padding: 3px 7px; border-radius: 999px; background: var(--moss-pale); color: #365143;
      font: 700 9px/1.2 ui-sans-serif, sans-serif; }}
    .case-pool {{ margin-top: 9px; border-top: 1px solid var(--line); padding-top: 9px; }}
    .case-pool-head, .case-row > summary {{ list-style: none; cursor: pointer; display: flex; justify-content: space-between;
      align-items: center; gap: 10px; font-family: ui-sans-serif, "PingFang SC", sans-serif; }}
    .case-row > summary::-webkit-details-marker {{ display: none; }}
    .case-pool-head b {{ font-size: 12px; color: var(--moss); }}
    .case-pool-head small, .case-pool summary small {{ display: block; margin-top: 3px; color: var(--muted); font-size: 9px; line-height: 1.35; }}
    .case-open {{ color: var(--clay); text-decoration: none; font: 700 9px/1 ui-sans-serif, sans-serif; }}
    .case-list {{ margin-top: 9px; overflow: auto; padding-right: 3px; }}
    .case-row {{ border: 1px solid var(--line); border-radius: 11px; background: rgba(255,255,255,.64); }}
    .case-row + .case-row {{ margin-top: 6px; }}
    .case-row > summary {{ padding: 9px 10px; align-items: flex-start; }}
    .case-row > summary b {{ font-size: 10px; color: var(--ink); }}
    .case-due {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; max-width: 42%; }}
    .case-due span, .case-tags span {{ padding: 3px 5px; border-radius: 999px; background: var(--moss-pale);
      color: #365143; white-space: nowrap; font: 700 8px/1.15 ui-sans-serif, sans-serif; }}
    .case-due .due-risk {{ background: #f5d8d4; color: var(--alert); }}
    .case-due .stale-soft {{ background: #ece7df; color: var(--muted); }}
    .case-body {{ padding: 0 10px 10px; }}
    .case-tags {{ display: flex; flex-wrap: wrap; gap: 4px; }}
    .case-tags .baseline-chip {{ background: #eee8dc; color: var(--muted); }}
    .case-tags .active-chip {{ background: #dcebd9; color: #365143; }}
    .case-tags .stage-chip {{ background: #e7e2f0; color: #625579; }}
    .case-milestone {{ margin: 7px 0 0; color: #554f47; font: 10px/1.55 ui-sans-serif, "PingFang SC", sans-serif; }}
    .case-milestone b {{ color: var(--moss); }}
    .case-description {{ margin: 7px 0 0; color: #554f47; font: 10px/1.55 ui-sans-serif, "PingFang SC", sans-serif; }}
    .case-description.muted {{ color: var(--muted); font-style: italic; }}
    .lane-empty {{ padding: 16px 5px; color: #aaa294; font-size: 12px; font-style: italic; }}
    .wheel-graphic {{ display: grid; justify-items: center; }}
    .wheel-graphic svg {{ width: min(340px, 100%); max-width: 100%; filter: drop-shadow(0 10px 14px rgba(73,104,88,.08)); }}
    .wheel-ring {{ fill: none; stroke: #d9d1c4; stroke-width: .8; }}
    .wheel-axes line {{ stroke: #d9d1c4; stroke-width: .8; }}
    .wheel-shape {{ fill: rgba(73,104,88,.20); stroke: var(--moss); stroke-width: 2.2; stroke-linejoin: round; }}
    .wheel-label {{ cursor: pointer; outline: none; }}
    .wheel-label text {{ font-family: ui-sans-serif, "PingFang SC", sans-serif; }}
    .wheel-domain {{ font-size: 11px; font-weight: 760; }}
    .wheel-label.build, .wheel-legend .build {{ fill: var(--clay); color: var(--clay); }}
    .wheel-label.maintain, .wheel-legend .maintain {{ fill: var(--moss); color: var(--moss); }}
    .wheel-label.reflect, .wheel-legend .reflect {{ fill: #8a6c9b; color: #8a6c9b; }}
    .wheel-label.simplify, .wheel-legend .simplify {{ fill: #77736d; color: #77736d; }}
    .wheel-label:hover .wheel-domain, .wheel-label:focus-visible .wheel-domain {{ text-decoration: underline; }}
    .wheel-center {{ cursor: pointer; fill: var(--muted); font: 650 9px/1 ui-sans-serif, sans-serif; }}
    .wheel-center text:first-child {{ fill: var(--ink); font-size: 10px; font-weight: 760; }}
    .wheel-legend {{ display: flex; gap: 12px; justify-content: center; margin-top: -10px;
      font: 750 9px/1.4 ui-sans-serif, sans-serif; }}
    .month-section {{ margin-top: 30px; }}
    .toolbar {{ display: flex; gap: 12px; align-items: center; margin-bottom: 18px; }}
    input {{ width: min(440px, 100%); border: 1px solid var(--line); border-radius: 14px;
      padding: 13px 16px; background: rgba(255,255,255,.72); color: var(--ink); font-size: 15px; outline: none; }}
    input:focus {{ border-color: var(--moss); box-shadow: 0 0 0 3px rgba(73,104,88,.12); }}
    .weekdays, .calendar {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; }}
    .weekdays {{ margin: 0 0 8px; color: var(--muted); text-align: center; font: 700 12px/1.5 ui-sans-serif, sans-serif; }}
    .day-card, .blank {{ min-height: 264px; }}
    .day-card {{ position: relative; display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: 18px;
      padding: 14px; background: var(--card); box-shadow: 0 4px 16px rgba(67,55,39,.05);
      cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease; overflow: hidden; }}
    .day-card:hover {{ transform: translateY(-2px); border-color: #b9aa95; box-shadow: var(--shadow); }}
    .day-card:focus-visible {{ outline: 3px solid rgba(73,104,88,.35); outline-offset: 2px; }}
    .day-top {{ display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 12px; }}
    .day-number {{ font-size: 30px; line-height: 1; }}
    .day-number.published {{ display: grid; place-items: center; width: 37px; height: 37px; margin: -4px 0 -4px -5px;
      border: 1.5px solid var(--gold); border-radius: 50%; }}
    .day-week {{ color: var(--muted); font: 700 11px/1 ui-sans-serif, sans-serif; }}
    .title-scroll-wrap {{ position: relative; margin-top: 14px; }}
    .title-scroll {{ max-height: 117px; overflow-y: auto; overscroll-behavior: contain; padding-right: 5px;
      scrollbar-width: thin; scrollbar-color: rgba(73,104,88,.26) transparent; }}
    .title-scroll::-webkit-scrollbar {{ width: 4px; }}
    .title-scroll::-webkit-scrollbar-track {{ background: transparent; }}
    .title-scroll::-webkit-scrollbar-thumb {{ background: rgba(73,104,88,.26); border-radius: 999px; }}
    .title-scroll-wrap.has-more::after {{ content: ""; position: absolute; left: 0; right: 5px; bottom: 0; height: 12px;
      pointer-events: none; background: linear-gradient(to bottom, rgba(255,253,248,0), var(--card)); }}
    .title-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }}
    .title-list li {{ position: relative; box-sizing: border-box; display: flex; align-items: center;
      width: fit-content; max-width: 100%; height: 15px; padding: 0 3px; border-radius: 4px;
      background: transparent; color: #403b34;
      font-size: 11px; font-weight: 650; line-height: 15px; }}
    .title-list li.primary {{ padding: 0 7px; font-weight: 720; }}
    .title-list li.primary.domain-body {{ background: var(--moss-pale); }}
    .title-list li.primary.domain-work {{ background: var(--work-pale); }}
    .title-list li.primary.domain-life {{ background: var(--life-pale); }}
    .title-list li.primary.domain-growth {{ background: var(--growth-pale); }}
    .title-list li.primary.domain-neutral {{ background: var(--narrative); }}
    .title-text {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .more {{ margin-top: 10px; color: var(--muted); font: 600 11px/1.4 ui-sans-serif, sans-serif; }}
    .facts {{ display: grid; gap: 4px; margin-top: auto; padding-top: 12px; }}
    .primary-facts, .life-facts {{ display: flex; flex-wrap: nowrap; justify-content: flex-end; gap: 4px;
      min-height: 16px; max-height: 17px; overflow: hidden; }}
    .fact {{ padding: 2px 6px; border-radius: 999px; background: var(--moss-pale); color: #31463b;
      max-width: 88px; overflow: hidden; text-overflow: ellipsis; font: 700 9px/1.35 ui-sans-serif, sans-serif; white-space: nowrap; }}
    .fact.work {{ background: var(--work-pale); color: var(--work-ink); }}
    .fact.recovery {{ background: var(--recovery-pale); color: var(--recovery-ink); }}
    .fact.fuel {{ background: var(--fuel-pale); color: var(--fuel-ink); }}
    .fact.movement {{ background: var(--moss-pale); color: #31463b; }}
    .day-card.guardrail {{ border-color: rgba(155,63,55,.5); }}
    .day-card.guardrail::after {{ content: ""; position: absolute; top: 10px; right: 10px; width: 6px; height: 6px;
      border-radius: 50%; background: var(--alert); }}
    .empty {{ color: #aaa294; font-size: 13px; font-style: italic; }}
    dialog {{ width: min(900px, calc(100vw - 28px)); max-height: calc(100vh - 40px); padding: 0;
      border: 1px solid var(--line); border-radius: 22px; background: #fffdf8; color: var(--ink); box-shadow: 0 30px 100px rgba(40,34,25,.28); }}
    dialog::backdrop {{ background: rgba(35, 39, 35, .45); backdrop-filter: blur(3px); }}
    .dialog-head {{ position: sticky; top: 0; z-index: 2; display: flex; justify-content: space-between; align-items: center;
      padding: 20px 24px; border-bottom: 1px solid var(--line); background: rgba(255,253,248,.96); }}
    .dialog-title {{ margin: 0; font-size: 25px; font-weight: 500; }}
    .close {{ border: 0; border-radius: 999px; width: 36px; height: 36px; background: var(--moss-pale); color: var(--ink); cursor: pointer; font-size: 21px; }}
    .day-body {{ padding: 8px 28px 36px; font-size: 16px; line-height: 1.8; }}
    .day-body h1 {{ font-size: 31px; }} .day-body h2 {{ margin-top: 30px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }}
    .day-body h3 {{ margin-top: 27px; color: #365143; }} .day-body li {{ margin: 5px 0; }}
    .day-body table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }}
    .day-body th, .day-body td {{ padding: 8px 10px; border: 1px solid var(--line); text-align: left; vertical-align: top; }}
    .day-body th {{ background: var(--moss-pale); }}
    .toc {{ margin: 14px 0 22px; padding: 13px 16px; border: 1px solid var(--line); border-radius: 14px; background: #f7f3ea; }}
    .toc-label {{ margin-bottom: 6px; color: var(--clay); font: 700 11px/1.4 ui-sans-serif, sans-serif; letter-spacing: .1em; }}
    .toc a {{ display: block; padding: 3px 0; color: var(--moss); text-decoration: none; font-size: 14px; line-height: 1.5; }}
    .toc a:hover {{ text-decoration: underline; }}
    .day-body blockquote {{ margin-left: 0; padding-left: 16px; border-left: 3px solid var(--moss); color: var(--muted); }}
    .source {{ color: var(--muted); font: 12px/1.5 ui-sans-serif, sans-serif; }}
    .hidden {{ display: none !important; }}
    footer {{ margin-top: 28px; color: var(--muted); font: 12px/1.7 ui-sans-serif, sans-serif; text-align: right; }}
    @media (max-width: 980px) {{
      .weekdays {{ display: none; }} .calendar {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .blank {{ display: none; }}
      header {{ grid-template-columns: 1fr; }} .stats {{ justify-content: flex-start; }}
      .now-overview {{ grid-template-columns: 1fr; }} .wheel-side {{ padding: 16px 0 0; border-left: 0; border-top: 1px solid var(--line); }}
    }}
    @media (max-width: 560px) {{ .shell {{ padding: 28px 14px 48px; }} .overview-section {{ padding: 16px; border-radius: 20px; }}
      .calendar {{ grid-template-columns: 1fr; }} .day-card {{ min-height: 200px; }} .section-head {{ align-items: start; flex-direction: column; }}
      .section-head p {{ text-align: left; }} .priority-grid {{ grid-template-columns: 1fr; }} .task-lane.now {{ grid-column: auto; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div><div class="eyebrow">LIFE DASHBOARD · DAILY ARCHIVE</div>
        <div class="month-heading">{previous_link}<h1>{year} 年 {month_number} 月</h1>{next_link}</div>
        <div class="subtitle">一个事实源，三种时间：此刻正在交付什么、这个月怎样真实发生、生活整体正在往哪里长。</div></div>
      <div class="stats"><span class="stat">{len(records)} 天有记录</span><span class="stat">{narrative_count} 段叙事</span><span class="stat">工作记录 {total_hours:g}h</span></div>
    </header>
    <section class="overview-section" aria-labelledby="priority-title"><div class="section-head"><h2 id="priority-title">此刻</h2><p>任务回答下一刀 · 人生轮回答整体怎样住着</p></div>
      <div class="now-overview"><div class="priority-side"><div class="priority-grid">{task_html}</div></div>
        <aside class="wheel-side" aria-label="人生轮"><div class="wheel-side-head"><b>人生轮</b><span>点标题／节点看详情</span></div>{life_html}</aside>
      </div>
    </section>
    <section class="month-section" aria-labelledby="month-title"><div class="section-head"><h2 id="month-title">{month_number} 月怎样发生</h2><p>结果进入时间；未完成的 task 不拖成逾期幽灵</p></div>
      <div class="toolbar"><input id="search" type="search" placeholder="搜这个月：项目、身体、某一句话……" aria-label="搜索月度归档"></div>
      <div class="weekdays"><span>周一</span><span>周二</span><span>周三</span><span>周四</span><span>周五</span><span>周六</span><span>周日</span></div>
      <section id="calendar" class="calendar" aria-label="{year}年{month_number}月日历"></section>
    </section>
    <footer>只读派生页 · canonical 为任务台、人生轮与 daily-log Markdown · 生成于 {generated}</footer>
  </main>
  <dialog id="dialog"><div class="dialog-head"><div><h2 class="dialog-title" id="dialog-title"></h2><div class="source" id="dialog-source"></div></div><button class="close" aria-label="关闭">×</button></div><article class="day-body" id="dialog-body"></article></dialog>
  <script>
    const records = {data_json};
    const lifeWheel = {life_json};
    const startOffset = {first_weekday};
    const daysInMonth = {days_in_month};
    const calendarEl = document.querySelector('#calendar');
    const dialog = document.querySelector('#dialog');
    const byDay = new Map(records.map(r => [r.day, r]));
    const escapeText = s => String(s ?? '').replace(/[&<>\"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));

    for (let i = 0; i < startOffset; i++) calendarEl.insertAdjacentHTML('beforeend', '<div class="blank" aria-hidden="true"></div>');
    for (let day = 1; day <= daysInMonth; day++) {{
      const r = byDay.get(day);
      if (!r) {{ calendarEl.insertAdjacentHTML('beforeend', `<div class="day-card empty" data-day="${{day}}"><div class="day-top"><span class="day-number">${{day}}</span></div>尚无归档</div>`); continue; }}
      const narratives = r.narrative_meta?.length
        ? r.narrative_meta
        : r.card_titles.map(title => ({{title, domain:'neutral', axes:[], primary:false}}));
      const titles = narratives.map(item => {{
        const primary = item.primary ? ' primary' : '';
        return `<li class="domain-${{escapeText(item.domain || 'neutral')}}${{primary}}" title="${{escapeText(item.title)}}"><span class="title-text">${{escapeText(item.title)}}</span></li>`;
      }}).join('');
      const more = r.narrative_titles.length ? `共 ${{r.narrative_titles.length}} 段` : (r.anchors.length ? `${{r.anchors.length}} 个 anchor` : '');
      const primaryFacts = [
        r.sleep_duration ? {{kind:'recovery', label:`睡 ${{r.sleep_duration}}`, title:r.sleep}} : null,
        r.wake_score ? {{kind:'recovery', label:`醒 ${{r.wake_score}}/5`, title:`醒来感觉 ${{r.wake_score}}/5`}} : null,
      ].filter(Boolean);
      const primaryHtml = primaryFacts.map(f => `<span class="fact ${{escapeText(f.kind)}}" title="${{escapeText(f.title || f.label)}}">${{escapeText(f.label)}}</span>`).join('');
      const secondaryFacts = [
        r.billing_hours ? {{kind:'work', label:`工作 ${{r.billing_hours}}h`, title:`工作记录 ${{r.billing_hours}}h`}} : null,
        ...r.life_facts,
      ].filter(Boolean);
      const lifeFacts = secondaryFacts.map(f => `<span class="fact ${{escapeText(f.kind)}}" title="${{escapeText(f.title || f.label)}}">${{escapeText(f.label)}}</span>`).join('');
      const searchText = [r.iso_date, ...r.anchors, ...r.narrative_titles, ...r.supplement_items, r.sleep, ...r.life_facts.map(f => f.label), r.publish, r.guardrail].join(' ').toLowerCase();
      const guardrailClass = r.guardrail ? ' guardrail' : '';
      const guardrailTitle = r.guardrail ? ` data-guardrail="${{escapeText(r.guardrail)}}"` : '';
      const published = r.publish ? ' published' : '';
      const publishTitle = r.publish ? ` title="${{escapeText(r.publish)}}"` : '';
      calendarEl.insertAdjacentHTML('beforeend', `<article class="day-card${{guardrailClass}}" tabindex="0" role="button" data-day="${{day}}" data-search="${{escapeText(searchText)}}"${{guardrailTitle}}><div class="day-top"><span class="day-number${{published}}"${{publishTitle}}>${{day}}</span><span class="day-week">${{r.weekday}}</span></div><div class="title-scroll-wrap"><div class="title-scroll" aria-label="当天全部叙事标题"><ul class="title-list">${{titles}}</ul></div></div>${{more ? `<div class="more">${{more}}</div>` : ''}}<div class="facts">${{primaryHtml ? `<div class="primary-facts">${{primaryHtml}}</div>` : ''}}${{lifeFacts ? `<div class="life-facts">${{lifeFacts}}</div>` : ''}}</div></article>`);
    }}

    document.querySelectorAll('.title-scroll').forEach(scroller => {{
      const wrap = scroller.closest('.title-scroll-wrap');
      const updateFade = () => wrap.classList.toggle('has-more', scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 1);
      updateFade();
      scroller.addEventListener('scroll', updateFade, {{passive: true}});
    }});

    function openDay(day) {{
      const r = byDay.get(Number(day)); if (!r) return;
      document.querySelector('#dialog-title').textContent = `${{r.iso_date}} · ${{r.weekday}}`;
      document.querySelector('#dialog-source').textContent = r.source_name;
      const links = r.narrative_titles.map((title, index) => `<a href="#narrative-${{index + 1}}" data-target="narrative-${{index + 1}}">${{escapeText(title)}}</a>`).join('');
      const toc = links ? `<nav class="toc" aria-label="当天叙事目录"><div class="toc-label">这一天的叙事</div>${{links}}</nav>` : '';
      document.querySelector('#dialog-body').innerHTML = toc + r.body_html;
      dialog.showModal();
    }}
    function openLife(index) {{
      if (!lifeWheel) return;
      const domain = lifeWheel.domains[Number(index)]; if (!domain) return;
      const delta = Number(domain.delta);
      const deltaLabel = delta > 0 ? `较上期 +${{delta}}` : (delta < 0 ? `较上期 ${{delta}}` : '较上期持平');
      document.querySelector('#dialog-title').textContent = `${{domain.name}} · ${{domain.score}} / 5`;
      document.querySelector('#dialog-source').textContent = `人生轮.md · ${{lifeWheel.snapshot_date}} · ${{domain.status}} · ${{deltaLabel}}`;
      document.querySelector('#dialog-body').innerHTML = domain.detail_html || '<p>该领域尚无详情。</p>';
      dialog.showModal();
    }}
    document.querySelectorAll('[data-life-index]').forEach(target => {{
      target.addEventListener('click', () => openLife(target.dataset.lifeIndex));
      target.addEventListener('keydown', event => {{ if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); openLife(target.dataset.lifeIndex); }} }});
    }});
    const openFullWheel = () => {{
      if (!lifeWheel) return;
      document.querySelector('#dialog-title').textContent = `完整人生轮 · ${{lifeWheel.snapshot_date}}`;
      document.querySelector('#dialog-source').textContent = '人生轮.md';
      document.querySelector('#dialog-body').innerHTML = lifeWheel.source_html;
      dialog.showModal();
    }};
    document.querySelector('[data-life-full]')?.addEventListener('click', openFullWheel);
    document.querySelector('[data-life-full]')?.addEventListener('keydown', event => {{ if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); openFullWheel(); }} }});
    calendarEl.addEventListener('click', e => {{ const card = e.target.closest('.day-card[data-search]'); if (card) openDay(card.dataset.day); }});
    calendarEl.addEventListener('keydown', e => {{ if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('.day-card[data-search]')) {{ e.preventDefault(); openDay(e.target.dataset.day); }} }});
    document.querySelector('.close').addEventListener('click', () => dialog.close());
    document.querySelector('#dialog-body').addEventListener('click', e => {{ const link = e.target.closest('.toc a'); if (!link) return; e.preventDefault(); document.getElementById(link.dataset.target)?.scrollIntoView({{behavior: 'smooth', block: 'start'}}); }});
    dialog.addEventListener('click', e => {{ if (e.target === dialog) dialog.close(); }});
    document.querySelector('#search').addEventListener('input', e => {{ const q = e.target.value.trim().toLowerCase(); document.querySelectorAll('.day-card[data-search]').forEach(card => card.classList.toggle('hidden', q && !card.dataset.search.includes(q))); }});
  </script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default=date.today().strftime("%Y-%m"), help="YYYY-MM")
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--task-ledger", type=Path, help="defaults to <vault>/任务台.md")
    parser.add_argument("--case-ledger", type=Path, help="defaults to <vault>/工作/case-ledger.md")
    parser.add_argument("--life-wheel", type=Path, help="defaults to <vault>/人生轮.md")
    parser.add_argument(
        "--live-today",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include today.md when generating the current month",
    )
    args = parser.parse_args()

    source_dir = args.vault_root.expanduser() / "daily-log" / args.month
    if not source_dir.is_dir():
        parser.error(f"daily-log month directory not found: {source_dir}")
    records = load_month(source_dir, args.month)
    if args.live_today and args.month == date.today().strftime("%Y-%m"):
        live_today = args.vault_root.expanduser() / "today.md"
        if live_today.is_file():
            live_record = parse_day(live_today, date.today().isoformat())
            records = [record for record in records if record.iso_date != live_record.iso_date]
            records.append(live_record)
            records.sort(key=lambda record: record.iso_date)
    if not records:
        parser.error(f"no daily logs found for {args.month}")

    task_path = (args.task_ledger or args.vault_root.expanduser() / "任务台.md").expanduser()
    case_path = (args.case_ledger or args.vault_root.expanduser() / "工作" / "case-ledger.md").expanduser()
    life_path = (args.life_wheel or args.vault_root.expanduser() / "人生轮.md").expanduser()
    tasks = load_task_ledger(task_path)
    cases = load_case_ledger(case_path)
    life_wheel = load_life_wheel(life_path)

    output = args.output or Path("output/daily-log-month-view") / f"{args.month}.html"
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_page(records, args.month, tasks, life_wheel, cases), encoding="utf-8")
    print(
        f"generated {output.resolve()} · {len(records)} days · "
        f"{len(tasks)} tasks · {len(cases)} cases · {len(life_wheel.domains) if life_wheel else 0} life domains"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
