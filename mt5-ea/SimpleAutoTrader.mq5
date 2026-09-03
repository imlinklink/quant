//+------------------------------------------------------------------+
//|                                                  SimpleAutoTrader |
//|          自动下单 + 止盈止损示例 EA（双均线交叉策略）             |
//+------------------------------------------------------------------+
#property copyright "Personal Use"
#property link      ""
#property version   "1.00"
#property description "自动监控行情，按双均线交叉信号下单；"
#property description "下单时同时挂上止盈止损（经纪商端执行，断线依然有效）；"
#property description "可选移动止损、按风险百分比计算手数、当日最大亏损熔断。"

#include <Trade\Trade.mqh>

//--- 自定义枚举
enum ENUM_TRADE_MODE
  {
   TRADE_MODE_BOTH  = 0,   // 多空都做
   TRADE_MODE_LONG  = 1,   // 只做多
   TRADE_MODE_SHORT = 2    // 只做空
  };

enum ENUM_LOT_MODE
  {
   LOT_MODE_FIXED        = 0,  // 固定手数
   LOT_MODE_RISK_PERCENT = 1   // 按账户余额百分比计算风险
  };

//--- 交易设置
input group "交易设置"
input string          InpSymbol            = "";                // 交易品种(留空=当前图表)
input long            InpMagic             = 20260901;          // 魔术数字(识别自己的订单)
input ENUM_TRADE_MODE InpTradeMode         = TRADE_MODE_BOTH;   // 交易方向
input int             InpMaxPositions      = 1;                 // 同品种最大持仓数
input int             InpDeviationPoints   = 20;                // 最大滑点(点)
input int             InpMaxSpreadPoints   = 60;                // 最大点差(点, 0=不限)
input int             InpTradeStartHour    = 0;                 // 允许交易开始小时(0=全天)
input int             InpTradeEndHour      = 0;                 // 允许交易结束小时(0=全天)

//--- 策略参数（双均线示例，可替换成你的策略）
input group "策略参数(示例:双均线交叉)"
input int               InpFastMAPeriod  = 10;                  // 快线周期
input int               InpSlowMAPeriod  = 30;                  // 慢线周期
input ENUM_MA_METHOD    InpMAMethod      = MODE_EMA;            // 均线算法
input ENUM_APPLIED_PRICE InpMAPrice      = PRICE_CLOSE;         // 应用价格

//--- 仓位与风险
input group "仓位与风险"
input ENUM_LOT_MODE InpLotMode            = LOT_MODE_RISK_PERCENT; // 手数模式
input double        InpFixedLot           = 0.01;               // 固定手数(固定模式用)
input double        InpRiskPercent        = 1.0;                // 每单风险(占余额%)
input double        InpMaxDailyLossPercent = 0.0;               // 当日最大亏损熔断%(0=关闭)

//--- 止盈止损
input group "止盈止损"
input int           InpSLPoints           = 300;                // 止损点数(0=不止损)
input int           InpTPPoints           = 600;                // 止盈点数(0=不止盈)
input int           InpTrailingStartPoints = 0;                 // 移动止损启动点数(0=关闭)
input int           InpTrailingStepPoints  = 100;               // 移动止损回撤点数

//--- LLM 桥接（人工确认模式）
input group "LLM 桥接(人工确认)"
input bool          InpUseLLMBridge       = true;               // 使用LLM桥接决策(替代双均线信号)
input string        InpLLMFolder          = "llm_bridge";       // 桥接文件夹(相对 MQL5\Files)

//--- 全局对象
CTrade   g_trade;
string   g_symbol;
long     g_magic;
int      g_fastMA = INVALID_HANDLE;
int      g_slowMA = INVALID_HANDLE;
datetime g_lastBarTime  = 0;
datetime g_dayStart     = 0;
double   g_dayStartEquity = 0.0;
bool     g_stopTrading    = false;
long     g_lastLLMSignalId = 0;
string   g_llmFolder       = "llm_bridge";

