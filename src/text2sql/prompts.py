"""Prompt contracts for the three LLM nodes (text2sql_V1.md sections 5.3, 5.5, 5.8).

The prompts carry *semantics*, never physical schema invented by the model:
``information_extraction`` receives business vocabulary only (no table or column
names), while ``generate_sql`` / ``regenerate_sql`` receive exactly the catalog
entries that ``schema_linking`` selected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

# Business vocabulary hints for information_extraction. These are business
# concepts, not physical schema: the extraction node must not pick tables.
BUSINESS_VOCABULARY = (
    "维度：渠道、会员等级、性别、年龄、城市等级、注册时间、行为阶段、活动、cohort 日期",
    "用户指标：用户数、行为次数、活跃天数、访问次数、点击次数、加购次数、支付次数、收藏次数",
    "交易指标：有效订单数、下单天数、原始金额、优惠金额、净消费金额、复购用户数",
    "活动指标：曝光次数、点击次数、转化次数、点击率、曝光转化率、点击后转化率",
    "漏斗指标：阶段行为次数、阶段去重用户数、阶段去重会话数",
    "留存指标：cohort 初始用户数、次日留存率、7 日留存率、30 日留存率",
)


@dataclass
class Prompt:
    """A system/user prompt plus real (never stringified) chat history."""

    system: str
    user: str
    history: list[Any] = field(default_factory=list)
    json_mode: bool = False


EXTRACTION_SYSTEM = """你是电商数据分析助手的查询意图解析器。
把用户的自然语言问题转换成结构化查询意图，只输出 JSON，不要输出任何解释或 Markdown。

输出 JSON 结构：
{
  "rewrite_question": "去掉歧义后的完整问题（中文，保留全部业务限定条件）",
  "keywords": ["业务关键词"],
  "dimensions": ["分组或展示维度"],
  "metrics": ["要统计的指标"],
  "filters": [{"field_hint": "过滤字段的语义", "operator": "eq|neq|gt|gte|lt|lte|contains|in|between", "value": "值或[值1,值2]"}],
  "start_time": "YYYY-MM-DD 或 null",
  "end_time": "YYYY-MM-DD 或 null",
  "timezone": "Asia/Shanghai 或 null"
}

规则：
1. rewrite_question 必须是完整问句，不能只返回关键词。
2. dimensions 只填写分组/展示维度；metrics 只填写要统计的业务指标，并且必须使用下面给出的业务词汇表中的标准叫法。
3. filters 只表达语义过滤（例如会员等级等于黄金、活跃天数大于 5、是否复购用户等于 1），禁止写 SQL 片段、字段名或表名。
   “复购用户”“复购”“重复购买”必须输出为 filters：[{"field_hint": "是否复购用户", "operator": "eq", "value": 1}]。
4. between 的 value 是长度为 2 的数组，in 的 value 是非空数组，其余操作符用单值。
5. 时间过滤只有在问题中出现明确或可推断的时间范围时才填写，无法确定时填 null；不要把“最近”“近 30 天”等相对时间编造成绝对日期，除非问题给出了可计算的基准。
6. 无法映射到业务词汇表的词（例如商品名称、品牌、类目）只能放进 keywords，不要放进 metrics 或 dimensions。
7. 如果问题要求“列出用户/用户明细/用户列表/哪些用户”这类明细，把“用户ID”放进 dimensions；
   如果同时没有明确指标，metrics 可以为空。
8. keywords、dimensions、metrics 内部去重。
9. 如果用户要求写入、修改或删除数据（插入/更新/删除/清空/建表/改表等），把 "unsafe_request" 设为 true。

