from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import datetime

from base_strategy import OrderRequest, OrderUpdate


class PaperOrderRouter:
    """Paper router with deterministic fills and explicit live-order isolation."""

    def __init__(self, master_logger: logging.Logger, slippage_bps: float = 1.0) -> None:
        self.master_logger = master_logger
        self.slippage_bps = float(slippage_bps)
        self._ids = itertools.count(1)

    async def place_order(self, request: OrderRequest) -> OrderUpdate:
        await asyncio.sleep(0)
        side = request.transaction_type.upper()
        intended = float(request.intended_price or request.target_price or 0.0)
        slip = intended * (self.slippage_bps / 10000.0)
        executed = intended + slip if side == "BUY" else intended - slip
        order_id = f"PAPER-{request.strategy_name}-{next(self._ids):06d}"

        update = OrderUpdate(
            strategy_name=request.strategy_name,
            symbol=request.symbol,
            order_id=order_id,
            transaction_type=side,
            quantity=int(request.quantity),
            status="FILLED",
            intended_price=intended,
            executed_price=executed,
            target_price=request.target_price,
            stop_loss=request.stop_loss,
            trigger_price=request.trigger_price,
            slippage=executed - intended,
            timestamp=datetime.now(),
            metadata=dict(request.metadata),
        )
        self.master_logger.info(
            "PAPER_ORDER_FILLED strategy=%s symbol=%s order_id=%s side=%s qty=%s "
            "intended=%.4f executed=%.4f slippage=%.4f",
            update.strategy_name,
            update.symbol,
            update.order_id,
            update.transaction_type,
            update.quantity,
            update.intended_price,
            update.executed_price,
            update.slippage,
        )
        return update