//--- LLM 决策结构
struct LLMDecision
  {
   long    id;
   string  status;
   string  action;
   double  lot;
   double  sl;
   double  tp;
   double  confidence;
  };

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_symbol = (InpSymbol == "") ? _Symbol : InpSymbol;
   g_magic  = InpMagic;

   if(!SymbolSelect(g_symbol, true))
     {
      PrintFormat("无法选择品种 %s", g_symbol);
      return INIT_FAILED;
     }

   ENUM_SYMBOL_TRADE_MODE mode = (ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(g_symbol, SYMBOL_TRADE_MODE);
   if(mode == SYMBOL_TRADE_MODE_DISABLED)
     {
      PrintFormat("品种 %s 不允许交易", g_symbol);
      return INIT_FAILED;
     }

   g_trade.SetExpertMagicNumber(g_magic);
   g_trade.SetDeviationInPoints(InpDeviationPoints);

   g_fastMA = iMA(g_symbol, PERIOD_CURRENT, InpFastMAPeriod, 0, InpMAMethod, InpMAPrice);
   g_slowMA = iMA(g_symbol, PERIOD_CURRENT, InpSlowMAPeriod, 0, InpMAMethod, InpMAPrice);
   if(g_fastMA == INVALID_HANDLE || g_slowMA == INVALID_HANDLE)
     {
      Print("均线指标句柄创建失败");
      return INIT_FAILED;
     }

   g_lastBarTime = iTime(g_symbol, PERIOD_CURRENT, 0);
   g_dayStart    = 0;
   g_llmFolder       = InpLLMFolder;
   g_lastLLMSignalId = (long)iTime(g_symbol, PERIOD_CURRENT, 0);
   UpdateDayState();

   PrintFormat("SimpleAutoTrader 初始化完成: %s, 快线=%d, 慢线=%d, 手数模式=%s",
               g_symbol, InpFastMAPeriod, InpSlowMAPeriod,
               (InpLotMode == LOT_MODE_FIXED) ? "固定手数" : "风险百分比");

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      Print("警告: 终端未启用自动交易(请点击工具栏的 Algo Trading 按钮)");
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("警告: 当前环境不允许自动交易(检查图表属性或测试器设置)");

   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_fastMA != INVALID_HANDLE)
      IndicatorRelease(g_fastMA);
   if(g_slowMA != INVALID_HANDLE)
      IndicatorRelease(g_slowMA);
  }

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
  {
   UpdateDayState();
   if(g_stopTrading)
      return;

   ManageTrailingStops();

   // LLM 桥接模式：由 DeepSeek 给出建议，人工确认后下单
   if(InpUseLLMBridge)
     {
      ProcessLLMBridge();
      return;
     }

   // 每根新K线只判断一次信号，避免同一根K线内反复开仓
   if(!IsNewBar())
      return;
   if(!InSession())
      return;
   if(!SpreadOK())
      return;

   int signal = GetSignal();
   bool hasLong  = HasPosition(POSITION_TYPE_BUY);
   bool hasShort = HasPosition(POSITION_TYPE_SELL);

   if(signal > 0)
     {
      // 金叉：持有多单则不动，持有空单先平掉
      if(hasShort)
         CloseAllPositions();
      if(!hasLong && CountPositions() < InpMaxPositions && DirectionAllowed(1))
         OpenPosition(ORDER_TYPE_BUY);
     }
   else if(signal < 0)
     {
      // 死叉：持有空单则不动，持有多单先平掉
      if(hasLong)
         CloseAllPositions();
      if(!hasShort && CountPositions() < InpMaxPositions && DirectionAllowed(-1))
         OpenPosition(ORDER_TYPE_SELL);
     }
  }

//+------------------------------------------------------------------+
//| 开仓（市价单，同时带止盈止损）                                    |
//+------------------------------------------------------------------+
void OpenPosition(ENUM_ORDER_TYPE type)
  {
   double point = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick))
      return;
   double entry = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;
   double sl = 0.0, tp = 0.0;

   if(InpSLPoints > 0)
      sl = NormalizePrice((type == ORDER_TYPE_BUY)
                          ? entry - InpSLPoints * point
                          : entry + InpSLPoints * point);
   if(InpTPPoints > 0)
      tp = NormalizePrice((type == ORDER_TYPE_BUY)
                          ? entry + InpTPPoints * point
                          : entry - InpTPPoints * point);

   OpenPositionWithSLTP(type, sl, tp);
  }

