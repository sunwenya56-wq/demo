# -*- coding: utf-8 -*-
"""命令行入口：python run.py --case data/sample_01/case.json"""
import argparse
import json
import sys
from pathlib import Path

import doccheck


def main() -> int:
    parser = argparse.ArgumentParser(description="进口报关三单一致性审核")
    parser.add_argument("--case", default="data/sample_01/case.json", help="案例 JSON 路径")
    parser.add_argument("--config", default="config/rules.json", help="规则配置路径")
    parser.add_argument("--out", default="output", help="报告输出目录")
    args = parser.parse_args()

    case_path = Path(args.case)
    if not case_path.exists():
        print(f"[错误] 找不到案例文件：{case_path}")
        return 1

    try:
        case = json.loads(case_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[错误] 案例文件 JSON 解析失败：{exc}")
        return 1

    config = doccheck.load_config(Path(args.config))
    result = doccheck.run_checks(case, config)

    out_dir = Path(args.out)
    html_path = doccheck.write_html_report(result, case, out_dir)
    detail_path = doccheck.write_excel_report(result, case, out_dir)

    doccheck.print_summary(result)
    print(f"HTML 报告：{html_path.resolve()}")
    print(f"明细报告：{detail_path.resolve()}")

    # 有错误时返回非零退出码，方便接入自动化流程
    return 2 if result.counts[doccheck.LEVEL_ERROR] else 0


if __name__ == "__main__":
    sys.exit(main())
