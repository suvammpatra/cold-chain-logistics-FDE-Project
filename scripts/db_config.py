"""Shared SQL Server connection settings for the local Docker and AWS EC2 targets.

Two independent profiles are read from the project .env:

    LOCAL_SQL_*  -> Docker container `legacy-mssql` on this PC (127.0.0.1:1433)
    AWS_SQL_*    -> SQL Server on EC2, reached ONLY through an SSH tunnel
                    (127.0.0.1:14330 -> EC2 127.0.0.1:1433)

Credentials live in .env only; nothing here prints or hardcodes a password.
"""
from __future__ import annotations

import os
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

TARGETS = ("local", "aws")
ODBC_DRIVER = os.getenv("SQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")


class ConfigError(RuntimeError):
    """Raised when .env settings are missing or point at the wrong server."""


@dataclass(frozen=True)
class DbTarget:
    name: str
    host: str
    port: int
    database: str
    user: str
    password: str

    def describe(self) -> str:
        """Human-readable target description; never includes the password."""
        via = " (via SSH tunnel to EC2)" if self.name == "aws" else " (local Docker)"
        return f"{self.name.upper()}{via}: {self.host}:{self.port} / database={self.database} / user={self.user}"

    def odbc_string(self, database: str | None = None) -> str:
        # Encrypt=no is acceptable here: local traffic never leaves the PC and
        # AWS traffic is carried inside the encrypted SSH tunnel.
        return (
            f"DRIVER={{{ODBC_DRIVER}}};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={database or self.database};"
            f"UID={self.user};"
            f"PWD={self.password};"
            "Encrypt=no;TrustServerCertificate=yes;"
        )

    def port_is_open(self, timeout: float = 3.0) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise ConfigError(f"Missing required setting {key} in {PROJECT_ROOT / '.env'}")
    return value


def resolve_target(name: str | None = None) -> str:
    target = (name or os.getenv("DB_TARGET") or "").strip().lower()
    if target not in TARGETS:
        raise ConfigError(
            f"Database target must be one of {TARGETS} (got {target or 'nothing'}). "
            "Pass --target local|aws or set DB_TARGET in .env."
        )
    return target


def get_target(name: str | None = None) -> DbTarget:
    target = resolve_target(name)
    prefix = f"{target.upper()}_SQL_"
    cfg = DbTarget(
        name=target,
        host=_require(prefix + "HOST"),
        port=int(_require(prefix + "PORT")),
        database=_require(prefix + "DATABASE"),
        user=_require(prefix + "USER"),
        password=_require(prefix + "PASSWORD"),
    )

    # Guard against the classic mix-up: the AWS profile pointing at the local
    # container's port (or straight at the public EC2 IP, which is not exposed).
    if target == "aws":
        local_port = os.getenv("LOCAL_SQL_PORT", "1433")
        if cfg.host in ("127.0.0.1", "localhost") and str(cfg.port) == local_port:
            raise ConfigError(
                f"AWS_SQL_PORT={cfg.port} is the LOCAL Docker SQL Server port. "
                "Use the SSH tunnel port (14330) for AWS."
            )
        if cfg.host == os.getenv("AWS_EC2_HOST"):
            raise ConfigError(
                "AWS_SQL_HOST is the public EC2 IP. SQL Server is not exposed publicly; "
                "set AWS_SQL_HOST=127.0.0.1 and AWS_SQL_PORT=14330 and open the SSH tunnel."
            )
    return cfg


def tunnel_hint() -> str:
    ec2 = os.getenv("AWS_EC2_HOST", "<EC2_PUBLIC_IP>")
    key = os.getenv("AWS_SSH_KEY_PATH", "<path-to-key.pem>")
    port = os.getenv("AWS_SQL_PORT", "14330")
    return f'ssh -i "{key}" -N -L {port}:127.0.0.1:1433 ubuntu@{ec2}'


def get_engine(cfg: DbTarget, *, fast_executemany: bool = False) -> Engine:
    params = urllib.parse.quote_plus(cfg.odbc_string())
    return create_engine(
        f"mssql+pyodbc:///?odbc_connect={params}",
        fast_executemany=fast_executemany,
        pool_pre_ping=True,
        connect_args={"timeout": 15},
    )
