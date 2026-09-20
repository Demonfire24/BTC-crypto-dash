//+------------------------------------------------------------------+
//|                                 HigherHighLowerLowStrategy.mq5   |
//|                                                                  |
//|  MQL5 port of the Pine Script v4 study                           |
//|  "Higher High Lower Low Strategy" (c) LonesomeTheBlue.           |
//|                                                                  |
//|  This source code is subject to the terms of the Mozilla Public  |
//|  License 2.0 at https://mozilla.org/MPL/2.0/                     |
//|                                                                  |
//|  Every branch of the original script is reproduced 1:1:          |
//|    * pivothigh()/pivotlow() confirmation and repainting window   |
//|    * the zig-zag filter (lines 22..27 of the Pine source)        |
//|    * findprevious() incl. its 1000 bar search window and its     |
//|      "xx == 0" fall-through quirk                                |
//|    * the HH / HL / LH / LL classification                        |
//|    * res / sup carry-forward, trend state and line management    |
//|    * barcolor() (drawn here as a coloured candle overlay)        |
//+------------------------------------------------------------------+
#property copyright "MQL5 port of the Pine v4 study by LonesomeTheBlue (MPL 2.0)"
#property link      "https://mozilla.org/MPL/2.0/"
#property version   "1.00"
#property description "Higher High Lower Low Strategy - MQL5 port of the Pine Script v4 study."
#property description "Marks HH / HL / LH / LL swing structure, draws support & resistance"
#property description "lines and colours the bars by trend."

#property indicator_chart_window
#property indicator_buffers 18
#property indicator_plots   5

//--- plot 1: trend coloured candles -> Pine barcolor()
#property indicator_label1  "HHLL open;HHLL high;HHLL low;HHLL close"
#property indicator_type1   DRAW_COLOR_CANDLES
#property indicator_color1  clrBlue,clrBlack
#property indicator_style1  STYLE_SOLID
#property indicator_width1  1
//--- plots 2..5: the four structure signals -> Pine plotshape() series
#property indicator_label2  "HH"
#property indicator_type2   DRAW_NONE
#property indicator_label3  "HL"
#property indicator_type3   DRAW_NONE
#property indicator_label4  "LL"
#property indicator_type4   DRAW_NONE
#property indicator_label5  "LH"
#property indicator_type5   DRAW_NONE

//--- Pine offers exactly these three line styles in the "Line Style" dropdown
enum ENUM_PINE_LINE_STYLE
  {
   PineStyleSolid  = 0,             // Solid       (STYLE_SOLID)
   PineStyleDashed = 1,             // Dashed      (STYLE_DASH)
   PineStyleDotted = 2              // Dotted      (STYLE_DOT)
  };

//--- inputs (same order / defaults as the Pine inputs).
//--- "input group" is deliberately not used: it shows up as an extra
//--- parameter in iCustom() calls on several MT5 builds.
input int                  InpLeftBars     = 5;              // Left Bars
input int                  InpRightBars    = 5;              // Right Bars
//--- support / resistance
input bool                 InpShowSupRes   = true;           // Support/Resistance
input color                InpSupColor     = clrLime;        // S/R: support colour
input color                InpResColor     = clrRed;         // S/R: resistance colour
input ENUM_PINE_LINE_STYLE InpSRLineStyle  = PineStyleDotted;// S/R: line style
input int                  InpSRLineWidth  = 3;              // S/R: line width (1..5)
input int                  InpMaxLines     = 500;            // S/R: max lines kept (max_lines_count)
//--- bar colouring
input bool                 InpChangeBarCol = true;           // Change Bar Color
input color                InpBarColUp     = clrBlue;        // Bar colour, up trend
input color                InpBarColDn     = clrBlack;       // Bar colour, down trend
//--- labels
input color                InpLabelUpColor = clrLime;        // Label colour: HH / HL
input color                InpLabelDnColor = clrRed;         // Label colour: LL / LH
input int                  InpLabelFontSize= 9;              // Label font size

