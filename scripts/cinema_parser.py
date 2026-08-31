# coding: utf-8
"""淘票票影院页场次解析器。

真实 DOM 结构（来自 probe_dom.py 实测，cinemaDetail.htm 场次表）：

    <tbody>
      <tr>                                   <!-- 奇数行无 class，偶数行 class="even" -->
        <td class="hall-time">
          <em class="bold">14:20</em> 预计16:40散场
        </td>
        <td class="hall-type">国语 2D</td>
        <td class="hall-name">2号餐影厅（...）</td>
        <td class="hall-flow">
          <div class="flowing-wrap flowing-loose">
            <label> 宽松 </label>
            <span class="flowing-view J_flowingView" data-scheduleid="1735338847"></span>
          </div>
        </td>
        <td class="hall-price" data-partcode="chenxing">
          <em class="now">29.90</em>
          <del class="old">70.00</del>
        </td>
        <td class="hall-seat">
          <div class="seat-btn to-choose-seat-btn"
               data-schedule-id="1735338847" data-cinema-id="77973"
               data-show-id="1425620">选座购票 ...</div>
        </td>
      </tr>
    </tbody>

用法：
    from scripts.cinema_parser import parse_schedule_html, pick_sessions

    sessions = parse_schedule_html(driver.page_source)
    matched = pick_sessions(sessions, session_priorities=["19:00", "20:15"])
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise ImportError("需要 beautifulsoup4: pip install beautifulsoup4") from exc


# 行选择器：场次表的每一行
ROW_SELECTOR = "tr"
TD_CLASSES = ("hall-time", "hall-type", "hall-name", "hall-flow", "hall-price", "hall-seat")

# 状态文案 → 售卖状态
END_TIME_RE = re.compile(r"预计\s*(\d{1,2}:\d{2})\s*散场")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
FLOW_CLASS_MAP = {
    "flowing-loose": "宽松",
    "flowing-normal": "适中",
    "flowing-tight": "紧张",
    "flowing-full": "满座",
}

# 可购票的按钮 class（不可购时通常是 "停售" 灰色按钮）
BUYABLE_BTN_CLASSES = ("to-choose-seat-btn", "buy-btn")


@dataclass
class SessionInfo:
    """一条场次记录。"""

    start_time: str = ""          # 开场时间 "14:20"
    end_time: str = ""            # 预计散场 "16:40"
    language: str = ""            # "国语" / "英语" ...
    dimension: str = ""           # "2D" / "3D" / "IMAX 2D" ...
    hall: str = ""                # 影厅名
    flow_status: str = ""         # 上座情况: 宽松/适中/紧张/满座
    price_now: Optional[float] = None   # 折后价
    price_old: Optional[float] = None   # 挂牌价
    part_code: str = ""           # 交易方 (data-partcode)
    schedule_id: str = ""         # 场次 ID（下单关键参数）
    cinema_id: str = ""
    show_id: str = ""             # 影片场次组 ID
    buyable: bool = False         # 是否可点选座购票
    raw_text: str = field(default="", repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def parse_schedule_html(html: str) -> List[SessionInfo]:
    """从影院页 HTML 中解析全部场次行。"""
    soup = BeautifulSoup(html, "html.parser")
    sessions: List[SessionInfo] = []
    for tr in soup.select(ROW_SELECTOR):
        tds = {td.get("class", [""])[0]: td for td in tr.find_all("td")}
        # 只处理完整场次行（必须同时有时间列和座位/价格列）
        if "hall-time" not in tds:
            continue
        if not ("hall-seat" in tds or "hall-price" in tds):
            continue

        info = SessionInfo()
        _fill_time(info, tds.get("hall-time"))
        _fill_type(info, tds.get("hall-type"))
        _fill_hall(info, tds.get("hall-name"))
        _fill_flow(info, tds.get("hall-flow"))
        _fill_price(info, tds.get("hall-price"))
        _fill_seat(info, tds.get("hall-seat"))

        # 没解析出有效开场时间的行直接丢弃（防误判其他表格）
        if not TIME_RE.match(info.start_time or ""):
            continue
        info.raw_text = " ".join(tr.get_text(" ", strip=True).split())
        sessions.append(info)
    return sessions


def _fill_time(info: SessionInfo, td: Any) -> None:
    if td is None:
        return
    em = td.select_one("em.bold") or td
    info.start_time = em.get_text(strip=True)
    m = END_TIME_RE.search(td.get_text(" ", strip=True))
    if m:
        info.end_time = m.group(1)


def _fill_type(info: SessionInfo, td: Any) -> None:
    """hall-type 形如 '国语 2D' / '英语 3D' / '国语 IMAX'。"""
    if td is None:
        return
    text = " ".join(td.get_text(" ", strip=True).split())
    info.language, info.dimension = _split_type(text)


def _split_type(text: str):
    text = text.strip()
    if not text:
        return "", ""
    m = re.match(r"^(.*?)\s*((?:IMAX\s*)?(?:2D|3D|4D|杜比|CINITY|CGS)?)$", text, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip().upper()
    return text, ""


def _fill_hall(info: SessionInfo, td: Any) -> None:
    if td is None:
        return
    info.hall = " ".join(td.get_text(" ", strip=True).split())


def _fill_flow(info: SessionInfo, td: Any) -> None:
    if td is None:
        return
    wrap = td.select_one(".flowing-wrap")
    if wrap is None:
        return
    # 优先用 class（flowing-loose 等），兜底用 label 文本
    for cls in wrap.get("class", []):
        if cls in FLOW_CLASS_MAP:
            info.flow_status = FLOW_CLASS_MAP[cls]
            return
    label = wrap.find("label")
    if label:
        info.flow_status = label.get_text(strip=True)


def _fill_price(info: SessionInfo, td: Any) -> None:
    if td is None:
        return
    info.part_code = td.get("data-partcode", "") or ""
    now = td.select_one("em.now")
    old = td.select_one("del.old")
    info.price_now = _to_float(now.get_text(strip=True) if now else None)
    info.price_old = _to_float(old.get_text(strip=True) if old else None)


def _fill_seat(info: SessionInfo, td: Any) -> None:
    if td is None:
        return
    btn = td.select_one(".seat-btn") or td.select_one("[data-schedule-id]")
    if btn is None:
        return
    info.schedule_id = btn.get("data-schedule-id", "") or btn.get("data-scheduleid", "") or ""
    info.cinema_id = btn.get("data-cinema-id", "") or ""
    info.show_id = btn.get("data-show-id", "") or ""
    classes = btn.get("class", [])
    info.buyable = any(c in classes for c in BUYABLE_BTN_CLASSES) and bool(info.schedule_id)


def _to_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return float(text.replace("¥", "").replace("￥", "").strip())
    except ValueError:
        return None


# ----------------------------------------------------------------------
# 筛选
# ----------------------------------------------------------------------
def pick_sessions(
    sessions: Iterable[SessionInfo],
    session_priorities: Optional[List[str]] = None,
    language: Optional[str] = None,
    dimension: Optional[str] = None,
    buyable_only: bool = True,
) -> List[SessionInfo]:
    """按优先级时间筛场次。

    session_priorities 形如 ["19:00", "20:15"]：按顺序返回可用的匹配项，
    不在优先级列表里的场次不会被返回。
    """
    pool = [s for s in sessions if (s.buyable or not buyable_only)]
    if language:
        pool = [s for s in pool if s.language and language in s.language]
    if dimension:
        pool = [s for s in pool if s.dimension and dimension.upper() in s.dimension.upper()]

    if not session_priorities:
        return pool

    wanted = [p.strip() for p in session_priorities if p and p.strip()]
    matched: Dict[str, SessionInfo] = {}
    for s in pool:
        for want in wanted:
            if _time_match(s.start_time, want) and want not in matched:
                matched[want] = s
    return [matched[w] for w in wanted if w in matched]


def _time_match(actual: str, want: str) -> bool:
    """时间匹配：'19:00' == '19:00'，也兼容 '19:00:00' / '9:00'。"""
    def norm(t: str) -> Optional[str]:
        m = re.match(r"^(\d{1,2}):(\d{2})", t.strip())
        if not m:
            return None
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    a, b = norm(actual), norm(want)
    return bool(a and b and a == b)


# ----------------------------------------------------------------------
# 摘要（写日志用）
# ----------------------------------------------------------------------
def summarize(sessions: List[SessionInfo]) -> str:
    """生成一行一场的可读摘要，供任务日志输出。"""
    if not sessions:
        return "（无场次）"
    lines = []
    for s in sessions:
        price = f"¥{s.price_now:g}" if s.price_now is not None else "-"
        flow = s.flow_status or "-"
        status = "可购" if s.buyable else "停售"
        lines.append(
            f"{s.start_time}-{s.end_time or '--'} {s.language} {s.dimension} "
            f"{s.hall} | {price} | {flow} | {status} | scheduleId={s.schedule_id or '?'}"
        )
    return "\n".join(lines)