//+------------------------------------------------------------------+
//| 开仓核心函数（可指定止盈止损价格）                                |
//+------------------------------------------------------------------+
bool OpenPositionWithSLTP(ENUM_ORDER_TYPE type, double sl, double tp)
  {
   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick))
      return false;

   double entry = (type == ORDER_TYPE_BUY) ? tick.ask : tick.bid;
   double volume = CalculateVolume(type, entry, sl);
   if(volume <= 0.0)
     {
      Print("手数计算失败或余额不足，跳过开仓");
      return false;
     }

   bool done = false;
   if(type == ORDER_TYPE_BUY)
      done = g_trade.Buy(volume, g_symbol, 0.0, sl, tp, "SimpleAutoTrader");
   else
      done = g_trade.Sell(volume, g_symbol, 0.0, sl, tp, "SimpleAutoTrader");

   if(!done)
     {
      PrintFormat("开仓失败: %s (%d)", g_trade.ResultRetcodeDescription(), g_trade.ResultRetcode());
      return false;
     }
   else
     {
      PrintFormat("已开仓 %s %s %.2f 手, SL=%s, TP=%s",
                  g_symbol, (type == ORDER_TYPE_BUY) ? "买入" : "卖出", volume,
                  (sl > 0.0) ? DoubleToString(sl, _Digits) : "无",
                  (tp > 0.0) ? DoubleToString(tp, _Digits) : "无");
     }
   return true;
  }

//+------------------------------------------------------------------+
//| 计算手数（固定手数 或 按止损距离反推风险手数）                    |
//+------------------------------------------------------------------+
double CalculateVolume(ENUM_ORDER_TYPE type, double entry, double sl)
  {
   double minLot = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;

   double volume = InpFixedLot;

   if(InpLotMode == LOT_MODE_RISK_PERCENT)
     {
      if(sl <= 0.0)
        {
         Print("风险模式必须设置止损(SL)，请配置 InpSLPoints");
         return 0.0;
        }
      double slDist    = MathAbs(entry - sl);
      double tickValue = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize  = SymbolInfoDouble(g_symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tickValue <= 0.0 || tickSize <= 0.0)
        {
         Print("无法获取品种的 Tick 价值，无法按风险计算手数");
         return 0.0;
        }

      double ticks     = slDist / tickSize;
      double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
      volume = riskMoney / (tickValue * ticks);
     }

   // 规范化到品种允许的最小步进，并限制在最小/最大手数之间
   volume = MathFloor(volume / step) * step;
   volume = MathMax(minLot, MathMin(maxLot, volume));

   // 保证金安全检查：可用保证金不足时逐步降手数
   double margin = 0.0;
   if(!OrderCalcMargin(type, g_symbol, volume, entry, margin))
      return 0.0;

   double free = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   int guard = 0;
   while(margin > free * 0.95 && volume > minLot + step / 2.0 && guard < 50)
     {
      volume -= step;
      if(volume < minLot)
         volume = minLot;
      if(!OrderCalcMargin(type, g_symbol, volume, entry, margin))
         break;
      guard++;
     }

   return volume;
  }

//+------------------------------------------------------------------+
//| 移动止损：浮盈达到启动点数后，把止损移到当前价-回撤点数           |
//+------------------------------------------------------------------+
void ManageTrailingStops()
  {
   if(InpTrailingStartPoints <= 0)
      return;

   double point    = SymbolInfoDouble(g_symbol, SYMBOL_POINT);
   double trailDist = InpTrailingStepPoints * point;

   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick))
      return;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol)
         continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_magic)
         continue;

      long   type = PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      double newSL = 0.0;

      if(type == POSITION_TYPE_BUY)
        {
         if(tick.bid - open >= InpTrailingStartPoints * point)
            newSL = NormalizePrice(tick.bid - trailDist);
        }
      else if(type == POSITION_TYPE_SELL)
        {
         if(open - tick.ask >= InpTrailingStartPoints * point)
            newSL = NormalizePrice(tick.ask + trailDist);
        }

      // 只往有利方向移动止损
      if(newSL > 0.0 &&
         (sl == 0.0 || (type == POSITION_TYPE_BUY ? newSL > sl : newSL < sl)))
        {
         if(!g_trade.PositionModify(ticket, newSL, tp))
            PrintFormat("移动止损失败 #%I64u: %s", ticket, g_trade.ResultRetcodeDescription());
        }
     }
  }

//+------------------------------------------------------------------+
//| 当日最大亏损熔断：达到限制后平仓并停止交易                        |
//+------------------------------------------------------------------+
void UpdateDayState()
  {
   datetime d = iTime(g_symbol, PERIOD_D1, 0);
   if(d == 0)
      return;

   if(d != g_dayStart)
     {
      g_dayStart        = d;
      g_dayStartEquity  = AccountInfoDouble(ACCOUNT_EQUITY);
      g_stopTrading     = false;
     }

   if(InpMaxDailyLossPercent > 0.0 && !g_stopTrading)
     {
      double loss = g_dayStartEquity - AccountInfoDouble(ACCOUNT_EQUITY);
      if(loss > g_dayStartEquity * InpMaxDailyLossPercent / 100.0)
        {
         PrintFormat("达到当日最大亏损限制(%.2f%%), 平掉所有持仓并停止交易", InpMaxDailyLossPercent);
         CloseAllPositions();
         g_stopTrading = true;
        }
     }
  }

