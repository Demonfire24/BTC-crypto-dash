# Higher High Lower Low Strategy — MT5 port

`HigherHighLowerLowStrategy.mq5` is a line-by-line MQL5 port of the Pine Script v4
study *"Higher High Lower Low Strategy"* by LonesomeTheBlue (MPL 2.0).

It reproduces everything the original draws:

* **HH / HL / LH / LL** labels on confirmed swing pivots,
* **support / resistance** lines that start at the pivot bar, extend to the right and
  are closed off as soon as the level changes,
* **trend bar colouring** (`barcolor()` in Pine).

## Install

1. Copy `HigherHighLowerLowStrategy.mq5` into
   `<terminal data folder>/MQL5/Indicators/` (MetaEditor → *File → Open Data Folder*).
2. Open it in MetaEditor and press **F7** to compile.
3. Drag the indicator onto a chart.

## Inputs

| Pine input | MQL5 input | Default |
|---|---|---|
| `lb` — Left Bars | `InpLeftBars` | 5 |
| `rb` — Right Bars | `InpRightBars` | 5 |
| `showsupres` — Support/Resistance | `InpShowSupRes` | true |
| `supcol` | `InpSupColor` | Lime |
| `rescol` | `InpResColor` | Red |
| `srlinestyle` — Line Style | `InpSRLineStyle` | Dotted |
| `srlinewidth` — Line Width | `InpSRLineWidth` | 3 |
| `max_lines_count` (script setting) | `InpMaxLines` | 500 |
| `changebarcol` — Change Bar Color | `InpChangeBarCol` | true |
| `bcolup` | `InpBarColUp` | Blue |
| `bcoldn` | `InpBarColDn` | Black |
| label colours (hard coded in Pine) | `InpLabelUpColor` / `InpLabelDnColor` | Lime / Red |
| — (MT5 needs an explicit size) | `InpLabelFontSize` | 9 |

## Buffers (for `iCustom()`)

| # | Contents |
|---|---|
| 0–3 | trend-coloured candle overlay (open, high, low, close) |
| 4 | colour index: 0 = up trend, 1 = down trend |
| 5 / 6 / 7 / 8 | HH / HL / LL / LH — pivot price on the pivot bar, `EMPTY_VALUE` otherwise |
| 9 | `hl` — +1 high pivot, −1 low pivot, `EMPTY_VALUE` = `na` |
| 10 | `zz` — zig-zag pivot price, `EMPTY_VALUE` = `na` |
| 11 / 12 | `res` / `sup` — the active resistance / support level |
| 13 | `trend` — +1 / −1 / 0 |
| 14–17 | internal state (occurrence chains for `valuewhen()`, active S/R line bars) |

## Behaviour notes

* **Confirmation delay / repainting.** As in Pine, a pivot is only known `rb` bars after
  it happened, and the label is then drawn back on the pivot bar
  (`plotshape(..., offset = -rb)`). The newest pivot can therefore still appear or
  disappear while the current bar is forming — this matches TradingView exactly.
* **Pivot ties.** TradingView compares strictly, so two equal highs produce *no* pivot.
  The port does the same (`>=` / `<=` rejects the candidate).
* **`na` semantics.** Pine's `na` is NaN: every comparison against it is false, while
  `a != b` is true as soon as one side is `na`. The port routes all comparisons through
  `EQ/GT/LT/GE/LE` and `PineNeq` helpers so the edge cases behave identically — including
  the fact that the first level change (`na` → value) does open a line.
* **`valuewhen()`.** Occurrence 0 is the value the variable holds at that point of the
  script on the current bar; occurrence 1 is the previous bar whose finished value was
  not `na`. This is kept intact (see `ValueWhenPrev()`), so the zig-zag filter accepts
  and rejects exactly the same pivots.
* **`findprevious()`.** Ported including its 1000-bar search window and the quirk that a
  loop which finds nothing leaves `xx` at 0, so the next loop starts at the current bar.
* **Line style vs. width.** MetaTrader draws non-solid styles only at width 1. With the
  Pine defaults (dotted, width 3) the S/R lines will therefore look solid — set
  `InpSRLineWidth = 1` if you prefer the dotted look.
* **Bar colouring.** MQL5 cannot recolour the chart's own bars, so the indicator draws a
  coloured candle overlay on top of them. Switch the chart to line mode (or accept the
  overlay) if the underlying candles show through.
* **Chart objects.** Labels and S/R lines are chart objects prefixed `HHLL_<lb>_<rb>_`.
  They are removed when the indicator is detached or its inputs change. Only one instance
  per chart with the same left/right bars setting should be used.

## Verification

`tests/verify.cpp` is a standalone harness that runs the ported algorithm side by side
with a deliberately naive, literal transcription of the Pine source (every `valuewhen()`
re-scans the whole history, nothing is cached). It checks, over randomised and
tie-heavy price series and several left/right bar combinations, that:

* the optimised port and the literal transcription agree on `hl`, `zz`, `res`, `sup`,
  `trend` and all four structure signals, bar for bar;
* incremental recalculation (MT5 re-running the last bar on every tick, including
  in-progress ticks that move the forming bar's high/low) ends in exactly the same state
  as a single full pass — buffers *and* chart objects;
* the S/R line bookkeeping is consistent (one open line per side, correct anchors,
  `max_lines_count` honoured) and at most one label is produced per bar.

```
g++ -O2 -std=c++17 -o verify tests/verify.cpp && ./verify
```
