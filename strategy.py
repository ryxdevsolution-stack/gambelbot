"""Pattern detection + confirmation + money-management rules.
Shared by backtest.py (replay on history) and bot.py (live/demo).
"""

from dataclasses import dataclass, field


def candle_color(candle):
    if candle["close"] > candle["open"]:
        return "green"
    if candle["close"] < candle["open"]:
        return "red"
    return "doji"


def pattern_at(candles, i):
    """Does candles[i] complete the pattern: 2+ green candles
    immediately followed by 1 red candle at index i?
    Returns the green streak length, or None if no pattern here.
    """
    if candle_color(candles[i]) != "red":
        return None
    streak = 0
    j = i - 1
    while j >= 0 and candle_color(candles[j]) == "green":
        streak += 1
        j -= 1
    return streak if streak >= 2 else None


def confirmation_passed(pattern_log, now, window_seconds=600):
    """pattern_log: chronological list of {'time': int, 'outcome': str}
    for previously resolved pattern instances. True if a prior WIN
    happened within window_seconds before `now`.
    """
    for entry in reversed(pattern_log):
        if entry["time"] < now - window_seconds:
            break
        if entry["outcome"] == "win":
            return True
    return False


@dataclass
class MoneyManager:
    """$1 base stake -> martingale $2 on loss -> reset to $1 on win
    or after two straight losses. Daily target/stop halt trading."""

    base_stake: float = 1.0
    martingale_stake: float = 2.0
    daily_target: float = 2.5
    daily_stop: float = -9.0

    stake: float = field(init=False)
    martingale_step: int = field(init=False, default=0)
    daily_pnl: float = field(init=False, default=0.0)
    day_stopped: bool = field(init=False, default=False)

    def __post_init__(self):
        self.stake = self.base_stake

    def new_day(self):
        self.daily_pnl = 0.0
        self.day_stopped = False
        self.martingale_step = 0
        self.stake = self.base_stake

    def can_trade(self):
        return not self.day_stopped

    def record_result(self, won, payout_rate):
        if won:
            self.daily_pnl += self.stake * payout_rate
            self.martingale_step = 0
            self.stake = self.base_stake
        else:
            self.daily_pnl -= self.stake
            if self.martingale_step == 0:
                self.martingale_step = 1
                self.stake = self.martingale_stake
            else:
                self.martingale_step = 0
                self.stake = self.base_stake

        if self.daily_pnl >= self.daily_target or self.daily_pnl <= self.daily_stop:
            self.day_stopped = True
