-- Read-only checks for the LOCAL Docker SQL Server (legacy-mssql, 127.0.0.1:1433).
-- Run with the VS Code profile "LOCAL Docker legacy-mssql (127.0.0.1:1433)".

USE master;

-- 1. Confirm you are on LOCAL (expect server_name = ce63abe9386f, current_db = master)
SELECT @@SERVERNAME AS server_name,
       DB_NAME()    AS current_db,
       SUSER_SNAME() AS login_name;

-- 2. Row count (expect 32065)
SELECT COUNT(*) AS total_rows
FROM dbo.TBL_SC_FLEET_HIST_RAW;

-- 3. Columns and types
SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'TBL_SC_FLEET_HIST_RAW'
ORDER BY ORDINAL_POSITION;

-- 4. Sample rows
SELECT TOP 5 *
FROM dbo.TBL_SC_FLEET_HIST_RAW
ORDER BY TS_UTC;

-- 5. Quick data sanity: date range, NULLs, ingest flag
SELECT MIN(TS_UTC)                                         AS first_ts,
       MAX(TS_UTC)                                         AS last_ts,
       SUM(CASE WHEN IOT_TEMP_VAL_C IS NULL THEN 1 ELSE 0 END) AS null_temps,
       SUM(CASE WHEN SYS_INGEST_FLAG = 'Y' THEN 1 ELSE 0 END)  AS flagged_rows
FROM dbo.TBL_SC_FLEET_HIST_RAW;

-- 6. Rows per risk class
SELECT RISK_CLS_TXT, COUNT(*) AS rows_per_class
FROM dbo.TBL_SC_FLEET_HIST_RAW
GROUP BY RISK_CLS_TXT
ORDER BY rows_per_class DESC;
