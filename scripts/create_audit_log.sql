-- ====================================================================
-- Agent audit log table used by src/ui.py (write_audit_log + admin viewer)
-- Run as sa, connected to the database that holds FDE_VIEWS:
--   LOCAL profile -> master        AWS profile -> SupplyChainDB
-- Safe to re-run: it only creates the table if it is missing.
-- ====================================================================
IF DB_NAME() NOT IN (N'master', N'SupplyChainDB')
    THROW 50000, 'Connect to master (LOCAL) or SupplyChainDB (AWS) before running this script.', 1;
GO

IF OBJECT_ID(N'FDE_VIEWS.AgentAuditLog', N'U') IS NULL
BEGIN
    CREATE TABLE FDE_VIEWS.AgentAuditLog (
        LogID        INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_AgentAuditLog PRIMARY KEY,
        [Timestamp]  DATETIME2(3)      NOT NULL CONSTRAINT DF_AgentAuditLog_Timestamp DEFAULT SYSDATETIME(),
        SessionID    NVARCHAR(64)      NOT NULL,
        NodeExecuted NVARCHAR(100)     NULL,
        ToolName     NVARCHAR(200)     NULL,
        Content      NVARCHAR(MAX)     NULL
    );
END
GO

-- The agent may only ADD log rows: no SELECT/UPDATE/DELETE on the log.
GRANT INSERT ON FDE_VIEWS.AgentAuditLog TO USR_FDE_RO;
GO

-- Verify
SELECT DB_NAME() AS current_db,
       OBJECT_ID(N'FDE_VIEWS.AgentAuditLog', N'U') AS audit_table_id,
       HAS_PERMS_BY_NAME(N'FDE_VIEWS.AgentAuditLog', N'OBJECT', N'INSERT') AS caller_can_insert;