//--- plot buffers ----------------------------------------------------
double BufOpen[];      // 0
double BufHigh[];      // 1
double BufLow[];       // 2
double BufClose[];     // 3
double BufColor[];     // 4
double BufHH[];        // 5  price of the pivot flagged as Higher High
double BufHL[];        // 6  price of the pivot flagged as Higher Low
double BufLL[];        // 7  price of the pivot flagged as Lower Low
double BufLH[];        // 8  price of the pivot flagged as Lower High
//--- calculation buffers (they also carry the state between ticks) ---
double BufHl[];        // 9  Pine "hl"    : +1 high pivot, -1 low pivot, EMPTY_VALUE = na
double BufZz[];        //10  Pine "zz"    : pivot price, EMPTY_VALUE = na
double BufRes[];       //11  Pine "res"
double BufSup[];       //12  Pine "sup"
double BufTrend[];     //13  Pine "trend" : +1 / -1 / 0
double BufHlChain[];   //14  index of the newest bar <= i with a non-na hl (else -1)
double BufZzChain[];   //15  index of the newest bar <= i with a non-na zz (else -1)
double BufResLine[];   //16  bar that created the resistance line active on bar i (else -1)
double BufSupLine[];   //17  bar that created the support line active on bar i (else -1)

//--- globals ---------------------------------------------------------
#define PINE_NA EMPTY_VALUE

int    g_lb        = 5;      // left bars  (validated)
int    g_rb        = 5;      // right bars (validated)
int    g_lineWidth = 3;      // S/R line width (validated)
int    g_maxLines  = 500;    // Pine max_lines_count
int    g_fontSize  = 9;      // label font size (validated)
string g_prefix    = "HHLL_";

string g_lineName[];         // S/R objects, oldest first (Pine's line pool)
int    g_lineBar[];          // bar that created g_lineName[k]

//+------------------------------------------------------------------+
//| na aware helpers - Pine treats any comparison with na as false,   |
//| so every comparison of the script goes through these.             |
//+------------------------------------------------------------------+
bool IsNa(const double v)              { return(v == PINE_NA || !MathIsValidNumber(v)); }
//--- Pine truthiness of a number: not na and not zero
bool Truthy(const double v)            { return(!IsNa(v) && v != 0.0); }
bool EQ(const double a,const double b) { return(!IsNa(a) && !IsNa(b) && a == b); }
bool GT(const double a,const double b) { return(!IsNa(a) && !IsNa(b) && a >  b); }
bool LT(const double a,const double b) { return(!IsNa(a) && !IsNa(b) && a <  b); }
bool GE(const double a,const double b) { return(!IsNa(a) && !IsNa(b) && a >= b); }
bool LE(const double a,const double b) { return(!IsNa(a) && !IsNa(b) && a <= b); }
//--- Pine na is NaN, so "a != b" is true as soon as one side is na
bool PineNeq(const double a,const double b)
  {
   if(IsNa(a) || IsNa(b))
      return(true);
   return(a != b);
  }
