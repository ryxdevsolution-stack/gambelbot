"""Phase 2: live/demo trading bot. Only build this out once
backtest.py shows real positive expectancy over a large sample.
Connects to Quotex demo account, applies strategy.py rules,
enforces daily_target/daily_stop as hard limits before every trade,
sends Telegram notifications per trade and at day-end.
"""

from dotenv import load_dotenv

from strategy import detect_pattern, confirmation_passed, MoneyManager

load_dotenv()


def main():
    raise NotImplementedError


if __name__ == "__main__":
    main()
