"""Upstox intraday equity cost model (NSE). Versioned so research results record which model they used.

Source: Upstox pricing page (help-center article 257381), verified 2026-10-05:
  brokerage     : min(Rs 20, 0.1% of turnover) per *executed order* (not per fill)
  STT           : 0.025% of sell-side turnover
  NSE txn charge: 0.00307% of turnover
  SEBI fee      : Rs 10 per crore of turnover
  stamp duty    : 0.003% of buy-side turnover
  GST           : 18% on (brokerage + transaction charges + SEBI fee)
Auto square-off by broker: Rs 88.50 per order (we never rely on it).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.types import Side

COST_MODEL_VERSION = "upstox-intraday-eq-2026-10"


@dataclass(frozen=True)
class CostParams:
    brokerage_flat: float = 20.0
    brokerage_pct: float = 0.001
    stt_sell_pct: float = 0.00025
    txn_pct: float = 0.0000307
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.00003
    gst_pct: float = 0.18
    auto_squareoff_fee: float = 88.50


@dataclass(frozen=True)
class Charges:
    brokerage: float
    stt: float
    txn: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.txn + self.sebi + self.stamp + self.gst, 2)


class UpstoxCostModel:
    version = COST_MODEL_VERSION

    def __init__(self, params: CostParams | None = None) -> None:
        self.p = params or CostParams()

    def order_charges(self, side: Side, turnover_rupees: float) -> Charges:
        """Charges for one executed order with total executed turnover (all fills of that order)."""
        if turnover_rupees <= 0:
            return Charges(0, 0, 0, 0, 0, 0)
        p = self.p
        brokerage = min(p.brokerage_flat, p.brokerage_pct * turnover_rupees)
        stt = p.stt_sell_pct * turnover_rupees if side == Side.SELL else 0.0
        txn = p.txn_pct * turnover_rupees
        sebi = p.sebi_per_crore * turnover_rupees / 1e7
        stamp = p.stamp_buy_pct * turnover_rupees if side == Side.BUY else 0.0
        gst = p.gst_pct * (brokerage + txn + sebi)
        return Charges(brokerage, stt, txn, sebi, stamp, gst)

    def round_trip(self, qty: int, buy_price_rupees: float, sell_price_rupees: float) -> float:
        b = self.order_charges(Side.BUY, qty * buy_price_rupees).total
        s = self.order_charges(Side.SELL, qty * sell_price_rupees).total
        return round(b + s, 2)

    def round_trip_bps(self, notional_rupees: float) -> float:
        return self.round_trip(1, notional_rupees, notional_rupees) / notional_rupees * 1e4
