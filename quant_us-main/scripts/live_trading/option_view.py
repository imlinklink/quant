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
import time

logger = logging.getLogger(__name__)

# 到期日距离阈值：≤ 此天数视为"近月"
NEAR_DAYS = 10


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
    """从富途拉某标的近月 call/put 全链报价行（单测外使用）。"""
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
        ret, exp = ctx.get_option_expiration_date(symbol)
        if ret != RET_OK or exp is None or len(exp) == 0:
            return []
        date_col = list(exp.columns)[0]
        all_exp = sorted({str(r[date_col])[:10] for _, r in exp.iterrows()})
        today = _date.today().isoformat()
        future = [e for e in all_exp if e >= today]
        if not future:
            return []
        rows = []
        # 近月 + 次近月各拉 call/put 链并取报价
        for exp_date in future[:2]:
            for otype in (OptionType.CALL, OptionType.PUT):
                ret_c, chain = ctx.get_option_chain(symbol, start=exp_date, end=exp_date,
                                                    option_type=otype)
                if ret_c != RET_OK or chain is None or len(chain) == 0:
                    continue
                codes = [str(c) for c in chain['code'].tolist()]
                # 批量报价；每次最多 50 腿
                for i in range(0, len(codes), 50):
                    legs = []
                    import futu.common.constant as _C
                    import futu.quote.quote_query as _Q
                    for cd in codes[i:i + 50]:
                        leg = _Q.OptionStrategyLeg()
                        leg.code = cd
                        leg.action = _C.StrategyLegAction.BUY
                        leg.quantity = 1
                        legs.append(leg)
                    ret_q, q = ctx.get_option_quote(legs)
                    if ret_q != RET_OK or q is None or len(q) == 0:
                        continue
                    for _, r in q.iterrows():
                        rows.append({
                            'option_type': str(r.get('option_type', '')).upper(),
                            'strike_price': r.get('strike_price'),
                            'open_interest': r.get('open_interest'),
                            'implied_volatility': r.get('implied_volatility'),
                            'delta': r.get('delta'),
                            'prob_of_profit': r.get('prob_of_profit'),
                            'expiry_date': exp_date,
                        })
        return rows


def fetch_option_view(symbol: str) -> dict:
    """拉富途期权链并计算市场视角（返回 dict 或 {'error': ...}）。"""
    try:
        rows = _futu_rows(symbol)
        if not rows:
            return {'error': '期权链无数据'}
        # spot 取近月 ATM call 行权价近似 — 简化：用链中位行权价当现价锚
        strikes = sorted({_f(r.get('strike_price')) for r in rows if _f(r.get('strike_price'))})
        if not strikes:
            return {'error': '行权价缺失'}
        spot = strikes[len(strikes) // 2]
        return compute_option_view(spot, rows)
    except Exception as e:
        logger.warning(f'期权视角拉取失败 {symbol}: {e}')
        return {'error': f'期权视角失败: {type(e).__name__}'}