double Nz(const double v)              { return(IsNa(v) ? 0.0 : v); }

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_lb        = (InpLeftBars  < 1 ? 1 : InpLeftBars);     // Pine minval = 1
   g_rb        = (InpRightBars < 1 ? 1 : InpRightBars);    // Pine minval = 1
   g_lineWidth = InpSRLineWidth;                           // Pine minval 1 / maxval 5
   if(g_lineWidth < 1)
      g_lineWidth = 1;
   if(g_lineWidth > 5)
      g_lineWidth = 5;
   g_maxLines  = (InpMaxLines      < 1 ? 1 : InpMaxLines);
   g_fontSize  = (InpLabelFontSize < 1 ? 1 : InpLabelFontSize);

   SetIndexBuffer(0, BufOpen,     INDICATOR_DATA);
   SetIndexBuffer(1, BufHigh,     INDICATOR_DATA);
   SetIndexBuffer(2, BufLow,      INDICATOR_DATA);
   SetIndexBuffer(3, BufClose,    INDICATOR_DATA);
   SetIndexBuffer(4, BufColor,    INDICATOR_COLOR_INDEX);
   SetIndexBuffer(5, BufHH,       INDICATOR_DATA);
   SetIndexBuffer(6, BufHL,       INDICATOR_DATA);
   SetIndexBuffer(7, BufLL,       INDICATOR_DATA);
   SetIndexBuffer(8, BufLH,       INDICATOR_DATA);
   SetIndexBuffer(9, BufHl,       INDICATOR_CALCULATIONS);
   SetIndexBuffer(10, BufZz,      INDICATOR_CALCULATIONS);
   SetIndexBuffer(11, BufRes,     INDICATOR_CALCULATIONS);
   SetIndexBuffer(12, BufSup,     INDICATOR_CALCULATIONS);
   SetIndexBuffer(13, BufTrend,   INDICATOR_CALCULATIONS);
   SetIndexBuffer(14, BufHlChain, INDICATOR_CALCULATIONS);
   SetIndexBuffer(15, BufZzChain, INDICATOR_CALCULATIONS);
   SetIndexBuffer(16, BufResLine, INDICATOR_CALCULATIONS);
   SetIndexBuffer(17, BufSupLine, INDICATOR_CALCULATIONS);

   for(int p = 0; p < 5; p++)
      PlotIndexSetDouble(p, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(0, PLOT_LINE_COLOR, 0, InpBarColUp);  // colour index 0 -> up trend
   PlotIndexSetInteger(0, PLOT_LINE_COLOR, 1, InpBarColDn);  // colour index 1 -> down trend

   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);
   IndicatorSetString(INDICATOR_SHORTNAME,
                      StringFormat("HH/LL Strategy (%d,%d)", g_lb, g_rb));

   g_prefix = StringFormat("HHLL_%d_%d_", g_lb, g_rb);
   ArrayFree(g_lineName);
   ArrayFree(g_lineBar);
   ObjectsDeleteAll(0, g_prefix);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, g_prefix);
   ArrayFree(g_lineName);
   ArrayFree(g_lineBar);
   ChartRedraw();
  }

//+------------------------------------------------------------------+
//| pivothigh(lb, rb) - value of the pivot that sits rb bars back.    |
//| TradingView compares strictly, so equal highs produce no pivot.   |
//+------------------------------------------------------------------+
double PivotHigh(const double &h[], const int i)
  {
   int p = i - g_rb;                       // candidate bar
   if(p - g_lb < 0)
      return(PINE_NA);                     // not enough history yet
   double v = h[p];
   for(int k = p - g_lb; k < p; k++)
      if(h[k] >= v)
         return(PINE_NA);
   for(int k = p + 1; k <= i; k++)
      if(h[k] >= v)
         return(PINE_NA);
   return(v);
  }

//+------------------------------------------------------------------+
//| pivotlow(lb, rb)                                                  |
//+------------------------------------------------------------------+
double PivotLow(const double &l[], const int i)
  {
   int p = i - g_rb;
   if(p - g_lb < 0)
      return(PINE_NA);
   double v = l[p];
   for(int k = p - g_lb; k < p; k++)
      if(l[k] <= v)
         return(PINE_NA);
   for(int k = p + 1; k <= i; k++)
      if(l[k] <= v)
         return(PINE_NA);
   return(v);
  }

//+------------------------------------------------------------------+
//| valuewhen(series, series, 1)                                      |
//|                                                                   |
//| Pine looks the series up in its history buffer: occurrence 0 is    |
//| the value the variable holds at this point of the script on the    |
//| current bar, occurrence 1 the previous bar whose (finished) value  |
//| was not na. p1 / p2 are the two newest such bars before bar i.     |
//+------------------------------------------------------------------+
double ValueWhenPrev(const double current, const int p1, const int p2,
                     const double &hist[])
  {
   if(Truthy(current))                     // the current bar is occurrence 0
      return(p1 >= 0 ? hist[p1] : PINE_NA);
   return(p2 >= 0 ? hist[p2] : PINE_NA);   // ... otherwise p1 is occurrence 0
  }

//+------------------------------------------------------------------+
//| hl[x] / zz[x] with x == 0 meaning "the value of the current bar"  |
//+------------------------------------------------------------------+
bool HistAt(const int i, const int x, const double curHl, const double curZz,
            const double &hlHist[], const double &zzHist[],
            double &hx, double &zx)
  {
   if(x == 0)
     {
      hx = curHl;
      zx = curZz;
      return(true);
     }
   int idx = i - x;
   if(idx < 0)
      return(false);                       // out of history -> na
   hx = hlHist[idx];
   zx = zzHist[idx];
   return(true);
  }

