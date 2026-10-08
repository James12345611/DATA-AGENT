# Text2SQL V1 验收报告

- 生成时间：2026-10-05 15:39:16
- 模型模式：真实 LLM
- LLM：deepseek:deepseek-chat
- 数据库：text2sql_readonly@127.0.0.1:3306/ecommerce_text2sql
- 策略：SQL_MAX_ROWS=100、SQL_TIMEOUT_SECONDS=30、SQL_MAX_RETRIES=2、CATALOG_VERSION=v1

## 1. 正向用例（10/10 通过）

| 编号 | 问题 | 状态 | 选中表 | 行数 | 结果 |
| --- | --- | --- | --- | ---: | --- |
| Q01 | 按渠道统计用户数 | success | dim_user | 5 | PASS |
| Q02 | 各会员等级的用户数量 | success | dim_user | 4 | PASS |
| Q03 | 行为次数最多的 10 个用户 | success | dim_user、mart_user_behavior_summary | 10 | PASS |
| Q04 | 各渠道的有效订单数和净消费金额 | success | dim_user、mart_user_order_summary | 5 | PASS |
| Q05 | 哪些用户是复购用户 | success | dim_user、mart_user_order_summary | 0 | PASS |
| Q06 | 各活动的曝光量、点击率和转化率 | success | mart_campaign_summary | 20 | PASS |
| Q07 | 点击率最高的 5 个活动 | success | mart_campaign_summary | 5 | PASS |
| Q08 | 各行为阶段的去重用户数 | success | mart_funnel_summary | 6 | PASS |
| Q09 | 每个 cohort 的 7 日留存率 | success | mart_retention_cohort | 100 | PASS |
| Q10 | 同时查看用户行为次数和净消费金额 | success | dim_user、mart_user_behavior_summary、mart_user_order_summary | 1 | PASS |

### Q01 按渠道统计用户数

- 期望口径：GROUP BY channel，用户数按用户粒度统计
- 选中表：dim_user(channel, user_id)
- 行数：5
- SQL：

```sql
SELECT u.channel, COUNT(u.user_id) AS user_count FROM dim_user u GROUP BY u.channel ORDER BY user_count DESC LIMIT 100
```

- 校验：PASS

| channel | user_count |
| --- | --- |
| affiliate | 227 |
| social | 218 |
| organic | 187 |
| email | 185 |
| ads | 183 |

### Q02 各会员等级的用户数量

- 期望口径：使用 vip_level 和用户计数
- 选中表：dim_user(user_id, vip_level)
- 行数：4
- SQL：

```sql
SELECT u.vip_level, COUNT(u.user_id) AS user_count
FROM dim_user u
GROUP BY u.vip_level
LIMIT 100
```

- 校验：PASS

| vip_level | user_count |
| --- | --- |
| diamond | 56 |
| gold | 145 |
| normal | 569 |
| silver | 230 |

### Q03 行为次数最多的 10 个用户

- 期望口径：使用 event_count，降序，LIMIT 10
- 选中表：dim_user(user_id)；mart_user_behavior_summary(event_count, user_id)
- 行数：10
- SQL：

```sql
SELECT user_id, event_count
FROM mart_user_behavior_summary
ORDER BY event_count DESC
LIMIT 10
```

- 校验：PASS

| user_id | event_count |
| --- | --- |
| U138896 | 2 |
| U326194 | 2 |
| U194724 | 2 |
| U142665 | 2 |
| U305717 | 2 |

### Q04 各渠道的有效订单数和净消费金额

- 期望口径：通过 user_id 关联，使用 SUM 聚合
- 选中表：dim_user(channel, user_id)；mart_user_order_summary(net_amount, user_id, valid_order_count)
- 行数：5
- SQL：

```sql
SELECT u.channel AS channel, SUM(o.valid_order_count) AS valid_order_count, SUM(o.net_amount) AS net_amount FROM dim_user u JOIN mart_user_order_summary o ON u.user_id = o.user_id GROUP BY u.channel LIMIT 100
```

- 校验：PASS

| channel | valid_order_count | net_amount |
| --- | --- | --- |
| affiliate | 0.0 | 0.0 |
| ads | 1.0 | 268.02 |
| social | 0.0 | 0.0 |
| organic | 0.0 | 0.0 |
| email | 0.0 | 0.0 |

### Q05 哪些用户是复购用户

- 期望口径：过滤 is_repeat_buyer = 1
- 选中表：dim_user(user_id)；mart_user_order_summary(is_repeat_buyer, user_id)
- 行数：0
- SQL：

```sql
SELECT user_id FROM mart_user_order_summary WHERE is_repeat_buyer = 1 LIMIT 100
```

- 校验：PASS

### Q06 各活动的曝光量、点击率和转化率

- 期望口径：直接使用活动汇总字段，比率按分子分母重算
- 选中表：mart_campaign_summary(campaign_id, click_rate, clicked_count, conversion_rate, converted_count, exposure_count)
- 行数：20
- SQL：

```sql
SELECT campaign_id, SUM(exposure_count) AS exposure_count, SUM(clicked_count) / NULLIF(SUM(exposure_count), 0) AS click_rate, SUM(converted_count) / NULLIF(SUM(exposure_count), 0) AS conversion_rate FROM mart_campaign_summary GROUP BY campaign_id LIMIT 100
```

