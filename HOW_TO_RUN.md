# How to Run the Signal Bot

## 1. Start the bot

```bash
cd /var/www/others/quotex-bot
docker run --rm -it --env-file .env -v "$(pwd):/app" quotex-bot python3 live_signal_bot.py
```

- It may ask for a PIN code — check your email and enter it.
- Once connected, it watches 10 real markets on 5-min candles.
- Leave it running. Signals arrive on Telegram automatically.

## 2. If you get "authorization/reject" or "Connection failed"

This means a stale login file needs clearing. Run this, then retry step 1:

```bash
cd /var/www/others/quotex-bot
rm -f session.json
```

## 3. The pattern it looks for

- Watches for **2 or more green candles in a row, then 1 red candle** (the trigger).
- Only sends a signal if the **previous time** this same shape happened, it won cleanly on the very next candle (no martingale needed).
- If both conditions are true, it signals **DOWN** on the candle right after the trigger.

Example: `G, G, R` forms → check the last time `G,G,R` (or longer) happened → if that one's very next candle was also red (a clean win) → signal fires now.

## 4. Reading a signal

```
SIGNAL: AUDCHF  TRADE DOWN (PUT)
Trigger candle: 13:10 IST (closing now)
Entry candle: 13:15 IST -- trade THIS one
```

- Open that asset on Quotex, set chart to **5 minutes**.
- Trade **DOWN**, expiry **5 minutes**, on the **"Entry candle"** time shown.
- Place it fast — the alert comes ~5 seconds before that candle starts.

## 5. Auto-trade (optional, DEMO only)

By default the bot only sends alerts — you place the trade yourself. To have it place the ₹100 DEMO trade automatically instead:

```bash
cd /var/www/others/quotex-bot
docker run --rm -it --env-file .env -e AUTO_TRADE_DEMO=1 -v "$(pwd):/app" quotex-bot python3 live_signal_bot.py
```

- Only works on your **DEMO** account — there is no real-money auto-trade option.
- P&L shown is the actual result from your account, not an estimate.

## 6. Stopping

- Press `Ctrl+C` in the terminal anytime to stop the bot.
- It does not run unattended — start it fresh each session.

## 7. Daily safety stop

- Stake is **₹100** per trade.
- If the running total hits **-₹500** for the day (5 losing trades), it messages "STOP for today" and stops sending new signals until the next day.

## Notes

- This pattern is not proven to make money — treat results as a live test, not a guarantee.
- Signal-only mode never places trades for you. Auto-trade mode only ever touches the DEMO account.
