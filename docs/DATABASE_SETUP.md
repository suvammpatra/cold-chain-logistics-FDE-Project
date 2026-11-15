# Database Setup: Local Docker and AWS EC2 SQL Server

This project uses two **separate** SQL Server 2022 instances. They hold separate copies of the data and are never synchronised automatically.

| | **LOCAL** | **AWS** |
|---|---|---|
| Where it runs | Docker Desktop on this PC, container `legacy-mssql` | Docker on EC2 `logistics-supply-chain-server` (Ubuntu 24.04), container `supply-chain-sqlserver` |
| How you reach it | `127.0.0.1:1433` | SSH tunnel: `127.0.0.1:14330` → EC2 `127.0.0.1:1433` |
| Database used by the scripts | `master` | `SupplyChainDB` |
| Data storage | Docker volume `legacy_mssql_data` | Host bind mounts `/opt/supply-chain/sqlserver/data` and `/opt/supply-chain/sqlserver/backups` |
| `.env` prefix | `LOCAL_SQL_*` | `AWS_SQL_*` |
| Script flag | `--target local` | `--target aws` |
| VS Code profile | `LOCAL Docker legacy-mssql (127.0.0.1:1433)` | `AWS EC2 via SSH tunnel (127.0.0.1:14330)` |
| How to tell them apart | `@@SERVERNAME` = `ce63abe9386f`, 12 CPU / ~6 GB | `@@SERVERNAME` = `sqlserver`, 2 CPU / ~1.6 GB |

**Rule of thumb:** port **1433** is always LOCAL, and port **14330** is always AWS. SQL Server on EC2 is never exposed publicly. Never connect to `13.54.217.203,1433`.

---

## 1. Configuration (`.env`)

`.env` is gitignored. Copy `.env.example` to `.env` and fill in the passwords:

```ini
DB_TARGET=local                 # default when --target is not passed

LOCAL_SQL_HOST=127.0.0.1
LOCAL_SQL_PORT=1433
LOCAL_SQL_DATABASE=master
LOCAL_SQL_USER=sa
LOCAL_SQL_PASSWORD="..."

AWS_SQL_HOST=127.0.0.1          # always localhost: traffic goes through the tunnel
AWS_SQL_PORT=14330
AWS_SQL_DATABASE=SupplyChainDB
AWS_SQL_USER=sa
AWS_SQL_PASSWORD="..."

AWS_EC2_HOST=13.54.217.203      # re-check after every EC2 stop/start
AWS_SSH_KEY_PATH=C:/path/to/supply-chain-logistics-key.pem
```

Quote passwords that contain `#`, spaces or `;`. The scripts refuse to run if the AWS profile points at the local port (1433) or at the public EC2 IP.

All connection code is in [scripts/db_config.py](../scripts/db_config.py). No credentials are hardcoded in Python.

---

## 2. Local SQL Server (Docker Desktop)

```powershell
docker ps --filter name=legacy-mssql        # expect: Up ... 127.0.0.1:1433->1433/tcp
docker start legacy-mssql                   # if it is stopped (restart policy is "no")
```

Data is stored in the named volume `legacy_mssql_data` (mounted at `/var/opt/mssql`), so it survives container restarts. **Do not** run `docker rm`, `docker volume rm` or `docker compose down -v` on it.

The port is bound to `127.0.0.1` only, so other machines on your network cannot reach it.

---

## 3. AWS EC2 SQL Server

### 3.1 Check the current public IP

An EC2 public IP **changes every time the instance is stopped and started** (unless it has an Elastic IP). Check it in the AWS Console under EC2 → Instances → `logistics-supply-chain-server` → *Public IPv4 address*, and update `AWS_EC2_HOST` and the `HostName` in `~/.ssh/config` if it changed.

### 3.2 SSH key and config

`C:\Users\<you>\.ssh\config`:

```
Host aws-ec2
    HostName 13.54.217.203
    User ubuntu
    IdentityFile "C:/Users/<you>/.ssh/supply-chain-logistics-key.pem"
    IdentitiesOnly yes
```

Keep the key in `~/.ssh/` rather than Downloads so it doesn't get moved or cleaned up. On Windows, OpenSSH rejects keys that other users can read. If you see `UNPROTECTED PRIVATE KEY FILE`, run:

```powershell
icacls "$env:USERPROFILE\.ssh\supply-chain-logistics-key.pem" /inheritance:r /grant:r "$($env:USERNAME):R"
```

Test the connection: `ssh aws-ec2 "hostname; docker ps"`

### 3.3 Container on EC2

The SQL Server container on EC2 should publish its port on loopback only:

```bash
# on EC2: check, don't recreate
docker ps --format '{{.Names}} {{.Status}} {{.Ports}}'
# expect: supply-chain-sqlserver Up ... 127.0.0.1:1433->1433/tcp
```

The container's restart policy is `unless-stopped`, and its data lives on the host under `/opt/supply-chain/sqlserver/` (bind mounts, not Docker volumes). Never delete that directory.

Because the tunnel connects to `127.0.0.1:1433` on the EC2 side, loopback binding is enough. The security group does **not** need an inbound rule for 1433. Only port 22 (SSH), restricted to your IP, is required.

### 3.4 Open the SSH tunnel

Run this in its own terminal and leave it open while you work:

```powershell
ssh -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 14330:127.0.0.1:1433 aws-ec2
# or, without the config alias:
ssh -i "C:/Users/<you>/.ssh/supply-chain-logistics-key.pem" -N -L 14330:127.0.0.1:1433 ubuntu@<CURRENT_EC2_PUBLIC_IP>
```

`-N` means the terminal shows nothing; that is normal. Press Ctrl+C to close the tunnel. Check that it's listening:

