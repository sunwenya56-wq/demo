# -*- coding: utf-8 -*-
"""进口报关三单一致性审核核心逻辑。

设计原则（对应岗位要求里的"知道 AI 什么时候会胡说"）：
1. 单号、金额、重量、件数等可以精确校验的字段，全部走确定性规则，绝不交给大模型；
2. 品名、收发货人这类天然存在表述差异的字段，用编辑距离相似度 + 阈值分级；
3. 大模型只负责把规则结果翻译成业务语言，不参与任何数值计算。
"""
from __future__ import annotations

import csv
import difflib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LEVEL_PASS = "PASS"
LEVEL_INFO = "INFO"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"

LEVEL_LABEL = {LEVEL_PASS: "通过", LEVEL_INFO: "提示", LEVEL_WARN: "预警", LEVEL_ERROR: "错误"}
LEVEL_RANK = {LEVEL_PASS: 0, LEVEL_INFO: 1, LEVEL_WARN: 2, LEVEL_ERROR: 3}

DEFAULT_CONFIG: Dict[str, Any] = {
    "hs_code_length": 10,
    "amount_tolerance": 0.01,
    "quantity_tolerance": 0.01,
    "weight_tolerance_pct": {"warn": 0.5, "error": 2.0},
    "text_similarity": {"pass": 0.95, "warn": 0.80},
    # 品名关键词 -> HS 编码前 4 位
    "hs_keyword_prefix": {
        "wine": "2204",
        "葡萄酒": "2204",
        "whisky": "2208",
        "威士忌": "2208",
        "olive oil": "1509",
        "橄榄油": "1509",
    },
    # 包装形态 -> HS 编码前 6 位（葡萄酒细分归类）
    "hs_packaging_rules": [
        {"keywords": ["sparkling", "起泡"], "prefix": "220410", "note": "起泡葡萄酒"},
        {"keywords": ["750ml"], "prefix": "220421", "note": "≤2L 容器装静态葡萄酒"},
    ],
}


@dataclass
class CheckItem:
    """一条核对结果。"""

    name: str
    invoice: str = ""
    packing_list: str = ""
    bill_of_lading: str = ""
    level: str = LEVEL_PASS
    message: str = ""

    def to_row(self) -> List[str]:
        return [
            self.name,
            self.invoice,
            self.packing_list,
            self.bill_of_lading,
            LEVEL_LABEL[self.level],
            self.message,
        ]


@dataclass
class AuditResult:
    case_id: str
    generated_at: str
    items: List[CheckItem] = field(default_factory=list)

    @property
    def counts(self) -> Dict[str, int]:
        counts = {LEVEL_PASS: 0, LEVEL_INFO: 0, LEVEL_WARN: 0, LEVEL_ERROR: 0}
        for item in self.items:
            counts[item.level] = counts.get(item.level, 0) + 1
        return counts

    @property
    def conclusion(self) -> str:
        counts = self.counts
        if counts[LEVEL_ERROR]:
            return "不通过"
        if counts[LEVEL_WARN]:
            return "需复核"
        return "通过"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def load_config(path: Optional[Path]) -> Dict[str, Any]:
    """读取规则配置，缺省项用 DEFAULT_CONFIG 补齐。"""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and Path(path).exists():
        user_config = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in user_config.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def norm_code(value: Any) -> str:
    """单号类字段：只去掉空白并转大写，保留连字符。"""
    return re.sub(r"\s+", "", str(value or "")).upper()