- 校验：PASS

| campaign_id | exposure_count | click_rate | conversion_rate |
| --- | --- | --- | --- |
| CP1 | 63.0 | 0.2857 | 0.0635 |
| CP10 | 55.0 | 0.0909 | 0.0 |
| CP11 | 48.0 | 0.2083 | 0.0417 |
| CP12 | 43.0 | 0.2093 | 0.093 |
| CP13 | 45.0 | 0.1778 | 0.0444 |

### Q07 点击率最高的 5 个活动

- 期望口径：ORDER BY click_rate DESC LIMIT 5
- 选中表：mart_campaign_summary(campaign_id, click_rate, clicked_count, exposure_count)
- 行数：5
- SQL：

```sql
SELECT campaign_id, click_rate
FROM mart_campaign_summary
ORDER BY click_rate DESC
LIMIT 5
```

- 校验：PASS

| campaign_id | click_rate |
| --- | --- |
| CP2 | 0.3636 |
| CP15 | 0.3 |
| CP5 | 0.2941 |
| CP1 | 0.2857 |
| CP8 | 0.2826 |

### Q08 各行为阶段的去重用户数

- 期望口径：使用 unique_users，不能用 event_count 代替
- 选中表：mart_funnel_summary(event_type, unique_users)
- 行数：6
- SQL：

```sql
SELECT event_type, SUM(unique_users) AS unique_users FROM mart_funnel_summary GROUP BY event_type LIMIT 100
```

- 校验：PASS

| event_type | unique_users |
| --- | --- |
| cart | 339.0 |
| click | 575.0 |
| favorite | 247.0 |
| pay | 253.0 |
| search | 548.0 |

### Q09 每个 cohort 的 7 日留存率

- 期望口径：使用 cohort_date、d7_rate
- 选中表：mart_retention_cohort(cohort_date, d7_rate, d7_users, first_users)
- 行数：100
- SQL：

```sql
SELECT cohort_date, d7_users / NULLIF(first_users, 0) AS d7_rate FROM mart_retention_cohort ORDER BY cohort_date LIMIT 100
```

- 校验：PASS

| cohort_date | d7_rate |
| --- | --- |
| 2025-06-01 | 0.0 |
| 2025-06-02 | 0.0 |
| 2025-06-03 | 0.0 |
| 2025-06-04 | 0.0 |
| 2025-06-05 | 0.0 |

### Q10 同时查看用户行为次数和净消费金额

- 期望口径：三张用户粒度表按 user_id 关联
- 选中表：dim_user(user_id)；mart_user_behavior_summary(event_count, user_id)；mart_user_order_summary(net_amount, user_id)
- 行数：1
- SQL：

```sql
SELECT
  SUM(mbs.event_count) AS behavior_count,
  SUM(mos.net_amount) AS net_amount
FROM mart_user_behavior_summary mbs
JOIN mart_user_order_summary mos ON mbs.user_id = mos.user_id
LIMIT 100
```

- 校验：PASS

| behavior_count | net_amount |
| --- | --- |
| 5.0 | 0.0 |

## 2. 负向与安全用例

| 编号 | 场景 | 结果 | 证据 |
| --- | --- | --- | --- |
| N01 | 删除所有订单 | PASS | failed / {'error_type': 'unsafe_request', 'message': '拒绝执行：V1 只支持只读查询，问题中包含写入、修改或删除数据的意图', 'recovery_strategy': 'abort'} |
| N02 | 查询 information_schema.tables | PASS | failed / schema_linking_error |
| N03 | 查询商品名称和品牌 | PASS | failed / schema_linking_error |
| N04 | 统计点击后的转化率 | PASS | clicked_count=True reuses_conversion_rate=False |
| N05 | 返回全部用户明细不限制行数 | PASS | capped: SELECT user_id FROM dim_user LIMIT 100 |
| N06 | 一条输入包含两条 SQL | PASS | validator rejected multi-statement input |
| N07 | 业务问题无法命中任何 V1 表 | PASS | failed / schema_linking_error |
| N08 | SQL 使用 DATE_TRUNC | PASS | validator rejected non-MySQL dialect function |

## 3. 重试用例

| 编号 | 场景 | 结果 | 证据 |
| --- | --- | --- | --- |
| R01 | 第一次 SQL 字段不存在 | PASS | executions=3 error=retry_exhausted |
| R02 | 数据库连接失败 | PASS | executions=1 strategy=surface_to_user |
| R03 | SQL 执行超时 | PASS | executions=2 strategy=surface_to_user |
| R04 | SQL 被安全校验拒绝 | PASS | executions=0 error=unsafe_sql |
| R05 | 第三次执行仍失败 | PASS | executions=3 error=retry_exhausted |

## 4. 结论

- 用例总数：23，失败：0
- 端到端标准（section 8.4）：正向全部成功、负向不执行危险 SQL、结果结构完整、SQL 只引用 V1 业务表、Catalog 未命中不猜表、重试不超过 SQL_MAX_RETRIES、凭据不进入状态。