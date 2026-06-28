from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Dict, List, Optional

from base_strategy import BaseStrategy, MarketDataEvent, StrategyContext
from data_engine import AsyncMarketDataEngine, LiveMarketDataEngine, build_data_provider
from logger_setup import setup_master_logger, setup_strategy_logger
from order_router import PaperOrderRouter
from performance_tracker import PerformanceTracker
from strategy_loader import import_strategy_class, load_engine_config, strategy_specs


class AsyncStrategyEngine:
    def __init__(self, config_path: str = "strategy_engine_config.json") -> None:
        self.config_path = config_path
        self.engine_config = load_engine_config(config_path)
        self.mode = str(self.engine_config.get("mode", "paper")).lower()
        self.log_dir = str(self.engine_config.get("log_dir", "logs"))
        self.performance_dir = str(self.engine_config.get("performance_dir", "performance"))
        self.master_logger = setup_master_logger(self.log_dir, logging.INFO)
        self.performance_tracker = PerformanceTracker(self.performance_dir)
        self.order_router = PaperOrderRouter(
            self.master_logger,
            slippage_bps=float(self.engine_config.get("paper_slippage_bps", 1.0)),
        )
        self.data_provider = build_data_provider(self.engine_config)
        data_config = dict(self.engine_config.get("data", {}))
        if self.mode == "live_sandbox":
            self.data_engine = LiveMarketDataEngine()
        else:
            self.data_engine = AsyncMarketDataEngine(
                self.data_provider,
                replay_sleep_seconds=float(data_config.get("replay_sleep_seconds", 0.0)),
            )
        self.strategies: Dict[str, BaseStrategy] = {}
        self.queues: Dict[str, asyncio.Queue] = {}

    def load_strategies(self) -> None:
        for spec in strategy_specs(self.engine_config):
            if not spec.enabled:
                self.master_logger.info("STRATEGY_DISABLED name=%s", spec.name)
                continue

            strategy_class = import_strategy_class(spec)
            strategy_logger = setup_strategy_logger(spec.name, self.log_dir)
            context = StrategyContext(
                name=spec.name,
                symbols=spec.symbols,
                timeframes=spec.timeframes,
                mode=self.mode,
                logger=strategy_logger,
                order_router=self.order_router,
                performance_tracker=self.performance_tracker,
                config=spec.config,
            )
            strategy = strategy_class(context)
            queue: asyncio.Queue = asyncio.Queue(maxsize=int(spec.config.get("queue_size", 1000)))
            self.strategies[spec.name] = strategy
            self.queues[spec.name] = queue
            self.data_engine.register_strategy(spec.name, spec.symbols, spec.timeframes, queue)
            self.master_logger.info(
                "STRATEGY_LOADED name=%s module=%s class=%s symbols=%s timeframes=%s",
                spec.name,
                spec.module,
                spec.class_name,
                ",".join(spec.symbols),
                ",".join(spec.timeframes),
            )

    async def start(self) -> None:
        self.load_strategies()
        initialized = await self._initialize_strategies()
        if not initialized:
            self.master_logger.error("NO_STRATEGIES_INITIALIZED")
            return

        strategy_tasks = [
            asyncio.create_task(self._strategy_loop(name, strategy, self.queues[name]), name=name)
            for name, strategy in initialized.items()
        ]
        
        if self.mode == "live_sandbox":
            data_task = asyncio.create_task(self.data_engine.run_live(), name="market_data_live")
        else:
            data_task = asyncio.create_task(self.data_engine.run_replay(), name="market_data_replay")

        try:
            if self.mode == "live_sandbox":
                await asyncio.gather(data_task, *strategy_tasks)
            else:
                await data_task
                await asyncio.gather(*strategy_tasks)
        except asyncio.CancelledError:
            self.master_logger.warning("ENGINE_CANCELLED")
            if self.mode == "live_sandbox":
                self.data_engine.stop()
            raise
        finally:
            await self._shutdown_strategies(initialized)

    async def _initialize_strategies(self) -> Dict[str, BaseStrategy]:
        initialized: Dict[str, BaseStrategy] = {}
        for name, strategy in self.strategies.items():
            try:
                await strategy.on_initialize()
                initialized[name] = strategy
                self.master_logger.info("STRATEGY_INITIALIZED name=%s", name)
            except Exception:
                strategy.logger.exception("STRATEGY_INITIALIZE_FAILED name=%s", name)
                self.master_logger.exception("STRATEGY_INITIALIZE_FAILED name=%s", name)
        return initialized

    async def _strategy_loop(
        self,
        name: str,
        strategy: BaseStrategy,
        queue: asyncio.Queue,
    ) -> None:
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            event: MarketDataEvent = item
            try:
                await strategy.update_timeframe_state(event)
                await strategy.on_market_data(event)
            except Exception:
                strategy.logger.exception(
                    "STRATEGY_RUNTIME_EXCEPTION name=%s symbol=%s timeframe=%s candle_ts=%s",
                    name,
                    event.symbol,
                    event.timeframe,
                    event.candle.timestamp.isoformat(),
                )
                self.master_logger.exception("STRATEGY_RUNTIME_EXCEPTION_ISOLATED name=%s", name)
            finally:
                queue.task_done()

    async def _shutdown_strategies(self, strategies: Optional[Dict[str, BaseStrategy]] = None) -> None:
        strategies = strategies or self.strategies
        for name, strategy in strategies.items():
            try:
                await strategy.on_shutdown()
            except Exception:
                strategy.logger.exception("STRATEGY_SHUTDOWN_FAILED name=%s", name)
            try:
                summary = self.performance_tracker.write_weekly_summary(name)
                self.master_logger.info("WEEKLY_SUMMARY strategy=%s summary=%s", name, summary)
                self.performance_tracker.write_all_trades(name)
                self.master_logger.info("ALL_TRADES_EXPORTED strategy=%s", name)
            except Exception:
                self.master_logger.exception("WEEKLY_SUMMARY_FAILED strategy=%s", name)

        try:
            self.performance_tracker.generate_llm_report(self.engine_config)
            self.master_logger.info("LLM_ANALYSIS_REPORT_GENERATED")
        except Exception:
            self.master_logger.exception("LLM_ANALYSIS_REPORT_FAILED")


async def run_from_config(config_path: str = "strategy_engine_config.json") -> None:
    engine = AsyncStrategyEngine(config_path=config_path)
    await engine.start()


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Async multi-strategy trading framework")
    parser.add_argument(
        "--config",
        default="strategy_engine_config.json",
        help="Path to strategy engine config JSON",
    )
    args = parser.parse_args(argv)
    asyncio.run(run_from_config(args.config))


if __name__ == "__main__":
    main()
