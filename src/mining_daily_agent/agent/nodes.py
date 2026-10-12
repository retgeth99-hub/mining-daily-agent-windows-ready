"""LangGraph 节点：planner → fetch_data → analyze → synthesize → render。

每个取数调用都单独捕获异常并记入 ``risk_notes``，不阻塞整条流程
。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime
from html import escape
from typing import TYPE_CHECKING, Final, Protocol

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError

from mining_daily_agent.agent.llm import build_llm
from mining_daily_agent.agent.state import BriefState, FetchPlan
from mining_daily_agent.config import default_report_url, reports_dir
from mining_daily_agent.models.news import NewsItem
from mining_daily_agent.models.prices import TrendSeries
from mining_daily_agent.models.resources import ResourceCategory, ResourceReport

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """编排层错误。"""


class ToolCaller(Protocol):
    """节点需要的连接池能力。

    只声明 ``call_tool``：真实的 ``McpConnectionPool`` 与测试替身都满足它，
    节点因此不必依赖具体连接池类型。
    """

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> CallToolResult:
        """路由一次工具调用。"""
        raise NotImplementedError


#: 当前连接池。由 run_daily_brief 注入、测试直接替换。
#:
#: 用模块级变量而不是把池塞进 state 或 config：节点要是纯函数（需求如此），
#: 而把活的连接对象放进 TypedDict 状态里会污染「状态即数据」的语义。
_pool: ToolCaller | None = None


def set_pool(pool: ToolCaller | None) -> None:
    """注入/清除连接池。"""
    global _pool
    _pool = pool


def get_pool() -> ToolCaller:
    """取当前连接池。

    Raises:
        AgentError: 尚未注入连接池。
    """
    if _pool is None:
        msg = "连接池尚未注入；请先调用 agent.nodes.set_pool()。"
        raise AgentError(msg)
    return _pool


#: topic 命中这些词就按锂矿处理（需求：Pilbara/锂矿类默认需要 PDF）。
LITHIUM_HINTS: Final[tuple[str, ...]] = ("lithium", "li2o", "pilbara", "spodumene", "锂")
#: 默认计划的回溯天数。
DEFAULT_PLAN_DAYS: Final = 3
#: 价格走势的回看交易日数。
TREND_DAYS: Final = 30
#: 新闻标题/摘要里出现即视为风险信号。
RISK_KEYWORDS: Final[tuple[str, ...]] = (
    "halt",
    "dispute",
    "decline",
    "suspend",
    "delay",
    "lawsuit",
    "investigation",
    "downgrade",
)
#: 判断一条新闻是否可能指向资源报告的线索词。
RESOURCE_HINTS: Final = ("resource", "reserve", "mineral", "feasibility", "report")
#: 来源里给降级数据加的后缀，提醒读者这不是真实数据。
DEGRADED_MARK: Final = "【降级示例数据】"
#: 文件名里保留的字符：Unicode 字母数字、下划线、连字符（中文因此得以保留）。
_SLUG_STRIP_RE: Final = re.compile(r"[^\w\-]+", re.UNICODE)
#: 从可能带代码围栏的文本里抓第一个 JSON 对象。
_JSON_OBJECT_RE: Final = re.compile(r"\{.*\}", re.DOTALL)

_PLANNER_PROMPT: Final = """你是矿业研究助手。请为下面的简报主题制定取数计划。

主题：{topic}

只输出一个 JSON 对象，不要任何解释或 Markdown 围栏，字段如下：
- "keywords": 传给新闻检索的关键词（英文效果最好）。字符串或字符串数组均可，
  多个相关词用数组给出覆盖面更好
- "days": 新闻回溯天数，1-30 的整数
- "commodity": 关注的大宗商品，只能是 lithium / nickel / copper / cobalt 之一
- "needs_pdf": 是否需要抽取资源报告 PDF（涉及矿山项目、储量、资源量时为 true）
- "rationale": 一句话说明理由
"""

_SYNTHESIZE_PROMPT: Final = """你是矿业分析师。请根据下方资料，写一份关于「{topic}」的中文行情解读。

硬性要求：
- 只输出 3–5 条短要点，每条先写结论，再写依据；不要输出标题或表格
- 第一条必须说明行情方向（上涨 / 下跌 / 震荡）及最关键的涨跌幅
- 后续要点依次覆盖新闻驱动、资源量变化和最重要的风险；没有数据时明确写“数据缺失”
- **每条事实后面必须用 [编号] 标注来源**，编号对应「编号来源」清单
- 资料里没有的数字一律不要编造
- **标有「{degraded_mark}」的资料不是真实数据**：引用时必须写明它不可采信，
  绝不能当作真实报道或真实资源量陈述