//+------------------------------------------------------------------+
//| findprevious() - the previous three/four pivots (b, c, d, e).     |
//| Mirrors the Pine loops, including the 1..1000 bar window and the  |
//| fact that xx stays 0 when a loop finds nothing (the next loop     |
//| then starts at offset 0, i.e. at the current bar).                |
//+------------------------------------------------------------------+
void FindPrevious(const int i, const double curHl, const double curZz,
                  const double &hlHist[], const double &zzHist[],
                  double &loc1, double &loc2, double &loc3, double &loc4)
  {
   double ehl = (curHl == 1.0) ? -1.0 : 1.0;
   loc1 = 0.0;
   loc2 = 0.0;
   loc3 = 0.0;
   loc4 = 0.0;
   int    xx = 0;
   double hx = 0.0, zx = 0.0;

   for(int x = 1; x <= 1000; x++)
      if(HistAt(i, x, curHl, curZz, hlHist, zzHist, hx, zx) && hx == ehl && !IsNa(zx))
        {
         loc1 = zx;
         xx   = x + 1;
         break;
        }

   ehl = curHl;
   for(int x = xx; x <= 1000; x++)
      if(HistAt(i, x, curHl, curZz, hlHist, zzHist, hx, zx) && hx == ehl && !IsNa(zx))
        {
         loc2 = zx;
         xx   = x + 1;
         break;
        }

   ehl = (curHl == 1.0) ? -1.0 : 1.0;
   for(int x = xx; x <= 1000; x++)
      if(HistAt(i, x, curHl, curZz, hlHist, zzHist, hx, zx) && hx == ehl && !IsNa(zx))
        {
         loc3 = zx;
         xx   = x + 1;
         break;
        }

   ehl = curHl;
   for(int x = xx; x <= 1000; x++)
      if(HistAt(i, x, curHl, curZz, hlHist, zzHist, hx, zx) && hx == ehl && !IsNa(zx))
        {
         loc4 = zx;
         break;
        }
  }

//+------------------------------------------------------------------+
//| Chart objects: HH / HL / LL / LH labels                           |
//+------------------------------------------------------------------+
void DrawLabel(const int signalBar, const datetime t, const double price,
               const string txt, const bool above, const color clr)
  {
   string name = g_prefix + "L" + IntegerToString(signalBar);
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_TEXT, 0, t, price);
   ObjectMove(0, name, 0, t, price);
   ObjectSetString(0, name, OBJPROP_TEXT, txt);
   ObjectSetString(0, name, OBJPROP_FONT, "Arial Black");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE,   g_fontSize);
   ObjectSetInteger(0, name, OBJPROP_COLOR,      clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR,     above ? ANCHOR_LOWER : ANCHOR_UPPER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTED,   false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
   ObjectSetInteger(0, name, OBJPROP_BACK,       false);
  }

//+------------------------------------------------------------------+
//| Chart objects: support / resistance lines                         |
//+------------------------------------------------------------------+
string SRLineName(const bool isRes, const int bar)
  {
   return(g_prefix + (isRes ? "R" : "S") + IntegerToString(bar));
  }

//--- line.new(x1 = bar_index - rb, x2 = bar_index, extend = extend.right)
void CreateSRLine(const bool isRes, const int bar, const datetime t1,
                  const datetime t2, const double price)
  {
   string name = SRLineName(isRes, bar);
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_TREND, 0, t1, price, t2, price);
   ObjectMove(0, name, 0, t1, price);
   ObjectMove(0, name, 1, t2, price);
   ObjectSetInteger(0, name, OBJPROP_COLOR,      isRes ? InpResColor : InpSupColor);
   ObjectSetInteger(0, name, OBJPROP_STYLE,      (ENUM_LINE_STYLE)InpSRLineStyle);
   ObjectSetInteger(0, name, OBJPROP_WIDTH,      g_lineWidth);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT,   false);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT,  true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTED,   false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
   ObjectSetInteger(0, name, OBJPROP_BACK,       true);
   RegisterLine(name, bar);
  }

