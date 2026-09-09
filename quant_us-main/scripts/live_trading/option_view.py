"""期权市场视角（供"股票现在是否适合买"参考，不交易期权）。

程序从富途期权链提取可解释的定价指标，作为给 LLM/用户的参考证据。
任何局部抽样都会显式标记，不能冒充完整链 PCR/Max Pain；delta 和
prob_of_profit 也不会被解释成标的上涨概率。

纯计算 compute_option_view 与富途 I/O fetch_option_view 分离，便于单测。
LLM 只读摘要，不自行算 IV/Greeks。
"""
import logging
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 目标期限桶。区间内取最接近 target 的到期日；区间缺失则该桶明确缺失。
DTE_BUCKETS = {
    'short': {'min': 7, 'max': 14, 'target': 10},
    'mid': {'min': 30, 'max': 45, 'target': 37},
    'long': {'min': 60, 'max': 90, 'target': 75},
}
_CACHE = {}
_CACHE_LOCK = threading.Lock()


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


def select_expiry_buckets(expiries, as_of=None, bucket_specs=None):
    """把到期日确定性映射到 short/mid/long 三个 DTE 桶。"""
    if as_of is None:
        as_of = datetime.now(ZoneInfo('America/New_York')).date()
    elif isinstance(as_of, datetime):
        as_of = as_of.date()
    elif isinstance(as_of, str):
        as_of = date.fromisoformat(as_of[:10])
    choices = []
    for raw in expiries or []:
        try:
            expiry = date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
        dte = (expiry - as_of).days
        if dte >= 0:
            choices.append((expiry.isoformat(), dte))
    specs = DTE_BUCKETS
    if bucket_specs:
        specs = {}
        for name, bounds in bucket_specs.items():
            if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                low, high = int(bounds[0]), int(bounds[1])
                specs[name] = {'min': low, 'max': high, 'target': (low + high) // 2}
    result = {}
    for name, spec in specs.items():
        eligible = [(expiry, dte) for expiry, dte in choices
                    if spec['min'] <= dte <= spec['max']]
        if eligible:
            expiry, dte = min(eligible, key=lambda x: (abs(x[1] - spec['target']), x[1]))
            result[name] = {'expiry_date': expiry, 'dte': dte}
    return result


def assess_data_quality(rows, *, expected_legs=None, observed_at=None, now=None,
                        min_quote_coverage=0.80, max_age_seconds=900,
                        max_quote_age_seconds=259200):
    """评估快照是否足以被 LLM 引用；返回稳定状态和原因码。"""
    rows = rows or []
    expected = int(expected_legs or len(rows) or 0)
    quoted = len(rows)
    coverage = quoted / expected if expected > 0 else 0.0
    sides = {str(r.get('option_type', '')).upper() for r in rows}
    valid_iv = sum(1 for r in rows if (_f(r.get('implied_volatility')) or 0) > 0)
    iv_coverage = valid_iv / quoted if quoted else 0.0
    reasons = []
    if expected <= 0:
        reasons.append('expected_legs_missing')
    if coverage < min_quote_coverage:
        reasons.append('quote_coverage_low')
    if not {'CALL', 'PUT'}.issubset(sides):
        reasons.append('one_sided_chain')
    if valid_iv < 2:
        reasons.append('iv_missing')
    elif iv_coverage < 0.50:
        reasons.append('iv_coverage_low')
    age_seconds = None
    if observed_at is not None:
        try:
            observed = datetime.fromisoformat(str(observed_at).replace('Z', '+00:00'))
            current = now or datetime.now(observed.tzinfo or ZoneInfo('UTC'))
            if isinstance(current, str):
                current = datetime.fromisoformat(current.replace('Z', '+00:00'))
            if observed.tzinfo is None and current.tzinfo is not None:
                observed = observed.replace(tzinfo=current.tzinfo)
            age_seconds = max(0.0, (current - observed).total_seconds())
            if age_seconds > max_age_seconds:
                reasons.append('snapshot_stale')
        except (TypeError, ValueError):
            reasons.append('observed_at_invalid')
    else:
        reasons.append('observed_at_missing')
    quote_times = []
    for row in rows:
        raw = row.get('update_time')
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo('America/New_York'))
            quote_times.append(parsed)
        except ValueError:
            continue
    quote_time_coverage = len(quote_times) / quoted if quoted else 0.0
    market_data_as_of = max(quote_times).isoformat() if quote_times else None
    if quote_time_coverage < 0.80:
        reasons.append('quote_time_coverage_low')
    if quote_times:
        reference = now
        if reference is None:
            reference = datetime.now(ZoneInfo('UTC'))
        elif isinstance(reference, str):
            reference = datetime.fromisoformat(reference.replace('Z', '+00:00'))
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=ZoneInfo('UTC'))
        newest_age = max(0.0, (reference.astimezone(quote_times[0].tzinfo) -
                               max(quote_times)).total_seconds())
        if newest_age > max_quote_age_seconds:
            reasons.append('market_quote_stale')
    fatal = {'quote_coverage_low', 'one_sided_chain', 'iv_missing', 'iv_coverage_low',
             'snapshot_stale', 'market_quote_stale'}
    if fatal.intersection(reasons):
        status = ('unusable' if coverage < 0.50 or 'one_sided_chain' in reasons
                  or 'iv_missing' in reasons or 'market_quote_stale' in reasons
                  else 'degraded')
    elif 'quote_time_coverage_low' in reasons:
        status = 'degraded'
    else:
        status = 'usable'
    return {
        'status': status,
        'reasons': reasons,
        'expected_legs': expected,
        'quoted_legs': quoted,
        'coverage_pct': round(coverage * 100, 1),
        'valid_iv_legs': valid_iv,
        'iv_coverage_pct': round(iv_coverage * 100, 1),
        'age_seconds': round(age_seconds, 1) if age_seconds is not None else None,
        'quote_time_coverage_pct': round(quote_time_coverage * 100, 1),
        'market_data_as_of': market_data_as_of,
    }