- 避免空泛判断；正文控制在 260 字以内

## 编号来源

{sources}

## 资料

{data}
"""


# --- MCP 工具结果解码 -------------------------------------------------------


def _payload_from_result(result: CallToolResult) -> object:
    """取出工具返回的载荷。

    MCP 的结构化内容必须是对象，所以返回**列表**的工具会被 FastMCP 包一层
    ``{"result": [...]}``，而返回单个对象的工具直接给出对象本身——两种形态都要认。
    另外列表返回时 ``content`` 里每个元素各占一个文本块，只读第一块会丢数据，
    因此优先用 ``structured_content``。
    """
    structured = result.structured_content
    if structured is not None:
        if set(structured) == {"result"} and isinstance(structured["result"], list):
            return structured["result"]
        return structured
    for block in result.content:
        if isinstance(block, TextContent):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return None
    return None


def _raw_items(payload: object) -> list[object]:
    """把载荷规整成列表；单个对象也包成单元素列表。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return list(payload)
    return [payload]


def _models_from_result[T: BaseModel](result: CallToolResult, model: type[T]) -> list[T]:
    """把工具结果解析成模型列表。

    Raises:
        AgentError: 工具报错或载荷无法解析。
    """
    if result.is_error:
        raise AgentError(_error_text(result))
    payload = _payload_from_result(result)
    if payload is None:
        msg = "工具返回了空载荷。"
        raise AgentError(msg)
    try:
        return [model.model_validate(item) for item in _raw_items(payload)]
    except ValidationError as exc:
        msg = f"工具返回的载荷无法解析成 {model.__name__}：{exc}"
        raise AgentError(msg) from exc


def _model_from_result[T: BaseModel](result: CallToolResult, model: type[T]) -> T:
    """把工具结果解析成单个模型。

    Raises:
        AgentError: 工具报错、载荷为空或无法解析。
    """
    items = _models_from_result(result, model)
    if not items:
        msg = f"工具没有返回 {model.__name__}。"
        raise AgentError(msg)
    return items[0]


def _error_text(result: CallToolResult) -> str:
    """拼接工具错误结果里的文本。"""
    parts = [block.text for block in result.content if isinstance(block, TextContent)]
    return "；".join(parts) or "工具执行失败（无详情）"


def _message_text(message: BaseMessage) -> str:
    """把消息内容压成纯文本（内容可能是多块结构）。"""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        else:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


# --- planner ----------------------------------------------------------------


def default_plan(topic: str) -> FetchPlan:
    """LLM 不可用或输出不可解析时的兜底计划。

    Pilbara / 锂矿类主题默认需要 PDF（需求指定）。
    """
    lowered = topic.casefold()
    is_lithium = any(hint in lowered for hint in LITHIUM_HINTS)
    return FetchPlan(
        keywords=topic.strip(),
        days=DEFAULT_PLAN_DAYS,
        commodity="lithium" if is_lithium else "copper",
        needs_pdf=is_lithium,
        rationale="默认计划（未采用 LLM 输出）",
    )


def _extract_json_object(text: str) -> dict[str, object] | None:
    """从可能带代码围栏的文本里取第一个 JSON 对象。"""
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_plan(text: str) -> FetchPlan | None:
    """把 LLM 输出解析成取数计划；不可解析时返回 None。"""
    payload = _extract_json_object(text)
    if payload is None:
        return None
    try:
        return FetchPlan.model_validate(payload)
    except ValidationError:
        return None


async def planner(state: BriefState) -> dict[str, object]:
    """调 LLM 产出取数计划；调用失败或输出不可解析时退回默认计划。"""
    topic = state["topic"]
    notes: list[str] = []
    plan: FetchPlan | None = None
    try:
        response = await build_llm().ainvoke(_PLANNER_PROMPT.format(topic=topic))
        plan = parse_plan(_message_text(response))
    except Exception as exc:  # 计划只是优化项，拿不到也要继续跑
        notes.append(f"LLM 计划生成失败，已使用默认计划：{type(exc).__name__}: {exc}")

    if plan is None:
        if not notes:
            notes.append("LLM 计划输出无法解析为 JSON，已使用默认计划。")
        plan = default_plan(topic)

    logger.info(
        "planner 完成：keywords=%r days=%d commodity=%s needs_pdf=%s",
        plan.keywords,
        plan.days,
        plan.commodity,
        plan.needs_pdf,
    )
    return {"plan": plan, "risk_notes": notes}


