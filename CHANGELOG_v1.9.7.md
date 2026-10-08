## v1.9.7 — 2026-10-08  (fixes from the live session)
- FIX trend card contradicted the P signal: at 10:55 IST "BULLISH · PULLBACK — watch for a re-entry LONG" was posted while a bearish P was forming.
  A move between EMA 50 and 80 is now called a PULLBACK only when the lines are still apart; when the gap is small and shrinking, or an opposite
  P / cross fired in the last hour, it is **WEAKENING — turn forming, don't buy this dip**. Re-entries in that state are suppressed (journaled as signal_suppressed).
- GUARDIAN CHANGE (P timeout): after 12 bars without the cross the guardian no longer closes a trade that is in profit, or one where you set
  your own TP. It holds, moves SL to entry ±0.5 (if > +1), and posts P TIMEOUT · HOLDING. Losing trades without a TP still close at timeout.
  Case: 8 Oct short 4131.39 closed at +1.46 by the timeout at 11:10 IST; the move reached +11.5 by 11:20.
- Strategy detection thresholds changed: NO. Guardian behaviour changed: YES (timeout). 77 tests.
