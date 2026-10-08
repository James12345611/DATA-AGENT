-- V1 read-only account for the Text2SQL agent.
-- Run as a MySQL administrator, e.g.:
--   mysql -uroot -p < scripts/setup_readonly_user.sql
--
-- Replace __PASSWORD__ before executing, or use scripts/setup_readonly_user.py
-- which injects the password from .env.
--
-- The account can only SELECT the six business views exposed in V1
-- (text2sql_V1.md section 6.2). It cannot read raw_*, dim_campaign,
-- dim_product, fact_* or catalog_* objects.

CREATE USER IF NOT EXISTS 'text2sql_readonly'@'localhost'
  IDENTIFIED BY '__PASSWORD__';
ALTER USER 'text2sql_readonly'@'localhost'
  IDENTIFIED BY '__PASSWORD__';

GRANT SELECT ON ecommerce_text2sql.dim_user                  TO 'text2sql_readonly'@'localhost';
GRANT SELECT ON ecommerce_text2sql.mart_user_behavior_summary TO 'text2sql_readonly'@'localhost';
GRANT SELECT ON ecommerce_text2sql.mart_user_order_summary    TO 'text2sql_readonly'@'localhost';
GRANT SELECT ON ecommerce_text2sql.mart_campaign_summary      TO 'text2sql_readonly'@'localhost';
GRANT SELECT ON ecommerce_text2sql.mart_funnel_summary        TO 'text2sql_readonly'@'localhost';
GRANT SELECT ON ecommerce_text2sql.mart_retention_cohort      TO 'text2sql_readonly'@'localhost';

FLUSH PRIVILEGES;

-- Verification: expected to show exactly six SELECT privileges.
SHOW GRANTS FOR 'text2sql_readonly'@'localhost';