//--- line.set_x2(old, bar_index) + line.set_extend(old, extend.none)
void FinishSRLine(const bool isRes, const int bar, const datetime t2)
  {
   string name = SRLineName(isRes, bar);
   if(ObjectFind(0, name) < 0)
      return;
   ObjectSetInteger(0, name, OBJPROP_TIME, 1, t2);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
  }

//--- undo FinishSRLine() when the bar that closed the line is recalculated
//--- (t2 goes back to the line's own bar, exactly as CreateSRLine() left it)
void ReopenSRLine(const bool isRes, const int bar, const datetime t2)
  {
   string name = SRLineName(isRes, bar);
   if(ObjectFind(0, name) < 0)
      return;
   ObjectSetInteger(0, name, OBJPROP_TIME, 1, t2);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, true);
  }

//--- keep at most g_maxLines objects, exactly like Pine's line pool
void RegisterLine(const string name, const int bar)
  {
   int n = ArraySize(g_lineName);
   for(int k = n - 1; k >= 0; k--)           // already registered? (re-drawn bar)
     {
      if(g_lineBar[k] < bar)
         break;
      if(g_lineName[k] == name)
         return;
     }
   ArrayResize(g_lineName, n + 1);
   ArrayResize(g_lineBar,  n + 1);
   g_lineName[n] = name;
   g_lineBar[n]  = bar;

   int over = (n + 1) - g_maxLines;
   if(over > 0)
     {
      for(int k = 0; k < over; k++)
         ObjectDelete(0, g_lineName[k]);
      ArrayRemove(g_lineName, 0, over);
      ArrayRemove(g_lineBar,  0, over);
     }
  }

//--- drop the objects that belong to bars which are about to be redone
void TruncateLines(const int fromBar)
  {
   for(int k = ArraySize(g_lineBar) - 1; k >= 0; k--)
     {
      if(g_lineBar[k] < fromBar)
         break;
      ObjectDelete(0, g_lineName[k]);
      ArrayRemove(g_lineName, k, 1);
      ArrayRemove(g_lineBar,  k, 1);
     }
  }