# --- fetch_data -------------------------------------------------------------


def _pick_report_url(news: list[NewsItem]) -> str | None:
    """挑一个交给 ``pdf.extract_resources`` 的地址，按确定性从高到低尝试。

    优先级：

    1. 新闻里**真正以 ``.pdf`` 结尾**的链接；
    2. ``config.default_report_url()``——可配置的**确定性**年报地址；
    3. 标题/链接里带 resource、report 等线索词的新闻条目。

    2 排在 3 前面是这条链路的要害：**Google News 返回的条目几乎不是 .pdf**，而线索词
    命中的绝大多数是普通新闻网页（矿业公司名里带 "Resources" 极常见，如
    "Raiden Resources"）。把新闻页喂给 PDF 解析器只会解析失败，再降级成 mock——
    也就是说挑到哪条全看运气，每跑一次结果都可能不同。回溯到确定的年报就没有这个问题。

    ⚠️ 3 是**保底分支，当前实际上不可达**：``default_report_url()`` 有内置默认值，
    不会返回空串，所以走到 ``for`` 循环前必定已经 return。保留它是因为「确定性优先」
    这条原则要写死在代码里——万一将来内置默认值被去掉或可被显式关闭，它仍然接得住，
    而不至于变成「挑到哪条看运气」。
    """
    pdf_urls = [item.url for item in news if item.url.casefold().endswith(".pdf")]
    if pdf_urls:
        return pdf_urls[0]

    configured = default_report_url()
    if configured:
        return configured

    for item in news:
        haystack = f"{item.title} {item.url}".casefold()
        if any(hint in haystack for hint in RESOURCE_HINTS):
            return item.url
    return None


async def fetch_data(state: BriefState) -> dict[str, object]:
    """并行取新闻与价格，再按需抽取资源报告；单个失败只记风险、不阻塞。"""
    pool = get_pool()
    plan = state["plan"]
    notes: list[str] = []

    news_result, price_result = await asyncio.gather(
        pool.call_tool("news", "search", {"query": plan.keywords, "days": plan.days}),
        pool.call_tool("price", "get_trend", {"commodity": plan.commodity, "days": TREND_DAYS}),
        return_exceptions=True,
    )

    news: list[NewsItem] = []
    if isinstance(news_result, BaseException):
        notes.append(f"新闻源失败，已降级：{type(news_result).__name__}: {news_result}")
    else:
        try:
            news = _models_from_result(news_result, NewsItem)
        except AgentError as exc:
            notes.append(f"新闻源失败，已降级：{exc}")
    if any(item.degraded for item in news):
        # mock 新闻带真实标题、来源与域名，不做这一步简报会把合成内容当报道引用。
        notes.append(
            "新闻为降级后的示例数据（真实 RSS 源不可用），标题与来源均系伪造，"
            "不得当作真实报道引用。"
        )

    trend: TrendSeries | None = None
    if isinstance(price_result, BaseException):
        notes.append(f"价格源失败，已降级：{type(price_result).__name__}: {price_result}")
    else:
        try:
            trend = _model_from_result(price_result, TrendSeries)
        except AgentError as exc:
            notes.append(f"价格源失败，已降级：{exc}")

    report: ResourceReport | None = None
    if plan.needs_pdf:
        report_url = _pick_report_url(news)
        if report_url is None:
            notes.append(
                "未找到资源报告 PDF，储量数据缺失；可设置 DEFAULT_REPORT_URL 指定兜底年报。"
            )
        else:
            try:
                result = await pool.call_tool("pdf", "extract_resources", {"pdf_url": report_url})
                report = _model_from_result(result, ResourceReport)
            except Exception as exc:  # 报告缺失不应中断简报，任何异常都只记风险
                notes.append(f"资源报告解析失败，已降级：{type(exc).__name__}: {exc}")

    if report is not None and report.degraded:
        # 合成吨位如果不加披露，简报会把它们当真实资源量呈现——那比报错更糟。
        notes.append("资源量为降级后的合成数据（PDF 未能真实解析），数值不可用于任何判断。")

    article = news[0] if news else None
    logger.info(
        "fetch_data 完成：news=%d trend=%s report=%s risks=%d",
        len(news),
        trend is not None,
        report is not None,
        len(notes),
    )
    return {
        "news": news,
        "article": article,
        "price_trend": trend,
        "resource_report": report,
        "risk_notes": notes,
    }


