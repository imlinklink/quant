"""买入信号"消息面上下文"打包器（信息层第 1 步：美股）。

在美股抄底信号出现时，拉取：
  1. Yahoo 财经新闻（最近若干条）
  2. Yahoo quoteSummary calendarEvents（下一次财报日期 / EPS 预期）

输出为紧凑文本，随提案一起展示在确认页，并喂给大模型做判定参考。

合规：Yahoo 数据条款为 personal use only，仅供个人研究；
调用频率受信号触发次数限制（低频）。未来可扩展 EDGAR 申报(需声明真实 UA)、
FINRA 空头占比、Nasdaq 财报日历等。
"""
import logging
import os
import tempfile
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger('signal_context')

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

_SUB_TYPE_LABEL = {'NEWS': '资讯', 'NOTICE': '公告', 'RATING': '评级'}

# FINRA Reg SHO 文件缓存目录（每日一份，避免重复下载全市场文件）
_FINRA_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'quant_finra_shvol')
_FINRA_URL = 'https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt'


def _yahoo_session() -> requests.Session:
    """带 cookie 的 Yahoo session（新闻搜索与 quoteSummary 都需要）。"""
    s = requests.Session()
    s.headers['User-Agent'] = UA
    s.get('https://fc.yahoo.com', timeout=10)
    return s


def fetch_news(symbol: str, count: int = 5) -> List[Dict]:
    """Yahoo Finance 新闻搜索（个人研究用途，低频调用）。"""
    try:
        s = _yahoo_session()
        url = 'https://query2.finance.yahoo.com/v1/finance/search'
        r = s.get(url, params={'q': symbol, 'quotesCount': 0, 'newsCount': count}, timeout=12)
        r.raise_for_status()
        out = []
        for n in (r.json().get('news') or []):
            ts = n.get('providerPublishTime')
            out.append({
                'title': n.get('title'),
                'publisher': n.get('publisher'),
                'time': datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '',
                'published_at': datetime.fromtimestamp(ts, ZoneInfo('UTC')).isoformat() if ts else None,
                'observed_at': datetime.now(ZoneInfo('UTC')).isoformat(),
                'url': n.get('link'),
            })
        return out
    except Exception as e:
        logger.warning(f'拉取新闻失败 {symbol}: {e}')
        return []


def fetch_futu_news(symbol: str, count: int = 5) -> List[Dict]:
    """富途 OpenD get_search_news：资讯/公告/评级（中文源，支持港股）。"""
    try:
        import yaml
        from futu import OpenQuoteContext, NewsSubType, RET_OK
        cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
        cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
        futu_cfg = cfg.get('futu') or (cfg.get('hk') or {}).get('futu') or {}
        keyword = str(symbol or '').replace('US.', '').replace('HK.', '')
        if not keyword:
            return []
        with OpenQuoteContext(
            host=str(futu_cfg.get('host', '127.0.0.1')),
            port=int(futu_cfg.get('port', 11111)),
        ) as ctx:
            ret, data = ctx.get_search_news(keyword, max_count=count,
                                            news_sub_type=NewsSubType.ALL)
        if ret != RET_OK or data is None or len(data) == 0:
            return []
        out = []
        for _, row in data.head(count).iterrows():
            kind = str(row.get('news_sub_type', 'NEWS')).upper()
            ts = str(row.get('publish_time', '') or '')
            time_txt = ''
            try:
                time_txt = datetime.fromtimestamp(int(ts)).strftime('%m-%d %H:%M')
            except Exception:
                pass
            out.append({
                'title': row.get('title'),
                'publisher': f"{row.get('source') or '富途'}·{_SUB_TYPE_LABEL.get(kind, kind)}",
                'time': time_txt,
                'url': row.get('url'),
                'observed_at': datetime.now(ZoneInfo('UTC')).isoformat(),
            })
        return out
    except Exception as e:
        logger.warning(f'富途新闻拉取失败 {symbol}: {e}')
        return []


def get_futu_news_text(symbol: str, count: int = 4) -> str:
    """港股/美股确认页用：只取富途资讯/公告，拼成紧凑文本（失败返回空串）。"""
    try:
        items = fetch_futu_news(symbol, count=count)
        if not items:
            return f'{symbol} 近期富途资讯: 无'
        lines = [f'{symbol} 近期富途资讯/公告:']
        for n in items:
            t = str(n.get('time', ''))
            lines.append(
                f"- [{t}] {n.get('title', '')} ({n.get('publisher', '')})"
            )
        return '\n'.join(lines)
    except Exception as e:
        logger.warning(f'港股消息面文本失败 {symbol}: {e}')
        return ''


