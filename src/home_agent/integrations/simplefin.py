from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass(frozen=True)
class SimpleFINAccount:
    id: str
    name: str
    currency: str
    balance: float
    available_balance: Optional[float]
    org_name: Optional[str]
    org_domain: Optional[str]


@dataclass(frozen=True)
class FinancialSummary:
    total_cash: float
    total_debt: float
    net_worth: float
    accounts: List[SimpleFINAccount]
    errors: List[str]


class SimpleFINClient:
    def __init__(self, *, access_url: str, timeout_seconds: float = 30.0) -> None:
        self._access_url = access_url.rstrip("/")
        self._timeout = float(timeout_seconds)

    async def fetch_accounts(self) -> List[SimpleFINAccount]:
        url = "%s/accounts?balances-only=1" % self._access_url
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        raw_accounts = data.get("accounts") or []
        accounts: List[SimpleFINAccount] = []
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            accounts.append(_parse_account(item))
        return accounts

    async def financial_summary(self) -> FinancialSummary:
        accounts = await self.fetch_accounts()
        total_cash = 0.0
        total_debt = 0.0
        for acct in accounts:
            if acct.currency.upper() != "USD":
                continue
            if acct.balance >= 0:
                total_cash += acct.balance
            else:
                total_debt += acct.balance
        net_worth = total_cash + total_debt
        return FinancialSummary(
            total_cash=total_cash,
            total_debt=total_debt,
            net_worth=net_worth,
            accounts=accounts,
            errors=[],
        )


def _parse_account(item: Dict[str, Any]) -> SimpleFINAccount:
    acct_id = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    currency = str(item.get("currency") or "USD").strip().upper()

    balance = _to_float(item.get("balance"), 0.0)
    available_balance = _to_float(item.get("available-balance"))

    org = item.get("org") or {}
    org_name: Optional[str] = None
    org_domain: Optional[str] = None
    if isinstance(org, dict):
        v = org.get("name")
        if isinstance(v, str) and v.strip():
            org_name = v.strip()
        v = org.get("domain")
        if isinstance(v, str) and v.strip():
            org_domain = v.strip()

    return SimpleFINAccount(
        id=acct_id,
        name=name,
        currency=currency,
        balance=balance,
        available_balance=available_balance,
        org_name=org_name,
        org_domain=org_domain,
    )


def _to_float(value: object, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        try:
            return float(str(value))
        except Exception:
            return default