# --- analyze ----------------------------------------------------------------


def _price_highlight(trend: TrendSeries) -> str:
    """把走势序列压成一行可引用的统计。"""
    parts = [f"{trend.commodity} 区间涨跌 {trend.change_pct:+.2f}%"]
    parts.append(f"区间 {trend.min:.4g}–{trend.max:.4g}")
    if trend.ma7 is not None:
        parts.append(f"ma7={trend.ma7:.4g}")
    if trend.ma30 is not None:
        parts.append(f"ma30={trend.ma30:.4g}")
    return "；".join(parts) + f"（{len(trend.points)} 个交易日）"


def _resource_highlights(report: ResourceReport) -> tuple[list[str], list[str]]:
    """按类别汇总吨位，并与报告自报合计做交叉核对。

    JORC 资源表同时列出**各分块小计**与**全矿总计**（Pilgangoora 的 In-situ 436 +
    Stockpiles 9 与全矿总计 445 是同一份资源量的三种口径）。解析器已按分块去重，
    这里再核一次：偏差超过 `ResourceReport.reconciliation` 里的容差（±5%）就说明
    解析口径仍然不对，此时以**报告自报合计**为准并留下提示——一个可疑的求和值
    不该冒充事实出现在简报里。

    Returns:
        ``(高亮行, 风险提示行)``。
    """
    totals: dict[ResourceCategory, float] = {}
    for item in report.resources:
        totals[item.category] = totals.get(item.category, 0.0) + item.tonnage_t

    reconciliation = report.reconciliation
    if reconciliation is not None and reconciliation.used_self_reported:
        note = (
            f"资源量按类别逐行求和得 {reconciliation.parsed_total_t / 1e6:.1f} Mt，"
            f"与报告自报合计 {reconciliation.self_reported_total_t / 1e6:.1f} Mt 相差 "
            f"{reconciliation.difference_ratio:+.1%}"
            f"（超过 ±{reconciliation.tolerance:.0%} 容差）——求和值不可信"
            "（常见于同一份资源量被重复计入，或多张不同项目的资源表被加在了一起），"
            "已以报告自报合计为准。"
        )
        headline = (
            f"{report.project_name} 自报资源量合计 "
            f"{reconciliation.self_reported_total_t / 1e6:.1f} Mt"
        )
        return [headline], [note]

    highlights: list[str] = []
    for category in (ResourceCategory.INDICATED, ResourceCategory.INFERRED):
        tonnes = totals.get(category)
        if tonnes:
            highlights.append(f"{report.project_name} {category.value} 合计 {tonnes / 1e6:.1f} Mt")
    return highlights, []


async def analyze(state: BriefState) -> dict[str, object]:
    """纯计算：价格统计、储量汇总、新闻标题里的风险词。"""
    highlights: list[str] = []
    notes: list[str] = []

    trend = state["price_trend"]
    if trend is not None:
        highlights.append(_price_highlight(trend))

    report = state["resource_report"]
    if report is not None:
        resource_highlights, resource_notes = _resource_highlights(report)
        highlights.extend(resource_highlights)
        notes.extend(resource_notes)

    for item in state["news"]:
        lowered = f"{item.title} {item.summary}".casefold()
        hits = [keyword for keyword in RISK_KEYWORDS if keyword in lowered]
        if hits:
            notes.append(f"风险信号（{'、'.join(hits)}）：{item.title}")

    return {"highlights": highlights, "risk_notes": notes}


# --- synthesize -------------------------------------------------------------


def build_citations(state: BriefState) -> list[str]:
    """按固定顺序给出编号来源。

    synthesize 需要编号才能要求 LLM 标注 [n]，render 需要同一份编号出「来源」小节；
    两处都调用本函数，保证编号一致（它是纯函数，结果只取决于 state）。
    """
    citations = [
        f"{item.title} — {item.url}（{item.source}{DEGRADED_MARK if item.degraded else ''}）"
        for item in state["news"]
    ]
    report = state["resource_report"]
    if report is not None:
        mark = DEGRADED_MARK if report.degraded else ""
        citations.append(f"{report.project_name} 资源量报告 — {report.source_url}{mark}")
    trend = state["price_trend"]
    if trend is not None:
        citations.append(f"{trend.commodity} 价格数据 — {trend.source}")
    return citations