业务词汇表（只能从这里选择 dimensions 和 metrics 的标准叫法）：
{{VOCABULARY}}
"""


def extraction_prompt(question: str, history: Sequence[Any]) -> Prompt:
    user = f"用户问题：{question}\n\n请输出 JSON。"
    return Prompt(
        system=EXTRACTION_SYSTEM.replace("{{VOCABULARY}}", "\n".join(BUSINESS_VOCABULARY)),
        user=user,
        history=list(history),
        json_mode=True,
    )


GENERATION_SYSTEM = """你是严谨的 MySQL 8.0 数据分析 SQL 生成器。
根据查询意图和给定的业务表字段生成**一条**只读 SQL。

硬性约束：
1. 只输出一条 SQL 语句，顶层必须是 SELECT 或以 WITH 开头的 CTE 且最终执行 SELECT；不要输出分号、注释或 Markdown。
2. 只能使用下面列出的表和字段，禁止使用任何未列出的表或字段，禁止使用 SELECT *。
3. 必须带 LIMIT，默认 LIMIT {{MAX_ROWS}}；问题中出现“最高/最多/前 N 个/TOP N”时使用对应的 N（不超过 {{MAX_ROWS}}）。
4. 使用 GROUP BY 时，所有非聚合字段都必须出现在 GROUP BY 中。
5. 用户粒度的汇总表（一行一个用户）之间只能通过 user_id 关联；不要重复累加明细。
6. 比率指标（点击率、转化率、留存率）单行可以直接取用；跨行汇总时必须用分子之和/分母之和重新计算，禁止对多行比率直接取平均。
7. “点击后的转化率”的分母是点击次数（clicked_count），不是曝光次数，禁止直接使用 conversion_rate。
8. 漏斗用户数必须使用 unique_users，不能用 event_count 代替。
9. 只使用 MySQL 8.0 兼容函数，禁止 DATE_TRUNC、APPROX_COUNT_DISTINCT 等其它方言函数。
10. 不要编造维度值；需要看全部取值时直接分组输出。

输出：只输出 SQL 本身。"""


def generation_prompt(
    *,
    question: str,
    rewrite_question: str,
    entities_json: str,
    schema_block: str,
    metric_guidance: str,
    max_rows: int,
    previous_errors: str | None = None,
    previous_sql: str | None = None,
) -> Prompt:
    parts = [
        f"原始问题：{question}",
        f"规范化问题：{rewrite_question}",
        f"结构化意图：{entities_json}",
        "",
        "可用业务表与字段：",
        schema_block,
        "",
        "指标口径：",
        metric_guidance,
    ]
    if previous_sql:
        parts += ["", "上一条被拒绝或执行失败的 SQL：", previous_sql]
    if previous_errors:
        parts += ["", "必须修正的错误（不得忽略，也不得重复上一条 SQL 的错误）：", previous_errors]
    parts += ["", "请输出一条 MySQL 8.0 只读 SQL。"]
    return Prompt(
        system=GENERATION_SYSTEM.replace("{{MAX_ROWS}}", str(max_rows)),
        user="\n".join(parts),
    )


REGENERATION_SYSTEM = GENERATION_SYSTEM + """

这是一次修正任务：
- 逐条修正给出的错误，保留原始业务语义（维度、指标、过滤、排序、行数限制）。
- 不得通过换表绕过限制：仍然只能使用下面列出的表和字段。
- 不得忽略安全校验发现的错误，也不得输出与上一条完全相同的 SQL。"""


def regeneration_prompt(
    *,
    question: str,
    rewrite_question: str,
    entities_json: str,
    schema_block: str,
    metric_guidance: str,
    max_rows: int,
    previous_sql: str,
    previous_errors: str,
) -> Prompt:
    prompt = generation_prompt(
        question=question,
        rewrite_question=rewrite_question,
        entities_json=entities_json,
        schema_block=schema_block,
        metric_guidance=metric_guidance,
        max_rows=max_rows,
        previous_errors=previous_errors,
        previous_sql=previous_sql,
    )
    prompt.system = REGENERATION_SYSTEM.replace("{{MAX_ROWS}}", str(max_rows))
    return prompt


def entities_json(entities: dict[str, Any]) -> str:
    return json.dumps(entities, ensure_ascii=False)