//+------------------------------------------------------------------+
//| LLM 桥接处理（人工确认模式）                                      |
//+------------------------------------------------------------------+
void ProcessLLMBridge()
  {
   if(!InSession())
      return;
   if(!SpreadOK())
     {
      // 只在每根新K线提示一次，避免刷屏
      if(IsNewBar())
         PrintFormat("点差过大(%d > %d), 跳过本K线信号",
                     (int)SymbolInfoInteger(g_symbol, SYMBOL_SPREAD), InpMaxSpreadPoints);
      return;
     }

   // 每根新K线写一次行情快照，供桥接服务询问 DeepSeek
   if(IsNewBar())
     {
      g_lastLLMSignalId = (long)iTime(g_symbol, PERIOD_CURRENT, 0);
      WriteLLMSnapshot(g_lastLLMSignalId);
     }

   // 每个 tick 检查是否有人工确认的决策
   LLMDecision d;
   if(!ReadLLMDecision(g_lastLLMSignalId, d))
      return;

   if(d.status == "confirmed")
      ExecuteLLMDecision(d);
  }

//+------------------------------------------------------------------+
//| 计算今日亚洲时段(北京时间 07:00-14:59)高低点，供伦敦突破使用        |
//+------------------------------------------------------------------+
void BuildAsiaRange(double &hi, double &lo)
  {
   hi = 0.0;
   lo = 0.0;
   int serverOffset = (int)(TimeCurrent() - TimeGMT());   // 服务器相对UTC偏移(秒)
   datetime nowBJ = TimeGMT() + 8 * 3600;                  // 当前北京墙钟(数值编码)
   MqlDateTime bd;
   TimeToStruct(nowBJ, bd);
   if(bd.hour < 7 || bd.hour >= 16)
      return;
   for(int i = 1; i <= 1440; i++)
     {
      datetime t = iTime(g_symbol, PERIOD_M1, i);
      if(t == 0)
         break;
      datetime bj = t - serverOffset + 8 * 3600;           // 转成北京墙钟(数值编码)
      MqlDateTime d;
      TimeToStruct(bj, d);
      if(d.year != bd.year || d.day_of_year != bd.day_of_year)
         break;
      if(d.hour < 7 || d.hour >= 15)
         continue;
      double h = iHigh(g_symbol, PERIOD_M1, i);
      double l = iLow(g_symbol, PERIOD_M1, i);
      if(hi == 0.0 || h > hi) hi = h;
      if(lo == 0.0 || l < lo) lo = l;
     }
  }