def _synthesis_data(state: BriefState) -> str:
    """把状态里的数据整理成给 LLM 的资料块。"""
    lines: list[str] = []

    if state["news"]:
        lines.append("### 新闻")
        lines.extend(
            f"- [{index}] {DEGRADED_MARK if item.degraded else ''}{item.title}：{item.summary}"
            for index, item in enumerate(state["news"], 1)
        )

    if state["highlights"]:
        lines.append("### 计算结果")
        lines.extend(f"- {item}" for item in state["highlights"])

    if state["resource_report"] is not None:
        lines.append("### 资源量明细")
        lines.extend(
            f"- {item.category.value} {item.commodity}：{item.tonnage_t / 1e6:.1f} Mt"
            + (f" @ {item.grade}{item.grade_unit}" if item.grade is not None else "")
            for item in state["resource_report"].resources
        )

    if state["risk_notes"]:
        lines.append("### 风险提示")
        lines.extend(f"- {item}" for item in state["risk_notes"])

    return "\n".join(lines) if lines else "（本次没有取到任何数据）"


async def synthesize(state: BriefState) -> dict[str, object]:
    """调 LLM 生成精简解读；失败时保留结构化日报而不让整条流程中断。"""
    citations = build_citations(state)
    sources = "\n".join(f"{index}. {text}" for index, text in enumerate(citations, 1))
    prompt = _SYNTHESIZE_PROMPT.format(
        topic=state["topic"],
        sources=sources or "（无来源）",
        data=_synthesis_data(state),
        degraded_mark=DEGRADED_MARK,
    )
    try:
        response = await build_llm().ainvoke(prompt)
        analysis = _message_text(response).strip()
    except Exception as exc:  # 行情表由 render 确定性生成，LLM 失败不应阻断日报
        logger.warning("行情解读生成失败，使用结构化日报：error=%s: %s", type(exc).__name__, exc)
        return {
            "markdown": "- 自动分析暂不可用；请直接参考上方行情数据和下方风险提示。",
            "risk_notes": [f"自动分析服务不可用：{type(exc).__name__}: {exc}"],
        }
    if not analysis:
        return {
            "markdown": "- 自动分析未返回内容；请直接参考上方行情数据和下方风险提示。",
            "risk_notes": ["自动分析未返回内容。"],
        }
    return {"markdown": analysis}


# --- render -----------------------------------------------------------------


def slugify(topic: str) -> str:
    """把主题转成可用作文件名的 slug；保留 Unicode 字母（因此中文会保留）。"""
    slug = _SLUG_STRIP_RE.sub("-", topic.strip()).strip("-")
    return (slug or "brief")[:80]


def report_path(topic: str, today: date | None = None) -> Path:
    """简报的落盘路径：``<REPORTS_DIR>/YYYY-MM-DD-<slug>.md``。"""
    day = today if today is not None else datetime.now(UTC).date()
    return reports_dir() / f"{day.isoformat()}-{slugify(topic)}.md"


def _cell(text: object) -> str:
    """把值转换成不会破坏 Markdown 表格的单行文本。"""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _trend_direction(change_pct: float) -> str:
    """把区间涨跌映射为适合扫读的方向标签。"""
    if change_pct >= 1:
        return "▲ 上涨"
    if change_pct <= -1:
        return "▼ 下跌"
    return "→ 震荡"


