from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


try:  # Optional dependency
    from pysnmp.hlapi.v1arch.asyncio import (  # type: ignore
        CommunityData,
        ObjectIdentity,
        ObjectType,
        SnmpDispatcher,
        UdpTransportTarget,
        get_cmd,
    )

    _HAS_PYSNMP = True
except Exception:  # pragma: no cover - import guarded at runtime
    _HAS_PYSNMP = False


@dataclass(frozen=True)
class UpsInputMetrics:
    voltage: Optional[float]
    frequency: Optional[float]


class UpsSnmpClient:
    def __init__(
        self,
        *,
        host: str,
        port: int = 161,
        community: str = "public",
        version: str = "2c",
        timeout_seconds: float = 2.0,
        retries: int = 1,
    ) -> None:
        if not _HAS_PYSNMP:
            raise RuntimeError("pysnmp_not_installed")
        self._host = host
        self._port = int(port)
        self._community = community
        self._version = (version or "2c").strip().lower()
        self._timeout = float(timeout_seconds)
        self._retries = int(retries)
        self._dispatcher = SnmpDispatcher()

    async def get_input_metrics(self, *, voltage_oid: str, frequency_oid: str) -> UpsInputMetrics:
        oids = [voltage_oid, frequency_oid]
        results = await self._snmp_get(oids)
        return UpsInputMetrics(
            voltage=_as_float(results.get(voltage_oid)),
            frequency=_as_float(results.get(frequency_oid)),
        )

    async def _snmp_get(self, oids: list[str]) -> dict[str, object]:
        mp_model = 1 if self._version in ("2", "2c", "v2", "v2c") else 0
        target = await UdpTransportTarget.create(
            (self._host, self._port), timeout=self._timeout, retries=self._retries
        )
        error_indication, error_status, error_index, var_binds = await get_cmd(
            self._dispatcher,
            CommunityData(self._community, mpModel=mp_model),
            *[ObjectType(ObjectIdentity(oid)) for oid in oids],
        )
        if error_indication:
            raise RuntimeError(str(error_indication))
        if error_status:
            idx = int(error_index) if error_index else 0
            err = "%s at %s" % (error_status, idx)
            raise RuntimeError(err)

        out: dict[str, object] = {}
        for var in var_binds:
            try:
                name, val = var
            except Exception:
                continue
            out[str(name)] = val
        return out


def _as_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        try:
            return float(str(value))
        except Exception:
            return None