def _true_max_pain(rows):
    """完整链的最小总内在价值结算价；局部链不得调用。"""
    strikes = sorted({_f(r.get('strike_price')) for r in rows
                      if _f(r.get('strike_price')) is not None})
    if not strikes:
        return None
    payouts = {}
    for settle in strikes:
        total = 0.0
        for row in rows:
            strike = _f(row.get('strike_price'))
            oi = _f(row.get('open_interest')) or 0.0
            if strike is None or oi <= 0:
                continue
            kind = str(row.get('option_type', '')).upper()
            if kind == 'CALL':
                total += max(0.0, settle - strike) * oi
            elif kind == 'PUT':
                total += max(0.0, strike - settle) * oi
        payouts[settle] = total
    return min(payouts, key=payouts.get)


def compute_option_view(spot, rows, *, chain_complete=False):
    """从期权链报价行计算市场视角指标。

    Args:
        spot: 标的现价
        rows: quote 行，每行含 {option_type, strike_price, open_interest,
              implied_volatility, delta, prob_of_profit?, volume, expiry_date}

    Returns:
        chain_complete=True 时才返回全链 pcr_oi/max_pain；抽样数据返回
        sample_pcr_oi/oi_peak_strike，并带 chain_scope='near_atm_sample'。
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

    # IV：富途字段为百分数（如 45.2 表示 45.2%），字段名显式带 _pct。
    atm_iv_pct = round(sum(atm_ivs) / len(atm_ivs), 1) if atm_ivs else None

    # OI 比：只有完整链才能称为 PCR；局部 ATM 样本只用于诊断。
    call_oi = sum(_f(r.get('open_interest')) or 0 for r in calls)
    put_oi = sum(_f(r.get('open_interest')) or 0 for r in puts)
    oi_ratio = round(put_oi / call_oi, 2) if call_oi and call_oi > 0 else None
    call_volume = sum(_f(r.get('volume')) or 0 for r in calls)
    put_volume = sum(_f(r.get('volume')) or 0 for r in puts)
    volume_ratio = round(put_volume / call_volume, 2) if call_volume > 0 else None

    # OI 最大集中行权价不是 Max Pain，单独保留正确名称。
    oi_by_strike = {}
    for r in rows:
        sp = _f(r.get('strike_price'))
        oi = _f(r.get('open_interest')) or 0
        if sp is not None:
            oi_by_strike[sp] = oi_by_strike.get(sp, 0) + oi
    oi_peak_strike = max(oi_by_strike, key=oi_by_strike.get) if oi_by_strike else None

    # Delta 是价格敏感度，只按原义输出，绝不命名为“上涨概率”。
    atm_call_delta = _f(atm_calls[0].get('delta')) if atm_calls else None
    atm_put_delta = _f(atm_puts[0].get('delta')) if atm_puts else None

    result = {
        'atm_iv_pct': atm_iv_pct,
        'atm_iv_leg_count': len(atm_ivs),
        'atm_call_delta': round(atm_call_delta, 4) if atm_call_delta is not None else None,
        'atm_put_delta': round(atm_put_delta, 4) if atm_put_delta is not None else None,
        'oi_peak_strike': oi_peak_strike,
        'n_quoted': len(rows),
        'spot': round(spot, 2),
        'chain_scope': 'full_chain' if chain_complete else 'near_atm_sample',
    }
    if chain_complete:
        result['pcr_oi'] = oi_ratio
        result['pcr_volume'] = volume_ratio
        result['max_pain'] = _true_max_pain(rows)
    else:
        result['sample_pcr_oi'] = oi_ratio
    expiries = sorted({str(r.get('expiry_date')) for r in rows if r.get('expiry_date')})
    if len(expiries) == 1:
        result['expiry_date'] = expiries[0]
    return result


def compute_term_structure(spot, bucket_rows, *, expected_legs=None,
                           observed_at=None, now=None, quality_config=None):
    """计算三个 DTE 桶的完整链指标、期限结构和总质量门。"""
    expected_legs = expected_legs or {}
    quality_config = quality_config or {}
    buckets = {}
    qualities = {}
    for name in DTE_BUCKETS:
        rows = list((bucket_rows or {}).get(name) or [])
        if not rows:
            continue
        view = compute_option_view(spot, rows, chain_complete=True)
        expiry = view.get('expiry_date')
        if expiry and observed_at:
            try:
                view['dte'] = (date.fromisoformat(expiry) -
                               date.fromisoformat(str(observed_at)[:10])).days
            except ValueError:
                view['dte'] = None
        quality = assess_data_quality(
            rows,
            expected_legs=expected_legs.get(name),
            observed_at=observed_at,
            now=now,
            min_quote_coverage=float(quality_config.get('min_quote_coverage', 0.80)),
            max_age_seconds=float(quality_config.get('max_snapshot_age_seconds', 900)),
            max_quote_age_seconds=float(
                quality_config.get('max_market_quote_age_seconds', 259200)),
        )
        atm_count = int(view.get('atm_iv_leg_count') or 0)
        if atm_count < 2:
            quality['reasons'].append('atm_pair_incomplete')
            quality['status'] = 'unusable' if atm_count == 0 else 'degraded'
        view['quality'] = quality
        buckets[name] = view
        qualities[name] = quality

    eligible_primary = [name for name in ('mid', 'short', 'long')
                        if name in buckets and qualities[name]['status'] != 'unusable']
    primary_name = eligible_primary[0] if eligible_primary else (
        next((name for name in ('mid', 'short', 'long') if name in buckets), None))
    if primary_name is None:
        return {'error': '目标DTE期限桶无有效期权链', 'quality': {
            'status': 'unusable', 'reasons': ['all_buckets_missing']}}

    result = dict(buckets[primary_name])
    result['primary_bucket'] = primary_name
    result['buckets'] = buckets
    result['observed_at'] = observed_at
    result['missing_buckets'] = [name for name in DTE_BUCKETS if name not in buckets]

    def iv(name):
        if (qualities.get(name) or {}).get('status') == 'unusable':
            return None
        return _f((buckets.get(name) or {}).get('atm_iv_pct'))

    short_iv, mid_iv, long_iv = iv('short'), iv('mid'), iv('long')
    result['term_structure'] = {
        'short_atm_iv_pct': short_iv,
        'mid_atm_iv_pct': mid_iv,
        'long_atm_iv_pct': long_iv,
        'short_minus_mid_pct': round(short_iv - mid_iv, 1)
        if short_iv is not None and mid_iv is not None else None,
        'short_minus_long_pct': round(short_iv - long_iv, 1)
        if short_iv is not None and long_iv is not None else None,
    }
    slope = result['term_structure']['short_minus_mid_pct']
    result['term_structure']['state'] = (
        'front_elevated' if slope is not None and slope >= 5.0 else
        'front_discounted' if slope is not None and slope <= -5.0 else
        'roughly_flat' if slope is not None else 'insufficient'
    )

    statuses = [q['status'] for q in qualities.values()]
    total_expected = sum(q['expected_legs'] for q in qualities.values())
    total_quoted = sum(q['quoted_legs'] for q in qualities.values())
    overall_status = ('unusable' if not statuses or all(s == 'unusable' for s in statuses)
                      else 'degraded' if any(s != 'usable' for s in statuses)
                      or result['missing_buckets'] else 'usable')
    result['quality'] = {
        'status': overall_status,
        'reasons': ([f"missing_{name}_bucket" for name in result['missing_buckets']] +
                    sorted({reason for q in qualities.values() for reason in q['reasons']})),
        'expected_legs': total_expected,
        'quoted_legs': total_quoted,
        'coverage_pct': round(100 * total_quoted / total_expected, 1) if total_expected else 0.0,
        'buckets': qualities,
    }
    return result


def option_view_summary(view) -> str:
    """把期权视角转成给 LLM 的中文摘要（数值一律程序算好）。"""
    if not view or view.get('error'):
        return f"期权视角: {view.get('error', '不可用')}" if view else '期权视角: 无数据'
    quality = view.get('quality') or {}
    if quality.get('status') == 'unusable':
        reasons = ','.join(quality.get('reasons') or ['质量门未通过'])
        return f"期权视角不可用：{reasons}"
    parts = [f"期权市场视角：标的 {view.get('spot')}"]
    if quality:
        parts.append(f"数据质量 {quality.get('status', 'unknown')}"
                     f"（覆盖 {quality.get('coverage_pct', 'N/A')}%）")
    iv = view.get('atm_iv_pct')
    parts.append(f"ATM IV≈{iv}%" if iv is not None else "ATM IV 缺失")
    if view.get('chain_scope') == 'full_chain':
        pcr = view.get('pcr_oi')
        parts.append((f"全链 Put/Call OI≈{pcr}（仅表示Put/Call持仓结构，不直接代表方向）"
                      if pcr is not None else "全链 PCR 缺失"))
        pcr_volume = view.get('pcr_volume')
        parts.append(f"全链 Put/Call成交量≈{pcr_volume}（不含成交发起方向）"
                     if pcr_volume is not None else "全链成交量PCR缺失")
        mp = view.get('max_pain')
        parts.append(f"MaxPain≈{mp}" if mp is not None else "MaxPain 缺失")
    else:
        ratio = view.get('sample_pcr_oi')
        parts.append(f"ATM附近样本 Put/Call OI≈{ratio}" if ratio is not None else "样本 OI 比缺失")
        parts.append("局部抽样，不计算全链 PCR/MaxPain")
    cd = view.get('atm_call_delta')
    if cd is not None:
        parts.append(f"ATM Call Delta≈{cd}（敏感度，非上涨概率）")
    if view.get('expiry_date'):
        parts.append(f"到期日 {view['expiry_date']}")
    term = view.get('term_structure') or {}
    if term:
        parts.append(
            f"期限IV 短/中/长={term.get('short_atm_iv_pct')}/"
            f"{term.get('mid_atm_iv_pct')}/{term.get('long_atm_iv_pct')}%"
        )
        if term.get('short_minus_mid_pct') is not None:
            parts.append(f"近端-中期={term['short_minus_mid_pct']:+.1f}pct"
                         f"（{term.get('state')}）")
    parts.append(f"（报价 {view.get('n_quoted')} 腿）")
    return "；".join(parts)


def make_option_evidence(code, view, now=None):
    """生成一条 kind='option' 的 evidence（可被 LLM 引用，事件本身不可变）。"""
    from mutifactor.llm.trade_review import evidence
    now = now if now is not None else time.time()
    summary = option_view_summary(view)
    return evidence(summary, 'internal:option-view', now, kind='option')


# ============ 富途 I/O（真跑需要 OpenD，单测不依赖） ============

def _snapshot_batches(ctx, codes, expiry_date, batch_size=200):
    """批量获取逐合约快照；失败时递归拆分，不因单腿失败丢掉整批。"""
    from futu import RET_OK

    def fetch(batch):
        _wait_quote()
        try:
            ret, frame = ctx.get_market_snapshot(batch)
        except Exception:
            ret, frame = -1, None
        if ret == RET_OK and frame is not None and len(frame) > 0:
            return [dict(
                option_type=str(r.get('option_type', '')).upper(),
                strike_price=r.get('option_strike_price'),
                open_interest=r.get('option_open_interest'),
                volume=r.get('volume'),
                implied_volatility=r.get('option_implied_volatility'),
                delta=r.get('option_delta'),
                bid_price=r.get('bid_price'),
                ask_price=r.get('ask_price'),
                last_price=r.get('last_price'),
                expiry_date=expiry_date,
                option_code=r.get('code'),
                update_time=r.get('update_time'),
            ) for _, r in frame.iterrows()]
        if len(batch) <= 1:
            return []
        middle = len(batch) // 2
        return fetch(batch[:middle]) + fetch(batch[middle:])

    rows = []
    for start in range(0, len(codes), batch_size):
        rows.extend(fetch(codes[start:start + batch_size]))
    return rows


def _futu_full_chains(symbol: str, spot=None):
    """拉取三个目标 DTE 桶的完整 Call/Put 链及覆盖率元数据。"""
    import yaml
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
            return {}, {}, {}
        date_col = list(exp.columns)[0]
        all_exp = sorted({str(r[date_col])[:10] for _, r in exp.iterrows()})
        selected = select_expiry_buckets(
            all_exp, bucket_specs=(cfg.get('option_view') or {}).get('dte_buckets'))
        bucket_rows, expected = {}, {}

        def get_chain(exp_date, otype):
            # OpenD 限流是跨进程的；本进程 limiter 无法感知其它监控器调用，
            # 因此失败后做有界退避，最终仍失败则交给质量门降级。
            last_ret, last_chain = -1, None
            for attempt, delay in enumerate((0, 3, 10, 20)):
                if delay:
                    time.sleep(delay)
                _wait_chain()
                last_ret, last_chain = ctx.get_option_chain(
                    symbol, start=exp_date, end=exp_date, option_type=otype)
                if last_ret == RET_OK and last_chain is not None and len(last_chain) > 0:
                    return last_ret, last_chain
                logger.warning(
                    f'[期权] {symbol} {exp_date} chain({otype}) 第{attempt + 1}次失败: '
                    f'{last_ret}')
            return last_ret, last_chain

        for bucket, expiry_info in selected.items():
            exp_date = expiry_info['expiry_date']
            codes = []
            for otype in (OptionType.CALL, OptionType.PUT):
                ret_c, chain = get_chain(exp_date, otype)
                if ret_c != RET_OK or chain is None or len(chain) == 0:
                    logger.warning(f'[期权] {symbol} {exp_date} chain({otype}) 失败: {ret_c}')
                    continue
                codes.extend(str(code) for code in chain['code'].tolist())
            expected[bucket] = len(set(codes))
            bucket_rows[bucket] = _snapshot_batches(
                ctx, list(dict.fromkeys(codes)), exp_date)
            logger.info(f"[期权] {symbol} {bucket}/{exp_date}: "
                        f"报价 {len(bucket_rows[bucket])}/{expected[bucket]}")
        return bucket_rows, expected, selected


def fetch_option_view(symbol: str, *, cache_seconds=300, quality_config=None) -> dict:
    """拉完整期权链并计算期限结构；短期进程缓存降低接口压力。"""
    now_ts = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(symbol)
        if cached and now_ts - cached[0] <= cache_seconds:
            return dict(cached[1])
    try:
        spot = _fetch_spot(symbol)
        bucket_rows, expected, selected = _futu_full_chains(symbol, spot=spot)
        if not bucket_rows:
            return {'error': '期权链无数据'}
        if spot is None:
            all_rows = [r for rows in bucket_rows.values() for r in rows]
            strikes = sorted({_f(r.get('strike_price')) for r in all_rows
                              if _f(r.get('strike_price'))})
            if not strikes:
                return {'error': '行权价缺失'}
            spot = strikes[len(strikes) // 2]
        observed_at = datetime.now(ZoneInfo('UTC')).isoformat()
        result = compute_term_structure(
            spot, bucket_rows, expected_legs=expected, observed_at=observed_at,
            quality_config=quality_config)
        result['selected_expiries'] = selected
        with _CACHE_LOCK:
            _CACHE[symbol] = (now_ts, dict(result))
        return result
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