def _price_section(state: BriefState) -> str:
    """生成固定的行情仪表盘，避免关键数字埋在 LLM 长文本里。"""
    trend = state["price_trend"]
    if trend is None:
        return "\n".join(
            (
                "| 指标 | 当前值 | 快速判断 |",
                "|---|---:|---|",
                "| 价格走势 | 数据缺失 | 无法判断行情方向 |",
            )
        )

    latest = trend.points[-1]
    citation = len(state["news"]) + (1 if state["resource_report"] is not None else 0) + 1
    direction = _trend_direction(trend.change_pct)
    average_parts: list[str] = []
    strength_parts: list[str] = []
    if trend.ma7 is not None:
        average_parts.append(f"MA7 {trend.ma7:.4g}")
        strength_parts.append("短线强于 MA7" if latest.price >= trend.ma7 else "短线弱于 MA7")
    if trend.ma30 is not None:
        average_parts.append(f"MA30 {trend.ma30:.4g}")
        strength_parts.append("中期强于 MA30" if latest.price >= trend.ma30 else "中期弱于 MA30")
    averages = " / ".join(average_parts) or "样本不足"
    strength = "；".join(strength_parts) or "样本不足，暂不判断强弱"
    source_lower = trend.source.casefold()
    if "mock:" in source_lower or "synthesized" in source_lower:
        quote_kind = "⚠ 合成示例数据"
        quote_note = "不可用于行情判断"
    elif "proxy quote" in source_lower:
        quote_kind = "上市工具代理价"
        quote_note = "非 LME 金属现货价"
    else:
        quote_kind = "市场报价"
        quote_note = "口径见来源"
    latest_value = f"{latest.price:.4g} {latest.unit}"
    latest_context = f"{latest.date.isoformat()} [{citation}]"
    range_value = f"{trend.min:.4g}–{trend.max:.4g} {latest.unit}"
    range_context = f"{len(trend.points)} 个交易日 [{citation}]"
    return "\n".join(
        (
            "| 指标 | 当前值 | 快速判断 |",
            "|---|---:|---|",
            f"| 区间涨跌 | **{trend.change_pct:+.2f}%** | **{direction}** [{citation}] |",
            f"| 最新价格 | {latest_value} | {latest_context} |",
            f"| 区间范围 | {range_value} | {range_context} |",
            f"| 均线 | {averages} | {strength} [{citation}] |",
            f"| 数据口径 | {quote_kind} | {quote_note} [{citation}] |",
        )
    )


def _write_price_chart(state: BriefState, day: date) -> str | None:
    """把价格走势写成零依赖 SVG，返回相对报告目录的 Markdown 路径。"""
    trend = state["price_trend"]
    if trend is None or len(trend.points) < 2:
        return None

    width, height = 960, 360
    left, right, top, bottom = 72, 28, 58, 54
    plot_width = width - left - right
    plot_height = height - top - bottom
    prices = [point.price for point in trend.points]
    reference_values = [value for value in (trend.ma7, trend.ma30) if value is not None]
    low = min([*prices, *reference_values])
    high = max([*prices, *reference_values])
    padding = (high - low) * 0.08 or max(abs(high) * 0.02, 1.0)
    chart_low = low - padding
    chart_high = high + padding

    def x_at(index: int) -> float:
        return left + index * plot_width / (len(prices) - 1)

    def y_at(value: float) -> float:
        return top + (chart_high - value) * plot_height / (chart_high - chart_low)

    points = " ".join(f"{x_at(index):.1f},{y_at(value):.1f}" for index, value in enumerate(prices))
    grid: list[str] = []
    for step in range(5):
        grid_value = chart_high - step * (chart_high - chart_low) / 4
        y = y_at(grid_value)
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#334155" stroke-width="1"/>'
        )
        grid.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="#94a3b8" font-size="12">{grid_value:.2f}</text>'
        )

    averages: list[str] = []
    for label, average_value, color in (
        ("MA7", trend.ma7, "#f59e0b"),
        ("MA30", trend.ma30, "#a78bfa"),
    ):
        if average_value is None:
            continue
        y = y_at(average_value)
        averages.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-width="1.5" stroke-dasharray="7 6"/>'
        )
        averages.append(
            f'<text x="{width - right - 4}" y="{y - 7:.1f}" text-anchor="end" '
            f'fill="{color}" font-size="12">{label} {average_value:.2f}</text>'
        )

    first = trend.points[0]
    middle = trend.points[len(trend.points) // 2]
    latest = trend.points[-1]
    title = escape(f"{state['topic']} · {len(prices)} 日价格走势")
    subtitle = escape(f"{latest.unit} · {trend.change_pct:+.2f}% · {latest.date.isoformat()}")
    svg = "\n".join(
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">',
            '<rect width="100%" height="100%" rx="16" fill="#0f172a"/>',
            f'<text x="{left}" y="28" fill="#f8fafc" font-size="18" '
            f'font-weight="700">{title}</text>',
            f'<text x="{width - right}" y="28" text-anchor="end" fill="#94a3b8" '
            f'font-size="13">{subtitle}</text>',
            *grid,
            *averages,
            f'<polyline points="{points}" fill="none" stroke="#38bdf8" '
            'stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>',
            f'<circle cx="{x_at(len(prices) - 1):.1f}" cy="{y_at(latest.price):.1f}" r="5" '
            'fill="#fb7185" stroke="#fff" stroke-width="2"/>',
            f'<text x="{left}" y="{height - 20}" fill="#94a3b8" font-size="12">'
            f"{first.date.isoformat()}</text>",
            f'<text x="{x_at(len(prices) // 2):.1f}" y="{height - 20}" text-anchor="middle" '
            f'fill="#94a3b8" font-size="12">{middle.date.isoformat()}</text>',
            f'<text x="{width - right}" y="{height - 20}" text-anchor="end" '
            f'fill="#94a3b8" font-size="12">{latest.date.isoformat()}</text>',
            "</svg>",
        )
    )

    filename = f"{day.isoformat()}-{slugify(state['topic'])}-price-trend.svg"
    assets_dir = reports_dir() / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / filename).write_text(svg, encoding="utf-8")
    return f"assets/{filename}"