//+------------------------------------------------------------------+
//| 把行情快照写入桥接文件夹                                         |
//+------------------------------------------------------------------+
void WriteLLMSnapshot(long signalId)
  {
   string content = "";
   content += StringFormat("ID=%I64d\n", signalId);
   content += StringFormat("SYMBOL=%s\n", g_symbol);
   content += StringFormat("TIMEFRAME=%s\n", EnumToString((ENUM_TIMEFRAMES)Period()));
   content += StringFormat("SPREAD=%d\n", (int)SymbolInfoInteger(g_symbol, SYMBOL_SPREAD));
   // ===== 策略池 v1: 时间与关键价位(伦敦突破路由/规则使用) =====
   content += StringFormat("UTC_EPOCH=%I64d\n", (long)TimeGMT());
   content += StringFormat("D1_HIGH=%s\n", DoubleToString(iHigh(g_symbol, PERIOD_D1, 1), _Digits));
   content += StringFormat("D1_LOW=%s\n", DoubleToString(iLow(g_symbol, PERIOD_D1, 1), _Digits));
   double asiaHigh = 0.0, asiaLow = 0.0;
   BuildAsiaRange(asiaHigh, asiaLow);
   content += "ASIA_HIGH=" + DoubleToString(asiaHigh, _Digits) + "\n";
   content += "ASIA_LOW=" + DoubleToString(asiaLow, _Digits) + "\n";

   MqlTick tick;
   if(SymbolInfoTick(g_symbol, tick))
     {
      content += "BID=" + DoubleToString(tick.bid, _Digits) + "\n";
      content += "ASK=" + DoubleToString(tick.ask, _Digits) + "\n";
     }

   double fast[], slow[];
   if(CopyBuffer(g_fastMA, 0, 0, 1, fast) == 1)
      content += "FASTMA=" + DoubleToString(fast[0], _Digits) + "\n";
   if(CopyBuffer(g_slowMA, 0, 0, 1, slow) == 1)
      content += "SLOWMA=" + DoubleToString(slow[0], _Digits) + "\n";
   content += "LOCAL_SIGNAL=" + IntegerToString(GetSignal()) + "\n";

   // 最近10根已收盘K线 + 当前K线
   string candles = "";
   int total = 11;
   for(int i = 1; i <= total; i++)
     {
      double o = iOpen(g_symbol, PERIOD_CURRENT, i);
      double h = iHigh(g_symbol, PERIOD_CURRENT, i);
      double l = iLow(g_symbol, PERIOD_CURRENT, i);
      double c = iClose(g_symbol, PERIOD_CURRENT, i);
      if(o == 0.0 || h == 0.0 || l == 0.0 || c == 0.0)
         break;
      candles += (i > 1 ? "," : "")
                 + DoubleToString(o, _Digits) + ";" + DoubleToString(h, _Digits) + ";"
                 + DoubleToString(l, _Digits) + ";" + DoubleToString(c, _Digits);
     }
   content += "CANDLES=" + IntegerToString(total) + "|" + candles + "\n";

   bool found = false;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol) continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_magic) continue;
      content += "POSITION=" + ((PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "BUY" : "SELL") + "\n";
      content += "POSITION_VOLUME=" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + "\n";
      content += "POSITION_PROFIT=" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + "\n";
      content += "POSITION_SL=" + DoubleToString(PositionGetDouble(POSITION_SL), _Digits) + "\n";
      content += "POSITION_TP=" + DoubleToString(PositionGetDouble(POSITION_TP), _Digits) + "\n";
      found = true;
      break;
     }
   if(!found)
      content += "POSITION=none\n";

   content += "BALANCE=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "\n";
   content += "EQUITY=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + "\n";

   // 直接覆盖写 snapshot.txt（FILE_WRITE 打开即截断）
   // 文件末尾写 COMPLETE=1，桥接服务看到它才认为快照完整
   content += "COMPLETE=1\n";
   int h = FileOpen(g_llmFolder + "\\snapshot.txt", FILE_TXT | FILE_WRITE | FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("LLM快照写入失败(错误码 %d)，请先启动桥接服务", GetLastError());
      return;
     }
   FileWriteString(h, content);
   FileFlush(h);
   FileClose(h);
  }

//+------------------------------------------------------------------+
//| 读取桥接服务的决策文件                                           |
//+------------------------------------------------------------------+
bool ReadLLMDecision(long signalId, LLMDecision &d)
  {
   int h = FileOpen(g_llmFolder + "\\decision.txt", FILE_TXT | FILE_READ | FILE_ANSI);
   if(h == INVALID_HANDLE)
      return false;

   string line = "";
   while(!FileIsEnding(h))
     {
      line = FileReadString(h);
      if(StringLen(line) > 0)
         break;
     }
   FileClose(h);

   if(line == "")
      return false;

   string parts[];
   int n = StringSplit(line, '|', parts);
   if(n < 7)
      return false;

   d.id = StringToInteger(parts[0]);
   if(d.id != signalId)
      return false;
   d.status     = parts[1];
   d.action     = parts[2];
   d.lot        = StringToDouble(parts[3]);
   d.sl         = StringToDouble(parts[4]);
   d.tp         = StringToDouble(parts[5]);
   d.confidence = StringToDouble(parts[6]);
   return true;
  }

//+------------------------------------------------------------------+
//| 执行已确认的LLM决策                                              |
//+------------------------------------------------------------------+
void ExecuteLLMDecision(LLMDecision &d)
  {
   string action = d.action;
   StringToLower(action);

   string execStatus = "EXECUTED";
   if(action == "buy")
     {
      if(HasPosition(POSITION_TYPE_SELL))
         CloseAllPositions();
      if(!HasPosition(POSITION_TYPE_BUY) && CountPositions() < InpMaxPositions)
         execStatus = OpenPositionWithSLTP(ORDER_TYPE_BUY, d.sl, d.tp) ? "EXECUTED" : "FAILED";
     }
   else if(action == "sell")
     {
      if(HasPosition(POSITION_TYPE_BUY))
         CloseAllPositions();
      if(!HasPosition(POSITION_TYPE_SELL) && CountPositions() < InpMaxPositions)
         execStatus = OpenPositionWithSLTP(ORDER_TYPE_SELL, d.sl, d.tp) ? "EXECUTED" : "FAILED";
     }
   else if(action == "close")
     {
      CloseAllPositions();
     }
   else if(action == "hold")
     {
      // 不做任何操作
     }
   else
     {
      execStatus = "FAILED";
     }

   PrintFormat("LLM决策已处理: %s (%s)", action, execStatus);
   MarkDecisionDone(execStatus, action);
  }

