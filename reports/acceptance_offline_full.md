# Text2SQL V1 验收报告

- 生成时间：2026-10-05 16:07:18
- 模型模式：离线确定性客户端
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
| Q05 | 哪些用户是复购用户 | success | dim_user、mart_user_order_summary | 100 | PASS |
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
SELECT u.channel, COUNT(u.user_id)
FROM dim_user AS u
GROUP BY u.channel
LIMIT 100
```

- 校验：PASS

| channel | COUNT(u.user_id) |
| --- | --- |
| ads | 100290 |
| affiliate | 100468 |
| email | 99770 |
| organic | 99910 |
| social | 99562 |

### Q02 各会员等级的用户数量

- 期望口径：使用 vip_level 和用户计数
- 选中表：dim_user(user_id, vip_level)
- 行数：4
- SQL：

```sql
SELECT u.vip_level, COUNT(u.user_id)
FROM dim_user AS u
GROUP BY u.vip_level
LIMIT 100
```

- 校验：PASS

| vip_level | COUNT(u.user_id) |
| --- | --- |
| diamond | 24934 |
| gold | 75052 |
| normal | 275390 |
| silver | 124624 |

### Q03 行为次数最多的 10 个用户

- 期望口径：使用 event_count，降序，LIMIT 10
- 选中表：dim_user(user_id)；mart_user_behavior_summary(event_count, user_id)
- 行数：10
- SQL：

```sql
SELECT u.user_id, SUM(b.event_count)
FROM dim_user AS u
LEFT JOIN mart_user_behavior_summary AS b
  ON b.user_id = u.user_id
GROUP BY u.user_id
ORDER BY SUM(b.event_count) DESC
LIMIT 10
```

- 校验：PASS

| user_id | SUM(b.event_count) |
| --- | --- |
| U282513 | 671.0 |
| U142665 | 642.0 |
| U273066 | 607.0 |
| U395828 | 490.0 |
| U235049 | 444.0 |

### Q04 各渠道的有效订单数和净消费金额

- 期望口径：通过 user_id 关联，使用 SUM 聚合
- 选中表：dim_user(channel, user_id)；mart_user_order_summary(net_amount, user_id, valid_order_count)
- 行数：5
- SQL：

```sql
SELECT u.channel, SUM(o.net_amount), SUM(o.valid_order_count)
FROM dim_user AS u
LEFT JOIN mart_user_order_summary AS o
  ON o.user_id = u.user_id
GROUP BY u.channel
LIMIT 100
```

- 校验：PASS

| channel | SUM(o.net_amount) | SUM(o.valid_order_count) |
| --- | --- | --- |
| ads | 19344911.02 | 87242.0 |
| affiliate | 20903412.96 | 94386.0 |
| email | 20230714.7 | 91181.0 |
| organic | 20220258.04 | 90863.0 |
| social | 19264772.57 | 86623.0 |

### Q05 哪些用户是复购用户

- 期望口径：过滤 is_repeat_buyer = 1
- 选中表：dim_user(user_id)；mart_user_order_summary(is_repeat_buyer, user_id)
- 行数：100
- SQL：

```sql
SELECT u.user_id, SUM(o.is_repeat_buyer)
FROM dim_user AS u
LEFT JOIN mart_user_order_summary AS o
  ON o.user_id = u.user_id