def _news_section(state: BriefState) -> str:
    """按时间列出最多五条新闻驱动，摘要保持一眼可扫。"""
    if not state["news"]:
        return "- 数据缺失：本次没有取得可用新闻。"
    lines: list[str] = []
    for index, item in enumerate(state["news"][:5], 1):
        summary = _cell(item.summary)
        if len(summary) > 180:
            summary = summary[:179].rstrip() + "…"
        mark = f" {DEGRADED_MARK}" if item.degraded else ""
        lines.append(
            f"- **{item.published_at.date().isoformat()}｜{_cell(item.title)}**{mark} "
            f"— {summary} [{index}]"
        )
    return "\n".join(lines)


def _resource_section(state: BriefState) -> str:
    """用表格展示资源量，保持类别、吨位和品位口径清晰。"""
    report = state["resource_report"]
    if report is None or not report.resources:
        return "- 数据缺失：本次没有取得可用资源量记录。"
    citation = len(state["news"]) + 1
    rows = ["| 类别 | 矿种 | 吨位 | 品位 |", "|---|---|---:|---:|"]
    for item in report.resources:
        grade = f"{item.grade:g} {item.grade_unit}" if item.grade is not None else "—"
        rows.append(
            f"| {_cell(item.category.value)} | {_cell(item.commodity)} | "
            f"{item.tonnage_t / 1e6:.2f} Mt | {_cell(grade)} [{citation}] |"
        )
    return "\n".join(rows)


def _risk_section(state: BriefState) -> str:
    """把所有降级与风险集中列出，便于快速判断报告可信度。"""
    if not state["risk_notes"]:
        return "- 未发现由当前数据直接触发的风险信号；仍需关注数据时效与代理价格口径。"
    return "\n".join(f"- {_cell(note)}" for note in state["risk_notes"])


async def render(state: BriefState) -> dict[str, object]:
    """用固定信息层级生成可快速扫读的 Markdown 日报。"""
    citations = build_citations(state)
    sources = (
        "\n".join(f"{index}. {text}" for index, text in enumerate(citations, 1))
        if citations
        else "（本次没有取到可引用的来源）"
    )
    trend = state["price_trend"]
    direction = _trend_direction(trend.change_pct) if trend is not None else "数据缺失"
    resource_count = len(state["resource_report"].resources) if state["resource_report"] else 0
    summary_line = (
        f"> **行情：{direction}** ｜ 新闻：{len(state['news'])} 条 ｜ "
        f"资源记录：{resource_count} 条 ｜ 风险提示：{len(state['risk_notes'])} 条"
    )
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    report_day = datetime.now(UTC).date()
    chart_path = _write_price_chart(state, report_day)
    chart = f"\n\n![{state['topic']} 价格趋势图]({chart_path})" if chart_path else ""
    body = state["markdown"].strip() or "- 暂无自动分析。"
    document = (
        "\n\n".join(
            (
                f"# {state['topic']} 每日简报",
                f"> 生成时间：{generated_at}\n>\n{summary_line}",
                f"## 行情速览\n\n{_price_section(state)}{chart}",
                f"## 关键新闻\n\n{_news_section(state)}",
                f"## 资源量\n\n{_resource_section(state)}",
                f"## 分析解读\n\n{body}",
                f"## 风险与数据质量\n\n{_risk_section(state)}",
                f"## 来源\n\n{sources}",
            )
        )
        + "\n"
    )

    path = report_path(state["topic"], today=report_day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    logger.info("简报已写入：path=%s chars=%d", path, len(document))
    return {"markdown": document, "citations": citations}
