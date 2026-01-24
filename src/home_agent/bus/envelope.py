from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return uuid4().hex


def make_event(*, source: str, typ: str, data: Dict[str, Any], trace_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Standard event envelope.
    """
    tid = trace_id or new_id()
    return {
        "id": new_id(),
        "ts": now_rfc3339(),
        "source": source,
        "type": typ,
        "trace_id": tid,
        "data": data,
    }