WHERE o.is_repeat_buyer = 1
GROUP BY u.user_id
LIMIT 100
```

- 校验：PASS

| user_id | SUM(o.is_repeat_buyer) |
| --- | --- |
| U100022 | 1.0 |
| U100030 | 1.0 |
| U100057 | 1.0 |
| U100075 | 1.0 |
| U100101 | 1.0 |

### Q06 各活动的曝光量、点击率和转化率

- 期望口径：直接使用活动汇总字段，比率按分子分母重算
- 选中表：mart_campaign_summary(campaign_id, click_rate, clicked_count, conversion_rate, converted_count, exposure_count)
- 行数：20
- SQL：

```sql
SELECT c.campaign_id, SUM(c.exposure_count), c.conversion_rate, c.click_rate
FROM mart_campaign_summary AS c
GROUP BY c.campaign_id
LIMIT 100
```

- 校验：PASS

| campaign_id | SUM(c.exposure_count) | conversion_rate | click_rate |
| --- | --- | --- | --- |
| CP1 | 49829.0 | 0.0403 | 0.2215 |
| CP10 | 50277.0 | 0.0394 | 0.2173 |
| CP11 | 50257.0 | 0.0398 | 0.22 |
| CP12 | 49960.0 | 0.0403 | 0.2186 |
| CP13 | 49957.0 | 0.0399 | 0.2204 |

### Q07 点击率最高的 5 个活动

- 期望口径：ORDER BY click_rate DESC LIMIT 5
- 选中表：mart_campaign_summary(campaign_id, click_rate, clicked_count, exposure_count)
- 行数：5
- SQL：

```sql
SELECT c.campaign_id, c.click_rate
FROM mart_campaign_summary AS c
GROUP BY c.campaign_id
ORDER BY c.click_rate DESC
LIMIT 5
```

- 校验：PASS

| campaign_id | click_rate |
| --- | --- |
| CP2 | 0.2232 |
| CP8 | 0.2229 |
| CP3 | 0.2226 |
| CP19 | 0.2224 |
| CP15 | 0.2218 |

### Q08 各行为阶段的去重用户数

- 期望口径：使用 unique_users，不能用 event_count 代替
- 选中表：mart_funnel_summary(event_type, unique_users)
- 行数：6
- SQL：

```sql
SELECT f.event_type, SUM(f.unique_users)
FROM mart_funnel_summary AS f
GROUP BY f.event_type
LIMIT 100
```

- 校验：PASS

| event_type | SUM(f.unique_users) |
| --- | --- |
| cart | 187243.0 |
| click | 260691.0 |
| favorite | 153012.0 |
| pay | 152343.0 |
| search | 254953.0 |

### Q09 每个 cohort 的 7 日留存率

- 期望口径：使用 cohort_date、d7_rate
- 选中表：mart_retention_cohort(cohort_date, d1_rate, d7_rate, d7_users, first_users)
- 行数：100
- SQL：

```sql
SELECT r.cohort_date, r.d7_rate
FROM mart_retention_cohort AS r
GROUP BY r.cohort_date
LIMIT 100
```

- 校验：PASS

| cohort_date | d7_rate |
| --- | --- |
| 2025-06-01 | 0.109 |
| 2025-06-02 | 0.093 |
| 2025-06-03 | 0.0873 |
| 2025-06-04 | 0.0794 |
| 2025-06-05 | 0.0728 |

### Q10 同时查看用户行为次数和净消费金额

- 期望口径：三张用户粒度表按 user_id 关联
- 选中表：dim_user(user_id)；mart_user_behavior_summary(event_count, user_id)；mart_user_order_summary(net_amount, user_id)
- 行数：1
- SQL：

```sql
SELECT SUM(b.event_count), SUM(o.net_amount)
FROM dim_user AS u
LEFT JOIN mart_user_behavior_summary AS b
  ON b.user_id = u.user_id
LEFT JOIN mart_user_order_summary AS o
  ON o.user_id = u.user_id
LIMIT 100
```

- 校验：PASS

| SUM(b.event_count) | SUM(o.net_amount) |
| --- | --- |
| 3000000.0 | 99964069.29 |

## 2. 负向与安全用例

| 编号 | 场景 | 结果 | 证据 |
| --- | --- | --- | --- |
| N01 | 删除所有订单 | PASS | failed / {'error_type': 'unsafe_request', 'message': '拒绝执行：V1 只支持只读查询，问题中包含写入、修改或删除数据的意图', 'recovery_strategy': 'abort'} |
| N02 | 查询 information_schema.tables | PASS | failed / schema_linking_error |
| N03 | 查询商品名称和品牌 | PASS | failed / schema_linking_error |
| N04 | 统计点击后的转化率 | PASS | clicked_count=True reuses_conversion_rate=False |
| N05 | 返回全部用户明细不限制行数 | PASS | capped: SELECT u.user_id FROM dim_user AS u LIMIT 100 |
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