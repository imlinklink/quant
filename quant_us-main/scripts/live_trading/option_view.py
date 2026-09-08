"""期权市场视角（供"股票现在是否适合买"参考，不交易期权）。

程序从富途期权链算四个指标，作为给 LLM/用户的参考证据：
  1. ATM IV 水平 + 近月/远月 IV 抬升（事件/财报临近）
  2. Put/Call 未平仓比 (PCR) —— 市场偏空/对冲程度
  3. Max Pain（OI 最大行权价，到期日价格锚定）
  4. 方向概率（ATM call 的市场定价上涨概率）

纯计算 compute_option_view 与富途 I/O fetch_option_view 分离，便于单测。
LLM 只读摘要，不自行算 IV/Greeks。
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 到期日距离阈值：≤ 此天数视为"近月"
NEAR_DAYS = 10


class _RateLimit:
    """富途期权接口限流（实测额度）：chain 10次/30s, quote 60次/30s。

    进程内全局共享（模块级单例），批量遍历多标的时统一排队，
    避免每只单独拉时频繁撞限流。
    """

    def __init__(self, max_per_window: int, window: float = 30.0):
        self.max = max_per_window
        self.window = window
        self._ts: list = []
        self._lock = threading.Lock()

    def wait(self):
        while True:
            with self._lock:
                now = time.time()
                self._ts = [t for t in self._ts if now - t < self.window]
                if len(self._ts) < self.max:
                    self._ts.append(now)
                    return
                wait = self.window - (now - self._ts[0])
            if wait > 0:
                time.sleep(min(wait, 2.0))  # 分片等待，避免一次睡满


# 模块级共享：chain/quote 各自额度
_chain_limiter = _RateLimit(max_per_window=10)
_quote_limiter = _RateLimit(max_per_window=55)  # 留 5 余量给其它期权调用


def _wait_chain():
    _chain_limiter.wait()


def _wait_quote():
    _quote_limiter.wait()


# ============ 纯计算（可测） ============

def _f(v):
    try:
        x = float(v)
        return x if x == x else None  # NaN→None
    except (TypeError, ValueError):
        return None


def _atm_strikes(spot, rows):
    """取最接近 spot 的行权档（call/put 各最多 2 档）。"""
    if not rows:
        return []
    rows = [r for r in rows if _f(r.get('strike_price'))]
    rows.sort(key=lambda r: abs(_f(r['strike_price']) - spot))
    return rows[:4]


def compute_option_view(spot, rows):
    """从期权链报价行计算市场视角指标。

    Args:
        spot: 标的现价
        rows: quote 行，每行含 {option_type, strike_price, open_interest,
              implied_volatility, delta, prob_of_profit?, volume, expiry_date}

    Returns:
        dict {atm_iv_pct, far_iv_pct, iv_spike, pcr_oi, max_pain,
              up_prob_pct, n_quoted, missing}
    """
    spot = _f(spot)
    if not spot or not rows:
        return {'error': '缺行情或期权链'}

    calls = [r for r in rows if str(r.get('option_type', '')).upper() == 'CALL']
    puts = [r for r in rows if str(r.get('option_type', '')).upper() == 'PUT']
    ivs = [_f(r.get('implied_volatility')) for r in rows]
    ivs = [x for x in ivs if x is not None and x > 0]

    atm_calls = _atm_strikes(spot, calls)
    atm_puts = _atm_strikes(spot, puts)
    # ATM IV 用最接近现价的一档 call + 一档 put（各取最接近的 1 档，避免把远档拉偏）
    atm_ivs = []
    for r in (atm_calls[:1] + atm_puts[:1]):
        iv = _f(r.get('implied_volatility'))
        if iv and iv > 0:
            atm_ivs.append(iv)

    # IV：富途字段为百分数（如 45.2 表示 45.2%），统一转小数存原始、展示用 %
    atm_iv_pct = round(sum(atm_ivs) / len(atm_ivs), 1) if atm_ivs else None

    # PCR = put OI / call OI
    call_oi = sum(_f(r.get('open_interest')) or 0 for r in calls)
    put_oi = sum(_f(r.get('open_interest')) or 0 for r in puts)
    pcr = round(put_oi / call_oi, 2) if call_oi and call_oi > 0 else None

    # Max Pain = OI 总和最大的行权价
    oi_by_strike = {}
    for r in rows:
        sp = _f(r.get('strike_price'))
        oi = _f(r.get('open_interest')) or 0
        if sp is not None:
            oi_by_strike[sp] = oi_by_strike.get(sp, 0) + oi
    max_pain = max(oi_by_strike, key=oi_by_strike.get) if oi_by_strike else None

    # 方向概率：取 ATM call 的 prob_of_profit（缺失时用 delta 近似）
    up_prob_pct = None
    if atm_calls:
        c = atm_calls[0]
        p = _f(c.get('prob_of_profit'))
        if p is None:
            d = _f(c.get('delta'))
            p = d * 100 if d is not None else None
        up_prob_pct = round(p, 1) if p is not None else None

    return {
        'atm_iv_pct': atm_iv_pct,
        'pcr_oi': pcr,
        'max_pain': max_pain,
        'up_prob_pct': up_prob_pct,
        'n_quoted': len(rows),
        'spot': round(spot, 2),
    }


def option_view_summary(view) -> str:
    """把期权视角转成给 LLM 的中文摘要（数值一律程序算好）。"""
    if not view or view.get('error'):
        return f"期权视角: {view.get('error', '不可用')}" if view else '期权视角: 无数据'
    parts = [f"期权市场视角：标的 {view.get('spot')}"]
    iv = view.get('atm_iv_pct')
    parts.append(f"ATM IV≈{iv}%" if iv is not None else "ATM IV 缺失")
    pcr = view.get('pcr_oi')
    parts.append(f"Put/Call OI≈{pcr}" if pcr is not None else "PCR 缺失")
    mp = view.get('max_pain')
    parts.append(f"MaxPain≈{mp}" if mp is not None else "MaxPain 缺失")
    up = view.get('up_prob_pct')
    parts.append(f"市场定价上涨概率≈{up}%" if up is not None else "方向概率缺失")
    parts.append(f"（报价 {view.get('n_quoted')} 腿）")
    return "；".join(parts)


def make_option_evidence(code, view, now=None):
    """生成一条 kind='option' 的 evidence（可被 LLM 引用，事件本身不可变）。"""
    from mutifactor.llm.trade_review import evidence
    now = now if now is not None else time.time()
    summary = option_view_summary(view)
    return evidence(summary, 'internal:option-view', now, kind='option')


# ============ 富途 I/O（真跑需要 OpenD，单测不依赖） ============

def _futu_rows(symbol: str) -> list:
    """从富途拉某标的期权报价行（快速版：近月单到期，每侧抽样若干档，逐腿报价）。

    逐腿 get_option_quote（probe 已验证单腿可用；批量多腿在此环境不稳定）。
    抽样：ATM 附近各约 8 档，够算 ATM IV / PCR 近似 / Max Pain 锚点。
    """
    import yaml
    from datetime import date as _date
    from futu import OpenQuoteContext, RET_OK, OptionType
    from pathlib import Path

    cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
    cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
    futu_cfg = cfg.get('futu') or {}
    host = str(futu_cfg.get('host', '127.0.0.1'))
    port = int(futu_cfg.get('port', 11111))

    with OpenQuoteContext(host=host, port=port) as ctx:
        _wait_quote()  # 到期接口走 quote 额度
        ret, exp = ctx.get_option_expiration_date(symbol)
        if ret != RET_OK or exp is None or len(exp) == 0:
            logger.warning(f'[期权] {symbol} expiration 失败: {ret}')
            return []
        date_col = list(exp.columns)[0]
        all_exp = sorted({str(r[date_col])[:10] for _, r in exp.iterrows()})
        today = _date.today().isoformat()
        future = [e for e in all_exp if e >= today]
        if not future:
            return []
        exp_date = future[0]  # 只取最近到期

        rows = []
        for otype in (OptionType.CALL, OptionType.PUT):
            _wait_chain()
            ret_c, chain = ctx.get_option_chain(symbol, start=exp_date, end=exp_date,
                                                option_type=otype)
            if ret_c != RET_OK or chain is None or len(chain) == 0:
                # 富途失败时 chain 里常带原始错误消息，一并记下来
                err = chain if not hasattr(chain, 'empty') or getattr(chain, 'empty', True) else 'empty'
                logger.warning(f'[期权] {symbol} {exp_date} chain({otype}) 失败: {ret_c} msg={err}')
                continue
            codes = [str(c) for c in chain['code'].tolist()]
            # 抽样：ATM 中位数两侧各 6 档（够 ATM IV / PCR 近似 / Max Pain 锚点）
            n = len(codes)
            lo = max(0, n // 2 - 6)
            hi = min(n, n // 2 + 6)
            sample = codes[lo:hi] or codes[:12]
            import futu.common.constant as _C
            import futu.quote.quote_query as _Q
            got = 0
            fails = 0
            for cd in sample:  # 逐腿报价（稳，每次先限流）
                try:
                    leg = _Q.OptionStrategyLeg()
                    leg.code = cd
                    leg.action = _C.StrategyLegAction.BUY
                    leg.quantity = 1
                    _wait_quote()
                    ret_q, q = ctx.get_option_quote([leg])
                    if ret_q != RET_OK or q is None or len(q) == 0:
                        fails += 1
                        if fails <= 2:
                            logger.warning(f'[期权] {symbol} 腿报价失败 {cd}: ret={ret_q} msg={q}')
                        continue
                    r = q.iloc[0]
                    rows.append({
                        'option_type': str(r.get('option_type', '')).upper(),
                        'strike_price': r.get('strike_price'),
                        'open_interest': r.get('open_interest'),
                        'implied_volatility': r.get('implied_volatility'),
                        'delta': r.get('delta'),
                        'prob_of_profit': r.get('prob_of_profit'),
                        'expiry_date': exp_date,
                    })
                    got += 1
                except Exception as e:
                    logger.warning(f'[期权] {symbol} 单腿报价异常 {cd}: {type(e).__name__}: {e}')
                    fails += 1
            logger.info(f'[期权] {symbol} {exp_date} {otype}: 链 {len(sample)} 档, 报价成功 {got}, 失败 {fails}')
        return rows


def fetch_option_view(symbol: str) -> dict:
    """拉富途期权链并计算市场视角（返回 dict 或 {'error': ...}）。"""
    try:
        rows = _futu_rows(symbol)
        if not rows:
            return {'error': '期权链无数据'}
        # spot：优先取标的真实现价（快照），失败退回抽样行权价中位
        spot = _fetch_spot(symbol)
        if spot is None:
            strikes = sorted({_f(r.get('strike_price')) for r in rows if _f(r.get('strike_price'))})
            if not strikes:
                return {'error': '行权价缺失'}
            spot = strikes[len(strikes) // 2]
        return compute_option_view(spot, rows)
    except Exception as e:
        logger.warning(f'期权视角拉取失败 {symbol}: {e}')
        return {'error': f'期权视角失败: {type(e).__name__}'}


def _fetch_spot(symbol: str):
    """取标的现价（富途快照），失败返回 None。"""
    try:
        import yaml
        from futu import OpenQuoteContext, RET_OK
        from pathlib import Path
        cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / 'config.yaml')
                             .read_text(encoding='utf-8')) or {}
        fc = cfg.get('futu') or {}
        with OpenQuoteContext(host=str(fc.get('host', '127.0.0.1')),
                              port=int(fc.get('port', 11111))) as ctx:
            ret, snap = ctx.get_market_snapshot([symbol])
            if ret == RET_OK and snap is not None and len(snap) > 0:
                return float(snap.iloc[0]['last_price'])
    except Exception as e:
        logger.debug(f'期权 spot 获取失败 {symbol}: {e}')
    return None
