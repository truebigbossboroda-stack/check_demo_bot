import os
import socket
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..db import get_db
from ..auth import require_bearer

router = APIRouter(tags=["health"])


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _kafka_tcp_ping(bootstrap: str, timeout_sec: float = 1.0) -> bool:
    """
    bootstrap like "localhost:19092"
    TCP connect only (fast readiness probe).
    """
    try:
        host, port_str = bootstrap.split(":")
        port = int(port_str)
    except Exception:
        return False

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_sec)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


@router.get("/health")
def health(_: bool = Depends(require_bearer), db: Session = Depends(get_db)):
    # DB ping
    db.execute(sql_text("SELECT 1"))

    # Alembic version
    ver = db.execute(sql_text("SELECT version_num FROM alembic_version")).scalar_one()

    # Kafka ping (optional, but useful)
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
    kafka_ok = _kafka_tcp_ping(bootstrap)

    return {
        "status": "ok",
        "service_time_utc": _utc_now_iso(),
        "db": "ok",
        "alembic_version": ver,
        "kafka_bootstrap": bootstrap,
        "kafka": "ok" if kafka_ok else "fail",
    }