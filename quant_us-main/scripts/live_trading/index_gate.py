# -*- coding: utf-8 -*-
"""美股抄底 大盘/指数门（板块代理，影子优先）

设计（配置见 dip_buy.index_gate）：
  - 每只监控票按 code_cluster 归到板块，用 1x 代理 ETF（SOXX/FXI/SPY/QQQ…）
    判断系统性环境，不使用池内 3x 标的身兼代理；
  - 代理指数 < MA20 且当日跌幅 ≤ pause_drop_pct → pause（停抄）；
    仅 < MA20（below_ma20_action=stricter）→ stricter（提高买入门槛）；
  - 默认 enforce=false：只把状态写入 result / 提案 / dip_scans，
    供 scan_attribution 回填收益验证后，再决定是否真正拦截。

MA20 只用“已收盘”的日K（排除今日盘中未收盘bar）；现价用富途实时快照，
拿不到快照时回退昨收（pause 口径失效，但 MA 弱势状态保留，整体偏放行）。
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_PROXIES = {
    'semis': ['US.SOXX', 'US.QQQ'],
    'china': ['US.FXI'],
    'default': ['US.SPY', 'US.QQQ'],
}


def compute_index_state(
    closes: Optional[List[float]],
    last_price: Optional[float],
    pause_drop_pct: float = 0.01,
    below_ma20_action: str = 'stricter',
    strict_bonus: int = 2,
) -> Dict:
    """纯函数：由已收盘日K收盘价序列 + 现价算出指数门状态（便于单测）。"""
    base = {
        'ok': False,
        'below_ma20': None,
        'ma20': None,
        'prev_close': None,
        'drop_pct': None,
        'action': '',
        'strict_bonus': 0,
        'reason': '',
    }
    if not closes or len(closes) < 30:
        base['reason'] = '指数日K不足(<30)，放行'
        return base
    if last_price is None or last_price <= 0:
        base['reason'] = '指数现价缺失，放行'
        return base
    prev = float(closes[-1])
    ma20 = float(sum(float(c) for c in closes[-20:]) / 20.0)
    if prev <= 0 or ma20 <= 0:
        base['reason'] = '指数价格异常，放行'
        return base

    below = last_price < ma20
    drop = last_price / prev - 1.0
    action = ''
    if below and drop <= -pause_drop_pct:
        action = 'pause'
    elif below and str(below_ma20_action).lower() == 'pause':
        action = 'pause'
    elif below:
        action = 'stricter'

    base.update({
        'ok': True,
        'below_ma20': bool(below),
        'ma20': round(ma20, 3),
        'prev_close': round(prev, 3),
        'drop_pct': round(drop * 100, 3),
        'action': action,
        'strict_bonus': strict_bonus if action == 'stricter' else 0,
    })
    return base


class DipIndexGate:
    """实时指数门：按 code 聚类取代理 ETF，缓存日K与快照。"""

    def __init__(self, dip_cfg: Optional[Dict], pool=None):
        cfg = {}
        if isinstance(dip_cfg, dict):
            raw = dip_cfg.get('index_gate') or {}
            cfg = raw if isinstance(raw, dict) else {}
        self.enabled = bool(cfg.get('enabled', False))
        self.enforce = bool(cfg.get('enforce', False))
        self.pause_drop_pct = float(cfg.get('pause_drop_pct', 0.01))
        self.below_action = str(cfg.get('below_ma20_action', 'stricter')).lower()
        self.strict_bonus = int(cfg.get('strict_score_bonus', 2))
        self.cache_seconds = float(cfg.get('cache_seconds', 300))
        proxies = cfg.get('proxies') or {}
        self.proxies = {
            k: (list(v) if isinstance(v, list) else DEFAULT_PROXIES.get(k, []))
            for k, v in proxies.items()
        }
        if 'default' not in self.proxies:
            self.proxies['default'] = DEFAULT_PROXIES['default']
        cc = cfg.get('code_cluster') or {}
        self.code_cluster = {str(k): str(v) for k, v in cc.items()} if isinstance(cc, dict) else {}
        self.pool = pool
        self._daily_cache: Dict[str, tuple] = {}
        self._price_cache: Dict[str, tuple] = {}

    def cluster_for(self, code: str) -> str:
        return str(self.code_cluster.get(code, 'default'))

    def _et_now(self) -> datetime:
        return datetime.now().astimezone(ZoneInfo('America/New_York'))

    def _fetch_daily_closes(self, proxy: str) -> Optional[List[float]]:
        """拉取代理 ETF 日K，只用已收盘（date < 美东今日）的收盘价。"""
        et_now = self._et_now()
        et_date = et_now.strftime('%Y-%m-%d')
        key = f'{proxy}|{et_date}'
        cached = self._daily_cache.get(key)
        if cached and time.time() - cached[0] < self.cache_seconds:
            return cached[1]
        if self.pool is None:
            return None
        from futu import KLType, RET_OK
        start = (et_now - timedelta(days=500)).strftime('%Y-%m-%d')
        end = et_now.strftime('%Y-%m-%d')
        try:
            with self.pool.get_quote_ctx() as ctx:
                ret, data, _ = ctx.request_history_kline(
                    code=proxy, start=start, end=end,
                    ktype=KLType.K_DAY, autype='qfq',
                )
        except Exception as e:
            logger.warning(f"[指数门] 拉取 {proxy} 日K失败: {e}")
            return None
        if ret != RET_OK or data is None or len(data) == 0:
            return None
        try:
            df = data[['time_key', 'close']].copy()
            df['time_key'] = df['time_key'].astype(str)
            df = df[df['time_key'] < et_date]  # 只保留已收盘bar
            df = df.sort_values('time_key')
            closes = [float(c) for c in df['close'].astype(float).tail(150)]
        except Exception as e:
            logger.warning(f"[指数门] 解析 {proxy} 日K失败: {e}")
            return None
        self._daily_cache[key] = (time.time(), closes)
        return closes

    def _fetch_current_price(self, proxy: str) -> Optional[float]:
        """实时快照价：按美东时段取对应字段（与 dip 监控器一致）。"""
        cached = self._price_cache.get(proxy)
        if cached and time.time() - cached[0] < min(self.cache_seconds, 60):
            return cached[1]
        if self.pool is None:
            return None
        from futu import RET_OK, SubType
        try:
            with self.pool.get_quote_ctx() as ctx:
                ret, err = ctx.subscribe([proxy], [SubType.QUOTE], subscribe_push=False)
                if ret != RET_OK:
                    logger.debug(f"[指数门] 订阅 {proxy} 失败: {err}")
                    return None
                ret, snap = ctx.get_market_snapshot([proxy])
                if ret == RET_OK and snap is not None and len(snap) > 0:
                    r = snap.iloc[0]
                    et_now = self._et_now()
                    t = et_now.hour * 60 + et_now.minute
                    if t < 4 * 60 or t >= 20 * 60:
                        field = 'overnight_price'
                    elif t < 9 * 60 + 30:
                        field = 'pre_price'
                    elif t < 16 * 60:
                        field = 'last_price'
                    else:
                        field = 'after_price'
                    order = [field, 'last_price', 'pre_price',
                             'after_price', 'overnight_price']
                    for k in order:
                        try:
                            v = float(r.get(k) or 0)
                        except (TypeError, ValueError):
                            continue
                        if v > 0:
                            self._price_cache[proxy] = (time.time(), v)
                            return v
        except Exception as e:
            logger.warning(f"[指数门] 快照 {proxy} 失败: {e}")
        return None

    def get_state(self, code: str, last_price: Optional[float] = None) -> Dict:
        """返回该 code 对应代理指数的门状态。任何异常/数据缺失都放行。"""
        if not self.enabled:
            return {'ok': False, 'proxy': '', 'cluster': self.cluster_for(code),
                    'action': '', 'strict_bonus': 0, 'reason': '指数门未启用'}
        if self.pool is None:
            return {'ok': False, 'proxy': '', 'cluster': self.cluster_for(code),
                    'action': '', 'strict_bonus': 0, 'reason': '数据源未就绪，放行'}
        cluster = self.cluster_for(code)
        proxy_list = list(self.proxies.get(cluster) or [])
        if not proxy_list:
            proxy_list = list(self.proxies.get('default') or [])
        errs = []
        for proxy in proxy_list:
            try:
                closes = self._fetch_daily_closes(proxy)
                if not closes or len(closes) < 30:
                    errs.append(f'{proxy}日K不足')
                    continue
                price = last_price if (last_price or 0) > 0 else self._fetch_current_price(proxy)
                fallback_price = False
                if price is None or price <= 0:
                    price = float(closes[-1])
                    fallback_price = True
                st = compute_index_state(
                    closes, price,
                    pause_drop_pct=self.pause_drop_pct,
                    below_ma20_action=self.below_action,
                    strict_bonus=self.strict_bonus,
                )
                if not st.get('ok'):
                    errs.append(f'{proxy}: {st.get("reason", "")}')
                    continue
                st.update({
                    'proxy': proxy,
                    'cluster': cluster,
                    'code': code,
                    'fallback_price': fallback_price,
                    'reason': (
                        f'代理 {proxy} 现价 ${price:.2f} '
                        f'{"<" if st["below_ma20"] else "≥"} MA20 ${st["ma20"]:.2f}'
                        + (f'，日内 {st["drop_pct"]:+.2f}%，暂停新抄底'
                           if st['action'] == 'pause' else
                           f'，日内 {st["drop_pct"]:+.2f}%，弱势提高门槛'
                           if st['action'] == 'stricter' else
                           '，环境正常')
                    ),
                })
                return st
            except Exception as e:
                errs.append(f'{proxy}: {e}')
        return {
            'ok': False, 'proxy': '', 'cluster': cluster, 'action': '',
            'strict_bonus': 0, 'reason': '代理指数不可用，放行'
            + ('（' + '；'.join(errs[-2:]) + '）' if errs else ''),
        }
