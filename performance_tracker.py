from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import DefaultDict, Dict, List, Optional

from base_strategy import TradeRecord, max_drawdown_from_equity


class PerformanceTracker:
    def __init__(self, output_dir: str = "performance") -> None:
        self.output_dir = output_dir
        self._trades: DefaultDict[str, List[TradeRecord]] = defaultdict(list)
        self._slippage: DefaultDict[str, List[float]] = defaultdict(list)
        os.makedirs(output_dir, exist_ok=True)

    def record_trade(self, strategy_name: str, trade: TradeRecord) -> None:
        self._trades[strategy_name].append(trade)
        self._slippage[strategy_name].append(float(trade.slippage or 0.0))

    def record_slippage(self, strategy_name: str, slippage: float) -> None:
        self._slippage[strategy_name].append(float(slippage or 0.0))

    def weekly_summary(self, strategy_name: str, now: Optional[datetime] = None) -> Dict[str, float]:
        trades_list = self._trades.get(strategy_name, [])
        if not now and trades_list:
            now = max(trade.exit_time for trade in trades_list)
        now = now or datetime.now()
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)
        trades = [
            trade for trade in self._trades.get(strategy_name, [])
            if week_start <= trade.exit_time < week_end
        ]

        wins = [trade for trade in trades if trade.pnl > 0]
        losses = [trade for trade in trades if trade.pnl <= 0]
        gross_profit = sum(float(trade.pnl) for trade in wins)
        gross_loss = abs(sum(float(trade.pnl) for trade in losses))

        equity = []
        total = 0.0
        for trade in trades:
            total += float(trade.pnl)
            equity.append(total)

        slippage_values = [float(trade.slippage or 0.0) for trade in trades]
        average_slippage = (
            sum(slippage_values) / len(slippage_values)
            if slippage_values
            else 0.0
        )

        loss_count = len(losses)
        win_loss_ratio = len(wins) / loss_count if loss_count else float(len(wins))

        return {
            "strategy": strategy_name,
            "week_start": week_start.date().isoformat(),
            "week_end": (week_end - timedelta(days=1)).date().isoformat(),
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_loss_ratio": round(win_loss_ratio, 4),
            "win_rate": round((len(wins) / len(trades) * 100.0) if trades else 0.0, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "max_drawdown": round(max_drawdown_from_equity(equity), 4),
            "average_slippage": round(average_slippage, 4),
        }

    def write_weekly_summary(self, strategy_name: str, now: Optional[datetime] = None) -> Dict[str, float]:
        summary = self.weekly_summary(strategy_name, now=now)
        week_key = str(summary["week_start"])
        safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in strategy_name)
        json_path = os.path.join(self.output_dir, f"{safe_name}_{week_key}_summary.json")
        csv_path = os.path.join(self.output_dir, f"{safe_name}_{week_key}_summary.csv")

        with open(json_path, "w", encoding="utf-8") as file_obj:
            json.dump(summary, file_obj, indent=2)

        with open(csv_path, "w", encoding="utf-8", newline="") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=list(summary.keys()))
            writer.writeheader()
            writer.writerow(summary)

        return summary

    def write_all_trades(self, strategy_name: str) -> None:
        trades = self._trades.get(strategy_name, [])
        if not trades:
            return
        safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in strategy_name)
        csv_path = os.path.join(self.output_dir, f"{safe_name}_trades.csv")
        headers = ["strategy_name", "symbol", "entry_time", "exit_time", "side", "quantity", "entry_price", "exit_price", "pnl", "slippage"]
        import csv
        with open(csv_path, "w", encoding="utf-8", newline="") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(headers)
            for t in trades:
                writer.writerow([
                    t.strategy_name,
                    t.symbol,
                    t.entry_time.isoformat() if hasattr(t.entry_time, "isoformat") else str(t.entry_time),
                    t.exit_time.isoformat() if hasattr(t.exit_time, "isoformat") else str(t.exit_time),
                    t.side,
                    t.quantity,
                    t.entry_price,
                    t.exit_price,
                    t.pnl,
                    t.slippage
                ])

    def generate_llm_report(self, engine_config: Dict[str, Any]) -> None:
        report_path = os.path.join(self.output_dir, "llm_analysis_report.md")
        import json

        lines = []
        lines.append("# LLM TRADING STRATEGY ANALYSIS REPORT")
        lines.append("This report contains performance metrics, strategy parameters, and trade-by-trade logs from the recent execution. Pass this entire document to an AI (like Claude or ChatGPT) with the instructions below to get feedback on how to improve your trading performance.")
        lines.append("")
        lines.append("## LLM ANALYSIS PROMPT")
        lines.append("```text")
        lines.append("Analyze the trading performance reports and logs below. Provide a detailed assessment addressing:")
        lines.append("1. Performance & Trade Review: Which strategies are performing best/worst? Analyze win rates, profit factors, and drawdown.")
        lines.append("2. Win/Loss and Risk-Reward: What is the ratio of average win to average loss? How does this compare to the target risk-reward? Why are we winning/losing (e.g. are losses larger than they should be)?")
        lines.append("3. Parameter Optimization: Based on the strategy parameters and the trade logs, what changes should be made to parameters (e.g. ema lengths, atr multipliers, target risk-reward, volume thresholds, cooling periods) to improve profitability?")
        lines.append("4. Worst Strategy Elimination: Identify any strategies that show structural decay and should be disabled.")
        lines.append("5. Execution & Slippage: Analyze the average slippage and suggest how to optimize order entry/exit to reduce transaction costs.")
        lines.append("6. Suggestions for UI/UX improvements to better track these metrics.")
        lines.append("```")
        lines.append("")

        strat_configs = {}
        for s in engine_config.get("strategies", []):
            strat_configs[s.get("name")] = s.get("config", {})

        for name, trades in self._trades.items():
            if not trades:
                continue

            wins = [t for t in trades if t.pnl > 0]
            losses = [t for t in trades if t.pnl <= 0]

            gross_profit = sum(t.pnl for t in wins)
            gross_loss = abs(sum(t.pnl for t in losses))
            net_pnl = gross_profit - gross_loss
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

            avg_win = gross_profit / len(wins) if wins else 0.0
            avg_loss = gross_loss / len(losses) if losses else 0.0
            avg_win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

            equity = []
            total = 0.0
            for t in trades:
                total += t.pnl
                equity.append(total)

            max_dd = max_drawdown_from_equity(equity) if equity else 0.0
            avg_slippage = sum(t.slippage for t in trades) / len(trades) if trades else 0.0

            lines.append(f"## Strategy: {name}")
            lines.append(f"### ⚙️ Parameters & Config")
            lines.append("```json")
            lines.append(json.dumps(strat_configs.get(name, {}), indent=2))
            lines.append("```")
            lines.append("")

            lines.append("### 📊 Performance Summary")
            lines.append(f"- **Total Trades**: {len(trades)}")
            lines.append(f"- **Wins**: {len(wins)} (Win Rate: {len(wins)/len(trades)*100:.2f}%)")
            lines.append(f"- **Losses**: {len(losses)}")
            lines.append(f"- **Gross Profit**: INR {gross_profit:.2f}")
            lines.append(f"- **Gross Loss**: INR {gross_loss:.2f}")
            lines.append(f"- **Net P&L**: INR {net_pnl:.2f}")
            lines.append(f"- **Profit Factor**: {profit_factor:.4f}")
            lines.append(f"- **Avg Win**: INR {avg_win:.2f}")
            lines.append(f"- **Avg Loss**: INR {avg_loss:.2f}")
            lines.append(f"- **Win/Loss Ratio (Avg Win / Avg Loss)**: {avg_win_loss_ratio:.4f}")
            lines.append(f"- **Max Drawdown**: INR {max_dd:.2f}")
            lines.append(f"- **Average Slippage**: INR {avg_slippage:.4f}")
            lines.append("")

            lines.append("### 📜 Trade Log")
            lines.append("| Symbol | Side | Quantity | Entry Time | Exit Time | Entry Price | Exit Price | P&L (INR) | Slippage | Duration (min) |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for t in trades:
                duration_min = 0.0
                try:
                    duration_min = (t.exit_time - t.entry_time).total_seconds() / 60.0
                except Exception:
                    pass

                entry_t_str = t.entry_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t.entry_time, "strftime") else str(t.entry_time)
                exit_t_str = t.exit_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t.exit_time, "strftime") else str(t.exit_time)

                lines.append(
                    f"| {t.symbol} | {t.side} | {t.quantity} | {entry_t_str} | {exit_t_str} | {t.entry_price:.2f} | {t.exit_price:.2f} | {t.pnl:.2f} | {t.slippage:.2f} | {duration_min:.1f} |"
                )
            lines.append("")
            lines.append("---")
            lines.append("")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