//+------------------------------------------------------------------+
//| 标记决策已完成，避免重复执行                                     |
//+------------------------------------------------------------------+
void MarkDecisionDone(string status, string action)
  {
   int h = FileOpen(g_llmFolder + "\\decision.txt", FILE_TXT | FILE_READ | FILE_WRITE | FILE_ANSI);
   if(h != INVALID_HANDLE)
     {
      FileSeek(h, 0, SEEK_SET);
      FileWriteString(h, StringFormat("%I64d|%s|%s\n", g_lastLLMSignalId, "done", action));
      FileFlush(h);
      FileClose(h);
     }

   h = FileOpen(g_llmFolder + "\\executed.txt", FILE_TXT | FILE_READ | FILE_WRITE | FILE_ANSI);
   if(h != INVALID_HANDLE)
     {
      FileSeek(h, 0, SEEK_SET);
      FileWriteString(h, StringFormat("%I64d|%s|%s\n", g_lastLLMSignalId, status, action));
      FileFlush(h);
      FileClose(h);
     }
  }

//+------------------------------------------------------------------+
//| 双均线交叉信号（用已收盘K线判断，避免未来函数）                   |
//+------------------------------------------------------------------+
int GetSignal()
  {
   double fast[], slow[];
   if(CopyBuffer(g_fastMA, 0, 1, 2, fast) != 2)
      return 0;
   if(CopyBuffer(g_slowMA, 0, 1, 2, slow) != 2)
      return 0;

   if(fast[0] > slow[0] && fast[1] <= slow[1])
      return  1;   // 金叉 -> 买入
   if(fast[0] < slow[0] && fast[1] >= slow[1])
      return -1;   // 死叉 -> 卖出
   return 0;
  }

//+------------------------------------------------------------------+
//| 工具函数                                                          |
//+------------------------------------------------------------------+
bool IsNewBar()
  {
   datetime t = iTime(g_symbol, PERIOD_CURRENT, 0);
   if(t != g_lastBarTime)
     {
      g_lastBarTime = t;
      return true;
     }
   return false;
  }

bool InSession()
  {
   if(InpTradeStartHour == InpTradeEndHour)
      return true;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(InpTradeStartHour < InpTradeEndHour)
      return (dt.hour >= InpTradeStartHour && dt.hour < InpTradeEndHour);
   return (dt.hour >= InpTradeStartHour || dt.hour < InpTradeEndHour);
  }

bool SpreadOK()
  {
   if(InpMaxSpreadPoints <= 0)
      return true;
   return (SymbolInfoInteger(g_symbol, SYMBOL_SPREAD) <= InpMaxSpreadPoints);
  }

bool DirectionAllowed(int signal)
  {
   if(signal > 0)
      return (InpTradeMode == TRADE_MODE_BOTH || InpTradeMode == TRADE_MODE_LONG);
   if(signal < 0)
      return (InpTradeMode == TRADE_MODE_BOTH || InpTradeMode == TRADE_MODE_SHORT);
   return false;
  }

int CountPositions()
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol)
         continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_magic)
         continue;
      count++;
     }
   return count;
  }

bool HasPosition(ENUM_POSITION_TYPE type)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol)
         continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_magic)
         continue;
      if((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == type)
         return true;
     }
   return false;
  }

void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != g_symbol)
         continue;
      if((long)PositionGetInteger(POSITION_MAGIC) != g_magic)
         continue;
      if(!g_trade.PositionClose(ticket))
         PrintFormat("平仓失败 #%I64u: %s", ticket, g_trade.ResultRetcodeDescription());
     }
  }

double NormalizePrice(double price)
  {
   int digits = (int)SymbolInfoInteger(g_symbol, SYMBOL_DIGITS);
   return NormalizeDouble(price, digits);
  }
//+------------------------------------------------------------------+