def norm_text(value: Any) -> str:
    """文本类字段：全角转半角、小写、标点压成空格。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: Any, b: Any) -> float:
    left, right = norm_text(a), norm_text(b)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def truncate(text: Any, length: int = 60) -> str:
    raw = str(text or "").strip()
    return raw if len(raw) <= length else raw[: length - 1] + "…"


def parse_date(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def sorted_item_keys(mapping: Dict[str, Any]) -> List[str]:
    def sort_key(key: str):
        try:
            return (0, int(key))
        except (TypeError, ValueError):
            return (1, str(key))

    return sorted(mapping.keys(), key=sort_key)


# --------------------------------------------------------------------------- #
# 比对函数：统一返回 (级别, 说明)
# --------------------------------------------------------------------------- #
def compare_codes(values: Iterable[Any]) -> Tuple[str, str]:
    vals = [norm_code(v) for v in values if norm_code(v)]
    if len(vals) < 2:
        return LEVEL_INFO, "有效单据不足两份，已跳过比对"
    if len(set(vals)) == 1:
        return LEVEL_PASS, "单据间一致"
    return LEVEL_ERROR, f"存在 {len(set(vals))} 种不同取值：" + " / ".join(sorted(set(vals)))


def compare_texts(values: Iterable[Any], config: Dict[str, Any]) -> Tuple[str, str]:
    vals = [str(v).strip() for v in values if str(v or "").strip()]
    if len(vals) < 2:
        return LEVEL_INFO, "有效单据不足两份，已跳过比对"
    base = vals[0]
    worst = min(similarity(base, other) for other in vals[1:])
    thresholds = config["text_similarity"]
    if worst >= thresholds["pass"]:
        return LEVEL_PASS, f"一致（相似度 {worst:.1%}）"
    if worst >= thresholds["warn"]:
        return LEVEL_WARN, f"相似度 {worst:.1%}，低于 {thresholds['pass']:.0%} 阈值，请确认是否为拼写/备注混入"
    return LEVEL_ERROR, f"相似度 {worst:.1%}，疑似不同主体或品名，请人工核对"


def compare_weight(
    source: Optional[float],
    target: Optional[float],
    config: Dict[str, Any],
    label: str = "重量",
) -> Tuple[str, str]:
    if source is None or target is None:
        return LEVEL_INFO, f"缺少{label}数据，已跳过"
    base = max(abs(source), abs(target)) or 1.0
    diff_pct = abs(source - target) / base * 100
    tolerance = config["weight_tolerance_pct"]
    if diff_pct <= tolerance["warn"]:
        return LEVEL_PASS, f"偏差 {diff_pct:.2f}%，在 {tolerance['warn']}% 容差内"
    if diff_pct <= tolerance["error"]:
        return LEVEL_WARN, f"偏差 {diff_pct:.2f}%，超过 {tolerance['warn']}% 预警线，请确认是否含托盘/包装重量"
    return LEVEL_ERROR, f"偏差 {diff_pct:.2f}%，超过 {tolerance['error']}% 容差，建议核实后改单"


def check_hs_code(item: Dict[str, Any], config: Dict[str, Any]) -> Tuple[str, str]:
    """校验 HS 编码格式、品名归类、包装形态归类。"""
    hs = re.sub(r"\s+", "", str(item.get("hs_code") or ""))
    desc = f"{item.get('description') or ''} {item.get('spec') or ''}"
    problems: List[str] = []
    expected_len = int(config["hs_code_length"])

    if not hs:
        return LEVEL_ERROR, "HS 编码为空"
    if not re.fullmatch(r"\d+", hs) or len(hs) != expected_len:
        problems.append(f"HS 编码应为 {expected_len} 位数字，当前为 {len(hs)} 位：{hs}")
        return LEVEL_ERROR, "；".join(problems)

    normalized_desc = norm_text(desc)
    for keyword, prefix in config["hs_keyword_prefix"].items():
        if norm_text(keyword) in normalized_desc and not hs.startswith(prefix):
            problems.append(f"品名含“{keyword}”，HS 应以 {prefix} 开头，当前为 {hs}")
            break

    for rule in config["hs_packaging_rules"]:
        if any(norm_text(k) in normalized_desc for k in rule["keywords"]) and not hs.startswith(rule["prefix"]):
            problems.append(f"按“{rule['note']}”应归入 {rule['prefix']}，当前为 {hs}")
            break

    if problems:
        return LEVEL_ERROR, "；".join(problems)
    return LEVEL_PASS, "格式与归类校验通过"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_checks(case: Dict[str, Any], config: Dict[str, Any]) -> AuditResult:
    invoice = case.get("invoice") or {}
    packing = case.get("packing_list") or {}
    bill = case.get("bill_of_lading") or {}
    result = AuditResult(
        case_id=str(case.get("case_id") or "UNKNOWN"),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    def add(name: str, level: str, message: str, inv: Any = "", pack: Any = "", bl: Any = "") -> None:
        result.items.append(
            CheckItem(
                name=name,
                invoice=str(inv or ""),
                packing_list=str(pack or ""),
                bill_of_lading=str(bl or ""),
                level=level,
                message=message,
            )
        )

    # 1. 单号 / 合同号
    level, message = compare_codes(
        [invoice.get("no"), packing.get("invoice_ref"), bill.get("invoice_ref")]
    )
    add("发票号一致性", level, message, invoice.get("no"), packing.get("invoice_ref"), bill.get("invoice_ref"))

    level, message = compare_codes([invoice.get("contract_no"), bill.get("contract_no")])
    add("合同号一致性", level, message, invoice.get("contract_no"), packing.get("contract_no"), bill.get("contract_no"))

    # 2. 交易主体
    level, message = compare_texts([invoice.get("shipper"), bill.get("shipper")], config)
    add("发货人一致性", level, message, invoice.get("shipper"), "", bill.get("shipper"))

    level, message = compare_texts([invoice.get("consignee"), bill.get("consignee")], config)
    add("收货人一致性", level, message, invoice.get("consignee"), "", bill.get("consignee"))

    # 3. 日期逻辑
    inv_date = parse_date(invoice.get("date"))
    bl_date = parse_date(bill.get("on_board_date"))
    if inv_date and bl_date:
        if inv_date <= bl_date:
            level, message = LEVEL_PASS, "发票日期不晚于装船日期"
        else:
            level, message = LEVEL_WARN, "发票日期晚于提单装船日期，请确认是否为补开发票"
    else:
        level, message = LEVEL_INFO, "缺少发票日期或装船日期，已跳过"
    add("日期逻辑", level, message, invoice.get("date"), "", bill.get("on_board_date"))

    # 4. 件数 / 重量
    pack_total = to_float(packing.get("total_packages"))
    bl_packages = to_float(bill.get("packages"))
    if pack_total is None or bl_packages is None:
        level, message = LEVEL_INFO, "缺少件数数据，已跳过"
    elif abs(pack_total - bl_packages) <= float(config["quantity_tolerance"]):
        level, message = LEVEL_PASS, f"件数一致（{fmt_num(pack_total, 0)} 件）"
    else:
        level, message = LEVEL_ERROR, f"装箱单 {fmt_num(pack_total, 0)} 件 ≠ 提单 {fmt_num(bl_packages, 0)} 件，存在短装或提单笔误"
    add("件数一致性", level, message, "", packing.get("total_packages"), bill.get("packages"))

    pack_gross = to_float(packing.get("gross_weight_kg"))
    bl_gross = to_float(bill.get("gross_weight_kg"))
    level, message = compare_weight(pack_gross, bl_gross, config, "毛重")
    add("毛重偏差", level, message, "", packing.get("gross_weight_kg"), bill.get("gross_weight_kg"))

    pack_net = to_float(packing.get("net_weight_kg"))
    if pack_gross is not None and pack_net is not None:
        if pack_net < pack_gross:
            level, message = LEVEL_PASS, f"净重 {fmt_num(pack_net)} kg < 毛重 {fmt_num(pack_gross)} kg"
        else:
            level, message = LEVEL_ERROR, "净重不小于毛重，明显不合理"
    else:
        level, message = LEVEL_INFO, "缺少毛重或净重，已跳过"
    add("净重合理性", level, message, "", f"净重 {packing.get('net_weight_kg')} / 毛重 {packing.get('gross_weight_kg')}", "")

    # 5. 金额 / 数量
    inv_items_raw = invoice.get("items") or []
    pack_items_raw = packing.get("items") or []
    inv_items = {str(it.get("item_no", idx + 1)): it for idx, it in enumerate(inv_items_raw)}
    pack_items = {str(it.get("item_no", idx + 1)): it for idx, it in enumerate(pack_items_raw)}

    calc_amount = sum(to_float(it.get("amount")) or 0.0 for it in inv_items.values())
    declared_amount = to_float(invoice.get("total_amount"))
    if declared_amount is None:
        level, message = LEVEL_INFO, "发票未填写总额，已跳过"
    elif abs(calc_amount - declared_amount) <= float(config["amount_tolerance"]):
        level, message = LEVEL_PASS, f"明细合计 {fmt_num(calc_amount)} = 发票总额"
    else:
        level, message = LEVEL_ERROR, (
            f"明细合计 {fmt_num(calc_amount)} ≠ 发票总额 {fmt_num(declared_amount)}，"
            f"差额 {fmt_num(calc_amount - declared_amount)}"
        )
    add("发票金额合计", level, message, f"声明 {fmt_num(declared_amount)} / 明细 {fmt_num(calc_amount)}", "", "")

    calc_qty = sum(to_float(it.get("quantity")) or 0.0 for it in inv_items.values())
    declared_qty = to_float(invoice.get("total_quantity"))
    if declared_qty is None:
        level, message = LEVEL_INFO, "发票未填写总数量，已跳过"
    elif abs(calc_qty - declared_qty) <= float(config["quantity_tolerance"]):
        level, message = LEVEL_PASS, f"明细数量合计 {fmt_num(calc_qty, 0)} = 声明总数量"
    else:
        level, message = LEVEL_ERROR, f"明细数量合计 {fmt_num(calc_qty, 0)} ≠ 声明 {fmt_num(declared_qty, 0)}"
    add("发票数量合计", level, message, f"声明 {fmt_num(declared_qty, 0)} / 明细 {fmt_num(calc_qty, 0)}", "", "")

    packed_total = sum(to_float(it.get("packages")) or 0.0 for it in pack_items.values())
    if pack_total is not None and pack_items:
        if abs(packed_total - pack_total) <= float(config["quantity_tolerance"]):
            level, message = LEVEL_PASS, f"明细箱数合计 {fmt_num(packed_total, 0)} = 声明总箱数"
        else:
            level, message = LEVEL_ERROR, f"明细箱数合计 {fmt_num(packed_total, 0)} ≠ 声明 {fmt_num(pack_total, 0)}"
        add("装箱单箱数合计", level, message, "", f"声明 {fmt_num(pack_total, 0)} / 明细 {fmt_num(packed_total, 0)}", "")

    if inv_items and pack_items:
        if len(inv_items) == len(pack_items):
            level, message = LEVEL_PASS, f"单据项数一致（{len(inv_items)} 项）"
        else:
            level, message = LEVEL_WARN, f"发票 {len(inv_items)} 项，装箱单 {len(pack_items)} 项，请确认是否漏项"
        add("单据项数一致", level, message, f"{len(inv_items)} 项", f"{len(pack_items)} 项", "")

    # 6. 逐项核对：品名 + HS 编码
    for key in sorted_item_keys(inv_items):
        inv_item = inv_items[key]
        pack_item = pack_items.get(key)
        desc = inv_item.get("description")
        if pack_item is None:
            add(f"品名一致性（第 {key} 项）", LEVEL_WARN, "装箱单缺少同序号商品，请确认是否漏项", desc, "缺失", "")
        else:
            level, message = compare_texts([desc, pack_item.get("description")], config)
            add(f"品名一致性（第 {key} 项）", level, message, desc, pack_item.get("description"), "")

        level, message = check_hs_code(inv_item, config)
        add(f"HS 编码校验（第 {key} 项）", level, message, inv_item.get("hs_code"), "", "")

    # 7. 计量单位
    inv_units = {str(it.get("unit") or "").upper() for it in inv_items.values() if it.get("unit")}
    pack_units = {str(it.get("unit") or "").upper() for it in pack_items.values() if it.get("unit")}
    if inv_units and pack_units and not (inv_units & pack_units):
        level, message = LEVEL_WARN, (
            f"发票单位为 {'/'.join(sorted(inv_units))}，装箱单为 {'/'.join(sorted(pack_units))}，"
            "单位不同，需按包装规格（如 12 支/箱）换算后再核对数量"
        )
        add("计量单位一致性", level, message, "/".join(sorted(inv_units)), "/".join(sorted(pack_units)), "")
    elif inv_units and pack_units:
        add("计量单位一致性", LEVEL_PASS, "计量单位一致", "/".join(sorted(inv_units)), "/".join(sorted(pack_units)), "")

    # 8. 提单货物描述
    bl_items = bill.get("items") or []
    if not bl_items:
        add(
            "提单货物描述",
            LEVEL_INFO,
            "提单仅标注货物统称，未拆分具体品名，按行业惯例不做逐项核对",
            "",
            "",
            truncate(bill.get("description")),
        )

    return result


# --------------------------------------------------------------------------- #
# 报告输出
# --------------------------------------------------------------------------- #
_HTML_STYLE = """
:root{--bg:#f5f7fa;--card:#fff;--line:#e3e8ef;--text:#1f2d3d;--muted:#6b7a90;
--pass:#2f855a;--pass-bg:#e9f6ef;--warn:#b7791f;--warn-bg:#fdf6e7;
--error:#c0392b;--error-bg:#fdeceb;--info:#2b6cb0;--info-bg:#eaf2fb;}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:var(--bg);color:var(--text);
font-family:"Microsoft YaHei","PingFang SC",-apple-system,Segoe UI,sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:24px;font-weight:700;margin-top:2px}
.banner{border-radius:10px;padding:14px 18px;margin-bottom:18px;font-weight:600;
display:flex;justify-content:space-between;align-items:center}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse}
th{background:#f0f4f9;text-align:left;font-size:12px;color:#43536b;padding:10px 12px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:13px}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap}
.PASS{color:var(--pass);background:var(--pass-bg)}
.WARN{color:var(--warn);background:var(--warn-bg)}
.ERROR{color:var(--error);background:var(--error-bg)}
.INFO{color:var(--info);background:var(--info-bg)}
.note{color:var(--muted);font-size:12px;margin-top:14px;line-height:1.8}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px 24px;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin-bottom:16px;font-size:13px}
.meta b{color:var(--muted);font-weight:400}
"""


def write_html_report(result: AuditResult, case: Dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"核对报告_{result.case_id}.html"
    counts = result.counts
    conclusion = result.conclusion
    banner_class = LEVEL_ERROR if conclusion == "不通过" else (LEVEL_WARN if conclusion == "需复核" else LEVEL_PASS)
    banner_text = {
        "不通过": f"存在 {counts[LEVEL_ERROR]} 处必须修改的问题，建议改单后再申报",
        "需复核": f"存在 {counts[LEVEL_WARN]} 处预警，需报关员人工确认",
        "通过": "全部核对项通过，可进入申报流程",
    }[conclusion]

    invoice = case.get("invoice") or {}
    packing = case.get("packing_list") or {}
    bill = case.get("bill_of_lading") or {}

    rows_html = []
    for idx, item in enumerate(result.items, 1):
        rows_html.append(
            "<tr>"
            f"<td>{idx}</td>"
            f"<td>{html.escape(item.name)}</td>"
            f"<td>{html.escape(item.invoice) or '-'}</td>"
            f"<td>{html.escape(item.packing_list) or '-'}</td>"
            f"<td>{html.escape(item.bill_of_lading) or '-'}</td>"
            f'<td><span class="tag {item.level}">{LEVEL_LABEL[item.level]}</span></td>'
            f"<td>{html.escape(item.message)}</td>"
            "</tr>"
        )

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三单一致性审核报告 - {html.escape(result.case_id)}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>进口报关三单一致性审核报告</h1>
  <div class="sub">规则引擎自动核对 · 生成时间 {html.escape(result.generated_at)}</div>

  <div class="meta">
    <div><b>案例编号：</b>{html.escape(result.case_id)}</div>
    <div><b>商业发票：</b>{html.escape(str(invoice.get('no') or '-'))}</div>
    <div><b>装箱单：</b>{html.escape(str(packing.get('no') or '-'))}</div>
    <div><b>提单号：</b>{html.escape(str(bill.get('no') or '-'))}</div>
    <div><b>启运港：</b>{html.escape(str(bill.get('port_of_loading') or '-'))}</div>
    <div><b>目的港：</b>{html.escape(str(bill.get('port_of_discharge') or '-'))}</div>
  </div>

  <div class="cards">
    <div class="card"><div class="k">核对项总数</div><div class="v">{len(result.items)}</div></div>
    <div class="card"><div class="k">通过</div><div class="v" style="color:var(--pass)">{counts[LEVEL_PASS]}</div></div>
    <div class="card"><div class="k">预警</div><div class="v" style="color:var(--warn)">{counts[LEVEL_WARN]}</div></div>
    <div class="card"><div class="k">错误</div><div class="v" style="color:var(--error)">{counts[LEVEL_ERROR]}</div></div>
  </div>

  <div class="banner {banner_class}">
    <span>审核结论：{conclusion}</span>
    <span style="font-weight:400;font-size:13px">{html.escape(banner_text)}</span>
  </div>

  <div class="panel">
    <table>
      <thead>
        <tr><th>#</th><th>核对项</th><th>商业发票</th><th>装箱单</th><th>提单</th><th>结论</th><th>说明</th></tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>
  </div>

  <div class="note">
    判定规则：单号、金额、件数必须完全一致；重量偏差 &gt; 0.5% 预警、&gt; 2% 判错；品名相似度 ≥ 95% 视为一致，80%-95% 需人工确认。<br>
    本报告由确定性规则引擎生成，大模型不参与任何数值比对。审核结果仅作辅助，最终申报以报关员复核及海关认定为准。
  </div>
</div>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
    return path


def write_excel_report(result: AuditResult, case: Dict[str, Any], out_dir: Path) -> Path:
    """优先导出 xlsx；本机没有 openpyxl 时自动降级为 UTF-8 BOM 的 CSV。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"核对报告_{result.case_id}"
    headers = ["序号", "核对项", "商业发票", "装箱单", "提单", "结论", "说明"]

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        path = out_dir / f"{stem}_明细.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for idx, item in enumerate(result.items, 1):
                writer.writerow([idx] + item.to_row())
        return path

    colors = {
        LEVEL_PASS: ("E9F6EF", "2F855A"),
        LEVEL_INFO: ("EAF2FB", "2B6CB0"),
        LEVEL_WARN: ("FDF6E7", "B7791F"),
        LEVEL_ERROR: ("FDECEB", "C0392B"),
    }
    thin = Side(style="thin", color="E3E8EF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    workbook = Workbook()
    summary = workbook.active
    summary.title = "审核汇总"

    summary.merge_cells("A1:B1")
    title_cell = summary["A1"]
    title_cell.value = "进口报关三单一致性审核报告"
    title_cell.font = Font(size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill("solid", fgColor="1F4E79")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    summary.row_dimensions[1].height = 30

    counts = result.counts
    summary_rows = [
        ("案例编号", result.case_id),
        ("审核时间", result.generated_at),
        ("核对项总数", len(result.items)),
        ("通过", counts[LEVEL_PASS]),
        ("提示", counts[LEVEL_INFO]),
        ("预警", counts[LEVEL_WARN]),
        ("错误", counts[LEVEL_ERROR]),
        ("审核结论", result.conclusion),
    ]
    for offset, (key, value) in enumerate(summary_rows, 3):
        summary.cell(row=offset, column=1, value=key).font = Font(bold=True, color="43536B")
        summary.cell(row=offset, column=2, value=value)
        summary.cell(row=offset, column=1).border = border
        summary.cell(row=offset, column=2).border = border

    conclusion_cell = summary.cell(row=10, column=2)
    fill, color = colors[LEVEL_ERROR if result.conclusion == "不通过" else (LEVEL_WARN if result.conclusion == "需复核" else LEVEL_PASS)]
    conclusion_cell.fill = PatternFill("solid", fgColor=fill)
    conclusion_cell.font = Font(bold=True, color=color)
    summary.column_dimensions["A"].width = 18
    summary.column_dimensions["B"].width = 40

    detail = workbook.create_sheet("核对明细")
    detail.append(headers)
    for cell in detail[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    detail.row_dimensions[1].height = 22

    for idx, item in enumerate(result.items, 1):
        detail.append([idx] + item.to_row())
        fill, color = colors[item.level]
        for column in range(1, len(headers) + 1):
            cell = detail.cell(row=idx + 1, column=column)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        detail.cell(row=idx + 1, column=6).fill = PatternFill("solid", fgColor=fill)
        detail.cell(row=idx + 1, column=6).font = Font(bold=True, color=color)

    widths = [6, 26, 32, 30, 30, 10, 52]
    for position, width in enumerate(widths, 1):
        detail.column_dimensions[get_column_letter(position)].width = width
    detail.freeze_panes = "A2"

    path = out_dir / f"{stem}.xlsx"
    workbook.save(path)
    return path


def print_summary(result: AuditResult) -> None:
    counts = result.counts
    print("=" * 56)
    print(f"案例编号：{result.case_id}")
    print(
        f"核对项：{len(result.items)}  通过：{counts[LEVEL_PASS]}  "
        f"提示：{counts[LEVEL_INFO]}  预警：{counts[LEVEL_WARN]}  错误：{counts[LEVEL_ERROR]}"
    )
    print(f"审核结论：{result.conclusion}")
    print("-" * 56)

    problems = [item for item in result.items if item.level in (LEVEL_ERROR, LEVEL_WARN)]
    if not problems:
        print("未发现需要处理的差异。")
    for item in problems:
        tag = "错误" if item.level == LEVEL_ERROR else "预警"
        print(f"[{tag}] {item.name}：{item.message}")
    print("=" * 56)