```powershell
Test-NetConnection 127.0.0.1 -Port 14330    # TcpTestSucceeded : True
```

---

## 4. Python commands

Run these from the project root with the `fde` conda environment active.

### Connection tests (read-only)

```powershell
python scripts\test_db_connection.py --target local
python scripts\test_db_connection.py --target aws      # tunnel must be open
python scripts\test_db_connection.py                   # both
```

Each test prints the target, `@@SERVERNAME`, CPU/RAM, login, databases, and every database that contains `dbo.TBL_SC_FLEET_HIST_RAW` with its row count. It runs only `SELECT` statements.

### Ingestion

```powershell
python scripts\ingest_legacy_data.py --target local --dry-run   # report only, writes nothing
python scripts\ingest_legacy_data.py --target local
python scripts\ingest_legacy_data.py --target aws               # tunnel must be open
```

Safety behaviour:

- The script prints the exact target (`LOCAL`/`AWS`, host, port, database, server name) before doing anything.
- If `dbo.TBL_SC_FLEET_HIST_RAW` already exists, it **stops without writing** and reports the current row count.
- `--append` adds rows to an existing table. This creates duplicates if the same CSV is loaded twice, so use it deliberately.
- It never drops, truncates or replaces the table (`if_exists="replace"` is not used).
- The load runs in one transaction, so a failure part-way commits nothing.

---

## 5. VS Code (SQL Server extension `ms-mssql.mssql`)

| Profile | Server | Database | Auth | Encrypt | Trust cert |
|---|---|---|---|---|---|
| `LOCAL Docker legacy-mssql (127.0.0.1:1433)` | `tcp:127.0.0.1,1433` | `master` | SQL Login `sa` | Optional | Yes |
| `AWS EC2 via SSH tunnel (127.0.0.1:14330)` | `127.0.0.1,14330` | `master` | SQL Login `sa` | Optional | Yes |

- SQL Server uses a **comma** between host and port (`127.0.0.1,14330`), not a colon.
- Open the SSH tunnel **before** connecting with the AWS profile.
- Only the LOCAL profile should have `"containerName": "legacy-mssql"`. That field links a profile to the local Docker container, so it must never be set on the AWS profile.
- The AWS profile opens in `master`. The scripts use `SupplyChainDB`, so either run `USE SupplyChainDB;` first or add a third profile with the same server and database `SupplyChainDB`.

---

## 6. Verification queries (read-only)

```sql
-- Which server am I on?  (local = ce63abe9386f)
SELECT @@SERVERNAME AS server_name, DB_NAME() AS current_db;

-- Which databases exist?
SELECT name, create_date FROM sys.databases ORDER BY database_id;

-- Row count (run in the database that holds the table)
SELECT COUNT(*) AS total_rows FROM dbo.TBL_SC_FLEET_HIST_RAW;

-- Where does the table exist on this server?
SELECT 'master' AS db, OBJECT_ID('master.dbo.TBL_SC_FLEET_HIST_RAW') AS obj
UNION ALL
SELECT 'SupplyChainDB', OBJECT_ID('SupplyChainDB.dbo.TBL_SC_FLEET_HIST_RAW');

-- Sample rows
SELECT TOP 5 * FROM dbo.TBL_SC_FLEET_HIST_RAW;
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Login timeout expired` / `TCP Provider: The wait operation timed out` to `13.54.217.203,1433` | Connecting directly to the public IP. SQL Server is not exposed there. | Use the tunnel: `127.0.0.1,14330`. |
| `nothing is listening on 127.0.0.1:14330` | SSH tunnel not running | Open the tunnel (§3.4). |
| `no such identity: ...pem` / `Permission denied (publickey)` | `IdentityFile` in `~/.ssh/config` points to a missing key file | Fix the path (§3.2). |
| `ssh: connect to host ... timed out` | EC2 stopped, IP changed, or SG port 22 doesn't allow your current IP | Check the instance state and IP in the console, and update the SG rule to *My IP*. |
| `bind [127.0.0.1]:14330: Address already in use` | A tunnel is already open | Use it, or close the other `ssh` process. |
| `Login failed for user 'sa'` | Wrong password for **that** server (local and AWS can differ) | Fix `LOCAL_SQL_PASSWORD` / `AWS_SQL_PASSWORD`. |
| Local `127.0.0.1:1433` refused | Container stopped | `docker start legacy-mssql` |
| `Invalid object name 'dbo.TBL_SC_FLEET_HIST_RAW'` | Wrong server or wrong database | Run `SELECT @@SERVERNAME, DB_NAME();` and `scripts\test_db_connection.py`. |
| Row count different from expected | You are on the other server | Same as above. Remember the two servers hold separate data. |

### VS Code extension errors vs real connectivity errors

Errors from the extension's **SQL Tools Service** (the backend process) are not database connection failures. To check, look at *Output → SQL Server* or `%APPDATA%\Code\logs\<session>\window1\exthost\ms-mssql.mssql\MSSQL.log`:

- `[ConnectionManager] Connected to server ...` followed by an `[ObjectExplorerService] ...` error means **the database connection worked**; only the tree view failed.
  - `Node does not have a session ID` / `Connection node with ID ... not found` means Object Explorer has stale state (for example, after the tunnel dropped or a profile was edited). Disconnect, refresh, or run *Developer: Reload Window*.
  - `Expand node failed: Unable to load one or more of the requested types` is an internal error in the SQL Tools Service. Queries still run. Reload the window. If it persists, update the extension, or uninstall and reinstall it so the service re-downloads.
- `[ConnectionManager] Error connecting to server ...` is a **real connectivity error**. Run `python scripts\test_db_connection.py --target <local|aws>` to diagnose it outside VS Code.