//+------------------------------------------------------------------+
//| OnCalculate                                                      |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   if(rates_total < g_lb + g_rb + 2)
      return(0);

   ArraySetAsSeries(time,  false);
   ArraySetAsSeries(open,  false);
   ArraySetAsSeries(high,  false);
   ArraySetAsSeries(low,   false);
   ArraySetAsSeries(close, false);

   int start;
   if(prev_calculated <= 0 || prev_calculated > rates_total)
     {
      //--- full recalculation: start from scratch
      ObjectsDeleteAll(0, g_prefix);
      ArrayFree(g_lineName);
      ArrayFree(g_lineBar);
      start = 0;
     }
   else
     {
      //--- redo the last finished bar as well (trend/res/sup follow close,
      //--- and the newest pivot is still inside its confirmation window)
      start = (prev_calculated > 1 ? prev_calculated - 1 : 0);
      TruncateLines(start);
      for(int j = start; j < rates_total; j++)
         ObjectDelete(0, g_prefix + "L" + IntegerToString(j));
      if(start > 0)
        {
         int rl = (int)BufResLine[start - 1];
         if(rl >= 0)
            ReopenSRLine(true, rl, time[rl]);
         int sl = (int)BufSupLine[start - 1];
         if(sl >= 0)
            ReopenSRLine(false, sl, time[sl]);
        }
     }

   for(int i = start; i < rates_total; i++)
     {
      BufHH[i] = EMPTY_VALUE;
      BufHL[i] = EMPTY_VALUE;
      BufLL[i] = EMPTY_VALUE;
      BufLH[i] = EMPTY_VALUE;

      //--- ph = pivothigh(lb, rb) / pl = pivotlow(lb, rb)
      double ph = PivotHigh(high, i);
      double pl = PivotLow(low, i);

      //--- the two newest finished occurrences, used by valuewhen()
      int hlP1 = (i > 0) ? (int)BufHlChain[i - 1] : -1;
      int hlP2 = (hlP1 > 0) ? (int)BufHlChain[hlP1 - 1] : -1;
      int zzP1 = (i > 0) ? (int)BufZzChain[i - 1] : -1;
      int zzP2 = (zzP1 > 0) ? (int)BufZzChain[zzP1 - 1] : -1;

      //--- hl = iff(ph, 1, iff(pl, -1, na))          (trend direction)
      //--- zz = iff(ph, ph, iff(pl, pl, na))         (zig-zag value)
      double hl = Truthy(ph) ?  1.0 : (Truthy(pl) ? -1.0 : PINE_NA);
      double zz = Truthy(ph) ?  ph  : (Truthy(pl) ?  pl  : PINE_NA);

      //--- zz := iff(pl and hl == -1 and valuewhen(hl,hl,1) == -1 and
      //---           pl > valuewhen(zz,zz,1), na, zz)
      if(Truthy(pl) && EQ(hl, -1.0)
         && EQ(ValueWhenPrev(hl, hlP1, hlP2, BufHl), -1.0)
         && GT(pl, ValueWhenPrev(zz, zzP1, zzP2, BufZz)))
         zz = PINE_NA;

      //--- zz := iff(ph and hl == 1 and valuewhen(hl,hl,1) == 1 and
      //---           ph < valuewhen(zz,zz,1), na, zz)
      if(Truthy(ph) && EQ(hl, 1.0)
         && EQ(ValueWhenPrev(hl, hlP1, hlP2, BufHl), 1.0)
         && LT(ph, ValueWhenPrev(zz, zzP1, zzP2, BufZz)))
         zz = PINE_NA;

      //--- hl := iff(hl == -1 and valuewhen(hl,hl,1) == 1 and
      //---           zz > valuewhen(zz,zz,1), na, hl)
      if(EQ(hl, -1.0)
         && EQ(ValueWhenPrev(hl, hlP1, hlP2, BufHl), 1.0)
         && GT(zz, ValueWhenPrev(zz, zzP1, zzP2, BufZz)))
         hl = PINE_NA;

      //--- hl := iff(hl == 1 and valuewhen(hl,hl,1) == -1 and
      //---           zz < valuewhen(zz,zz,1), na, hl)
      if(EQ(hl, 1.0)
         && EQ(ValueWhenPrev(hl, hlP1, hlP2, BufHl), -1.0)
         && LT(zz, ValueWhenPrev(zz, zzP1, zzP2, BufZz)))
         hl = PINE_NA;

      //--- zz := iff(na(hl), na, zz)
      if(IsNa(hl))
         zz = PINE_NA;

      //--- float a = na, b = na, c = na, d = na, e = na
      //--- if not na(hl) : [loc1..loc4] = findprevious()
      double a = PINE_NA, b = PINE_NA, c = PINE_NA, d = PINE_NA, e = PINE_NA;
      if(!IsNa(hl))
        {
         double l1, l2, l3, l4;
         FindPrevious(i, hl, zz, BufHl, BufZz, l1, l2, l3, l4);
         a = zz;
         b = l1;
         c = l2;
         d = l3;
         e = l4;
        }

      //--- the four structure conditions
      bool _hh = Truthy(zz) && (GT(a, b) && GT(a, c) && GT(c, b) && GT(c, d));
      bool _ll = Truthy(zz) && (LT(a, b) && LT(a, c) && LT(c, b) && LT(c, d));
      bool _hl = Truthy(zz) && ((GE(a, c) && (GT(b, c) && GT(b, d) && GT(d, c) && GT(d, e)))
                                || (LT(a, b) && GT(a, c) && LT(b, d)));
      bool _lh = Truthy(zz) && ((LE(a, c) && (LT(b, c) && LT(b, d) && LT(d, c) && LT(d, e)))
                                || (GT(a, b) && LT(a, c) && GT(b, d)));

      //--- plotshape(..., offset = -rb): the label belongs to the pivot bar
      int p = i - g_rb;
      if(p >= 0)
        {
         BufHH[p] = _hh ? zz : EMPTY_VALUE;
         BufHL[p] = _hl ? zz : EMPTY_VALUE;
         BufLL[p] = _ll ? zz : EMPTY_VALUE;
         BufLH[p] = _lh ? zz : EMPTY_VALUE;

         if(_hh)
            DrawLabel(i, time[p], high[p], "HH", true,  InpLabelUpColor);
         else
            if(_hl)
               DrawLabel(i, time[p], low[p],  "HL", false, InpLabelUpColor);
            else
               if(_ll)
                  DrawLabel(i, time[p], low[p],  "LL", false, InpLabelDnColor);
               else
                  if(_lh)
                     DrawLabel(i, time[p], high[p], "LH", true,  InpLabelDnColor);
        }

      //--- res := iff(_lh, zz, res[1]) / sup := iff(_hl, zz, sup[1])
      double prevRes = (i > 0) ? BufRes[i - 1] : PINE_NA;
      double prevSup = (i > 0) ? BufSup[i - 1] : PINE_NA;
      double res = _lh ? zz : prevRes;
      double sup = _hl ? zz : prevSup;

      //--- trend := iff(close > res, 1, iff(close < sup, -1, nz(trend[1])))
      double trend;
      if(GT(close[i], res))
         trend = 1.0;
      else
         if(LT(close[i], sup))
            trend = -1.0;
         else
            trend = (i > 0) ? Nz(BufTrend[i - 1]) : 0.0;

      //--- res := iff((trend == 1 and _hh) or (trend == -1 and _lh), zz, res)
      //--- sup := iff((trend == 1 and _hl) or (trend == -1 and _ll), zz, sup)
      if((trend == 1.0 && _hh) || (trend == -1.0 && _lh))
         res = zz;
      if((trend == 1.0 && _hl) || (trend == -1.0 && _ll))
         sup = zz;

      //--- rechange = res != res[1] / suchange = sup != sup[1]
      bool rechange = PineNeq(res, prevRes);
      bool suchange = PineNeq(sup, prevSup);

      //--- line handling (a na level cannot be drawn, and in Pine such a
      //--- line is invisible as well, so the chart output is identical)
      int curResLine = (i > 0) ? (int)BufResLine[i - 1] : -1;
      int curSupLine = (i > 0) ? (int)BufSupLine[i - 1] : -1;
      if(InpShowSupRes)
        {
         int      x1 = (p > 0 ? p : 0);                // x1 = bar_index - rb
         datetime t1 = time[x1];
         if(rechange && !IsNa(res))
           {
            if(curResLine >= 0)
               FinishSRLine(true, curResLine, time[i]);
            CreateSRLine(true, i, t1, time[i], res);
            curResLine = i;
           }
         if(suchange && !IsNa(sup))
           {
            if(curSupLine >= 0)
               FinishSRLine(false, curSupLine, time[i]);
            CreateSRLine(false, i, t1, time[i], sup);
            curSupLine = i;
           }
        }

      //--- barcolor(iff(changebarcol, iff(trend == 1, bcolup, bcoldn), na))
      if(InpChangeBarCol)
        {
         BufOpen[i]  = open[i];
         BufHigh[i]  = high[i];
         BufLow[i]   = low[i];
         BufClose[i] = close[i];
         BufColor[i] = (trend == 1.0) ? 0.0 : 1.0;
        }
      else
        {
         BufOpen[i]  = EMPTY_VALUE;
         BufHigh[i]  = EMPTY_VALUE;
         BufLow[i]   = EMPTY_VALUE;
         BufClose[i] = EMPTY_VALUE;
         BufColor[i] = 0.0;
        }

      //--- commit this bar
      BufHl[i]      = hl;
      BufZz[i]      = zz;
      BufRes[i]     = res;
      BufSup[i]     = sup;
      BufTrend[i]   = trend;
      BufResLine[i] = curResLine;
      BufSupLine[i] = curSupLine;
      BufHlChain[i] = Truthy(hl) ? (double)i : ((i > 0) ? BufHlChain[i - 1] : -1.0);
      BufZzChain[i] = Truthy(zz) ? (double)i : ((i > 0) ? BufZzChain[i - 1] : -1.0);
     }

   ChartRedraw();
   return(rates_total);
  }
//+------------------------------------------------------------------+