def _futu_ctx():
    import yaml
    from futu import OpenQuoteContext
    cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
    cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
    futu_cfg = cfg.get('futu') or (cfg.get('hk') or {}).get('futu') or {}
    return OpenQuoteContext(
        host=str(futu_cfg.get('host', '127.0.0.1')),
        port=int(futu_cfg.get('port', 11111)),
    )


def fetch_capital_flow_summary(symbol: str) -> Optional[Dict]:
    """富途当日资金流最新一笔：超大单/大单/中单/小单（主力≈超大单+大单）。"""
    try:
        from futu import RET_OK
        with _futu_ctx() as ctx:
            ret, data = ctx.get_capital_flow(symbol)
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        row = data.iloc[-1]

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return {
            'time': str(row.get('capital_flow_item_time', '')),
            'in_flow': _f(row.get('in_flow')),
            'super': _f(row.get('super_in_flow')),
            'big': _f(row.get('big_in_flow')),
            'mid': _f(row.get('mid_in_flow')),
            'sml': _f(row.get('sml_in_flow')),
            'main_proxy': None,
        }
    except Exception as e:
        logger.warning(f'资金流拉取失败 {symbol}: {e}')
        return None


def get_capital_flow_text(symbol: str) -> str:
    """资金流一行文本（供港股/美股提案用），失败返回空串。"""
    cap = fetch_capital_flow_summary(symbol)
    if not cap or cap.get('super') is None:
        return ''

    def _y(v):
        if v is None:
            return 'N/A'
        return f'{v / 1e8:+.2f}亿'

    main = (cap.get('super') or 0) + (cap.get('big') or 0)
    return (
        f"资金流({cap.get('time', '')}): 超大单 {_y(cap.get('super'))} / "
        f"大单 {_y(cap.get('big'))} / 中单 {_y(cap.get('mid'))} "
        f"(主力≈超大+大单 {_y(main)})"
    )


def fetch_option_summary(symbol: str) -> Optional[Dict]:
    """
    期权轻量摘要：最近到期日 + 近月 ATM 附近 call/put 的 IV/OI/Delta（抽样）。

    注意：这是"近月 ATM 抽样"，不是全市场 put/call 总量比。
    """
    try:
        from datetime import date as _date
        from futu import RET_OK, OptionType
        import futu.common.constant as _C
        import futu.quote.quote_query as _Q

        with _futu_ctx() as ctx:
            ret, exp_data = ctx.get_option_expiration_date(symbol)
            if ret != RET_OK or exp_data is None or len(exp_data) == 0:
                return None
            today = _date.today().isoformat()
            exps = []
            for _, r in exp_data.iterrows():
                v = r.iloc[0] if len(r) else ''
                s = str(v)[:10]
                if s >= today:
                    exps.append(s)
            if not exps:
                return None
            expiry = exps[0]
            legs, kinds = [], []
            for opt_type in (OptionType.CALL, OptionType.PUT):
                ret, chain = ctx.get_option_chain(
                    symbol, start=expiry, end=expiry, option_type=opt_type)
                if ret != RET_OK or chain is None or len(chain) == 0:
                    continue
                idx = len(chain) // 2  # 中位数行权价近似 ATM
                code = str(chain.iloc[idx]['code'])
                leg = _Q.OptionStrategyLeg()
                leg.code = code
                leg.action = _C.StrategyLegAction.BUY
                leg.quantity = 1
                legs.append(leg)
                kinds.append(('CALL' if opt_type == OptionType.CALL else 'PUT', code))
            if not legs:
                return None
            ret, q = ctx.get_option_quote(legs)
            if ret != RET_OK or q is None or len(q) == 0:
                return None
            out = {'expiry': expiry, 'legs': []}
            for (kind, code), (_, row) in zip(kinds, q.iterrows()):
                out['legs'].append({
                    'kind': kind,
                    'code': code,
                    'iv': row.get('implied_volatility'),
                    'open_interest': row.get('open_interest'),
                    'volume': row.get('volume'),
                    'delta': row.get('delta'),
                })
            return out
    except Exception as e:
        logger.warning(f'期权摘要拉取失败 {symbol}: {e}')
        return None


def get_option_oi_text(symbol: str) -> str:
    """期权近月 ATM 抽样摘要（只显示 OI，IV 缺失不展示）。"""
    opt = fetch_option_summary(symbol)
    if not opt or not opt.get('legs'):
        return ''
    parts = []
    for leg in opt['legs']:
        oi = leg.get('open_interest')
        oi_txt = f'{oi:,}' if isinstance(oi, int) else '-'
        delta = leg.get('delta')
        delta_txt = f'{float(delta):+.2f}' if isinstance(delta, (int, float)) else ''
        parts.append(f"{leg['kind']}: OI {oi_txt}{' Δ' + delta_txt if delta_txt else ''}")
    return f"期权近月抽样(到期 {opt.get('expiry')}): {'; '.join(parts)}"


