---
name: import-doc-checker
description: 进口报关三单一致性审核。输入商业发票、装箱单、提单的结构化数据，自动核对单号、合同号、收发货人、日期、件数、毛重净重、金额、数量、品名、HS 编码、计量单位共 20 项，输出 HTML 看板与 Excel 差异报告。当用户需要审核进口单证、核对三单一致、检查报关资料、生成单证审核报告时使用。
---

# 进口报关三单一致性审核

## 什么时候用

- 报关单证部收到国外供应商的发票（Invoice）、装箱单（Packing List）和货代给的提单（B/L），需要申报前做一致性核对
- 客户改单后需要复核改单是否到位
- 需要给主管/客户出一份可留档的审核报告

## 输入

一个 JSON 案例文件，包含 `invoice`、`packing_list`、`bill_of_lading` 三块数据，字段定义见 `data/sample_01/case.json`。

关键字段：

| 单据 | 字段 | 说明 |
|---|---|---|
| invoice | no / contract_no / date / shipper / consignee / total_amount / total_quantity | 表头信息 |
| invoice.items[] | item_no / description / spec / hs_code / quantity / unit / amount | 商品明细 |
| packing_list | no / invoice_ref / total_packages / gross_weight_kg / net_weight_kg | 表头信息 |
| packing_list.items[] | item_no / description / packages / unit | 装箱明细 |
| bill_of_lading | no / invoice_ref / contract_no / on_board_date / packages / gross_weight_kg / description | 提单信息 |

## 怎么执行

```bash
python run.py --case data/sample_01/case.json
```

可选参数：

- `--config config/rules.json`：调整相似度阈值、重量容差、HS 归类规则
- `--out output`：报告输出目录

退出码：存在错误时返回 `2`，全部通过返回 `0`，可直接接入批处理或定时任务。

## 输出

- `output/核对报告_<案例号>.html`：单文件看板，浏览器直接打开，红黄绿分级
- `output/核对报告_<案例号>.xlsx`：汇总页 + 明细页，可直接发给主管留档
- 控制台摘要：问题清单

## 判定规则

| 检查类型 | 规则 |
|---|---|
| 单号 / 合同号 | 去空格、转大写后必须完全一致 |
| 金额 / 数量 / 件数 | 绝对误差 ≤ 容差（默认 0.01）判通过 |
| 毛重 | 偏差 ≤ 0.5% 通过，0.5%-2% 预警，> 2% 判错 |
| 品名 / 主体名称 | 相似度 ≥ 95% 通过，80%-95% 需人工确认，< 80% 判错 |
| HS 编码 | 必须 10 位数字；校验品名关键词对应的前 4 位；校验包装形态对应的前 6 位 |
| 单位 | 发票与装箱单单位不一致时提示按包装规格换算 |

## 处理审核结果

1. 先看 ERROR：这些是必须改单后重新申报的硬性问题（金额、件数、HS 编码）
2. 再看 WARN：需要报关员结合业务判断，比如重量偏差可能来自托盘重量
3. 把 ERROR 与 WARN 整理成一段中文说明发给单证员，不要直接把报告原文转发

## 设计边界

- 数值、单号、HS 编码全部由确定性规则比对，**不让模型参与计算或判断大小**
- 模型只负责把规则结果写成业务语言，避免"一本正经地算错数"
- 提单通常是货物统称，不做逐项品名核对，只提示

## 已知限制

- 当前输入为结构化 JSON；PDF / 扫描件需要先经 OCR 或人工录入转成该结构
- HS 归类规则只覆盖葡萄酒、威士忌、橄榄油等常见品类，新品类需在 `config/rules.json` 中补充
- 不做原产地证、卫生证书等随附单证的核对