def fetch_next_earnings(symbol: str) -> Optional[Dict]:
    """Yahoo quoteSummary calendarEvents：下一次财报日期与 EPS 预期。"""
    try:
        s = _yahoo_session()
        crumb_r = s.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=10)
        crumb_r.raise_for_status()
        crumb = crumb_r.text.strip()
        r = s.get(
            f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}',
            params={'modules': 'calendarEvents', 'crumb': crumb}, timeout=12,
        )
        r.raise_for_status()
        result = (r.json().get('quoteSummary') or {}).get('result')
        if not result:
            return None
        cal = (result[0].get('calendarEvents') or {}).get('earnings') or {}
        dates = cal.get('earningsDate') or []
        date_fmt = dates[0].get('fmt') if dates else ''
        eps = (cal.get('epsAverage') or {}).get('fmt')
        return {'date': date_fmt, 'eps_forecast': eps}
    except Exception as e:
        logger.warning(f'拉取财报事件失败 {symbol}: {e}')
        return None


def _load_short_volume_file(date_str: str) -> Optional[str]:
    """拉取（或读缓存）某交易日 FINRA 全市场空头成交量文件。"""
    path = os.path.join(_FINRA_CACHE_DIR, f'CNMSshvol{date_str}.txt')
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                return f.read()
        except Exception:
            pass
    try:
        os.makedirs(_FINRA_CACHE_DIR, exist_ok=True)
        r = requests.get(_FINRA_URL.format(date=date_str),
                         headers={'User-Agent': UA}, timeout=20)
        r.raise_for_status()
        text = r.text
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return text
    except Exception as e:
        logger.warning(f'拉取 FINRA 空头数据失败 {date_str}: {e}')
        return None


def _recent_us_trading_dates(n: int = 5) -> List[str]:
    """美东最近的 N 个工作日日期（近似交易日，周末跳过即可）。"""
    out = []
    d = datetime.now(ZoneInfo('America/New_York')).date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return out


def fetch_nasdaq_short_interest(symbol: str) -> Optional[Dict]:
    """
    Nasdaq 官方短空持仓（每两周结算一次）。

    返回最近一次结算记录：
      {source, settlement_date, short_interest, avg_daily_volume, days_to_cover}
    """
    ticker = str(symbol or '').upper().replace('US.', '')
    if not ticker:
        return None
    try:
        r = requests.get(
            f'https://api.nasdaq.com/api/quote/{ticker}/short-interest',
            params={'assetClass': 'stocks'},
            headers={'User-Agent': UA},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        table = ((d.get('data') or {}).get('shortInterestTable') or {})
        rows = table.get('rows') or []
        if not rows:
            return None
        row = rows[0]
        try:
            interest = int(str(row.get('interest', '0')).replace(',', ''))
        except ValueError:
            interest = None
        try:
            avg_vol = int(str(row.get('avgDailyShareVolume', '0')).replace(',', ''))
        except ValueError:
            avg_vol = None
        return {
            'source': 'nasdaq',
            'settlement_date': row.get('settlementDate'),
            'short_interest': interest,
            'avg_daily_volume': avg_vol,
            'days_to_cover': row.get('daysToCover'),
        }
    except Exception as e:
        logger.warning(f'拉取 Nasdaq 短空持仓失败 {symbol}: {e}')
        return None


def fetch_short_ratio(symbol: str) -> Optional[Dict]:
    """
    单只美股最近一个交易日的空头成交占比（FINRA Reg SHO）。

    注意：short volume ≠ short interest。它是当日卖单中被标记为空头的部分
    （含做市商对冲），绝对值普遍偏高，请用它看"日度变化/相对高低"。
    """
    sym = str(symbol or '').upper().replace('US.', '')
    if not sym:
        return None
    for d in _recent_us_trading_dates():
        text = _load_short_volume_file(d)
        if not text:
            continue
        for line in text.splitlines()[1:]:
            p = line.split('|')
            if len(p) < 5:
                continue
            if p[1].upper() != sym:
                continue
            try:
                short = float(p[2])
                total = float(p[4])
            except ValueError:
                continue
            if total <= 0:
                return {'source': 'finra', 'date': d, 'short_volume': short,
                        'total_volume': total, 'short_ratio': None}
            return {
                'source': 'finra',
                'date': d,
                'short_volume': short,
                'total_volume': total,
                'short_ratio': round(short / total, 4),
            }
        break  # 文件在但无该代码：不再回退更早日期
    # FINRA 不可达/无数据 → Nasdaq 官方双周短空持仓
    return fetch_nasdaq_short_interest(symbol)


def fetch_signal_context(symbol: str) -> Dict:
    """总入口：富途新闻优先 + Yahoo 补充合并，任何失败都降级为空。"""
    futu_news = fetch_futu_news(symbol)
    yahoo_news = fetch_news(symbol)
    merged, seen = [], set()
    for n in futu_news + yahoo_news:
        t = str(n.get('title', '')).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        merged.append(n)
        if len(merged) >= 6:
            break
    return {
        'news': merged,
        'earnings': fetch_next_earnings(symbol),
        'short': fetch_short_ratio(symbol),
        'capital': fetch_capital_flow_summary(symbol),
        'options': fetch_option_summary(symbol),
    }


def format_context(symbol: str, ctx: Dict) -> str:
    """转成给页面/大模型的紧凑文本。"""
    lines = [f'消息面上下文 {symbol}:']
    earn = ctx.get('earnings')
    if earn and earn.get('date'):
        eps = earn.get('eps_forecast') or 'N/A'
        lines.append(f'下次财报: {earn["date"]} (EPS 预期 {eps})')
    else:
        lines.append('下次财报: 未获取到')
    news = ctx.get('news') or []
    if news:
        lines.append('近期新闻:')
        for n in news[:5]:
            lines.append(
                f"- [{n.get('time', '')}] {n.get('title', '')} ({n.get('publisher', '')})"
            )
    else:
        lines.append('近期新闻: 无')
    short = ctx.get('short')
    if short and short.get('source') == 'nasdaq':
        interest = short.get('short_interest')
        interest_txt = f'{interest:,}' if isinstance(interest, int) else 'N/A'
        lines.append(
            f"短空持仓: {interest_txt} 股 "
            f"(结算 {short.get('settlement_date')}, Days to Cover "
            f"{short.get('days_to_cover')}, Nasdaq 双周)"
        )
    elif short and short.get('short_ratio') is not None:
        lines.append(
            f"空头成交占比: {short['short_ratio'] * 100:.1f}% "
            f"(FINRA {short['date']})"
        )
    elif short and short.get('date'):
        lines.append(f"空头成交占比: 数据缺失 (FINRA {short['date']})")
    capital = ctx.get('capital')
    if capital and capital.get('super') is not None:
        def _y(v):
            if v is None:
                return 'N/A'
            return f'{v / 1e8:+.2f}亿'
        main = (capital.get('super') or 0) + (capital.get('big') or 0)
        lines.append(
            f"资金流({capital.get('time', '')}): 超大单 {_y(capital.get('super'))} / "
            f"大单 {_y(capital.get('big'))} / 中单 {_y(capital.get('mid'))} "
            f"(主力≈超大+大单 {_y(main)})"
        )
    opt = ctx.get('options')
    if opt and opt.get('legs'):
        parts = []
        for leg in opt['legs']:
            iv = leg.get('iv')
            iv_txt = f'{float(iv) * 100:.0f}%' if isinstance(iv, (int, float)) else '-'
            parts.append(
                f"{leg['kind']}: IV {iv_txt} OI {leg.get('open_interest')}"
            )
        lines.append(f"期权近月抽样(到期 {opt.get('expiry')}): {'; '.join(parts)}")
    return '\n'.join(lines)


def get_context_text(symbol: str) -> str:
    """便捷函数：拉取并格式化为文本（失败返回空串，不阻塞交易）。"""
    try:
        return format_context(symbol, fetch_signal_context(symbol))
    except Exception as e:
        logger.warning(f'打包消息面上下文失败 {symbol}: {e}')
        return ''


def fetch_event_evidence(symbol: str, count: int = 5) -> List[Dict]:
    """拉取财报/公告/新闻，转成不可变 evidence 列表（带 evidence_id/content_hash，可被 LLM 引用）。

    事件分类、去重和时间校验由程序完成；LLM 只解释影响。
    失败/缺数据返回空列表，不伪造证据。
    """
    from mutifactor.llm.trade_review import evidence

    ctx = fetch_signal_context(symbol)
    out: List[Dict] = []
    seen = set()

    # 财报事件（kind=filing，TTL 长）
    earn = ctx.get('earnings')
    if earn and earn.get('date'):
        summary = f"下次财报日期 {earn['date']}"
        if earn.get('eps_forecast'):
            summary += f"，EPS 预期 {earn['eps_forecast']}"
        e = evidence(summary, 'yahoo:calendarEvents', kind='filing')
        out.append(e)
        seen.add(e['evidence_id'])

    # 新闻/公告（kind=news；published_at 缺失时以 observed_at 为时效基准）
    for n in (ctx.get('news') or [])[:count]:
        title = str(n.get('title') or '').strip()
        if not title:
            continue
        source = str(n.get('publisher') or 'news')
        e = evidence(title, source, n.get('observed_at'), n.get('published_at'), kind='news')
        if e['evidence_id'] not in seen:
            out.append(e)
            seen.add(e['evidence_id'])

    return out
