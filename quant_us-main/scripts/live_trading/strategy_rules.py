"""已收盘K线上的入场/失效规则；不发送订单。"""
import pandas as pd
import numpy as np


def completed_bars(frame, now, minutes=None):
    if frame is None or frame.empty:
        return pd.DataFrame()
    d = frame.copy()
    column = 'time_key' if 'time_key' in d else 'date'
    stamp = pd.to_datetime(d[column])
    if stamp.dt.tz is None:
        stamp = stamp.dt.tz_localize('America/New_York')
    stamp = stamp.dt.tz_convert('America/New_York')
    now = pd.Timestamp(now)
    now = now.tz_localize('America/New_York') if now.tz is None else now.tz_convert('America/New_York')
    # 日线保守排除当日，15分钟明确加上周期才视为已完成。
    end = stamp + pd.Timedelta(minutes=minutes) if minutes else stamp.dt.normalize() + pd.Timedelta(days=1)
    d['date'] = stamp
    d['bar_end'] = end
    return d[end <= now].sort_values('date').drop_duplicates('date').reset_index(drop=True)


def atr(frame, period=14):
    prev = frame.close.shift(1)
    return pd.concat([frame.high-frame.low, (frame.high-prev).abs(), (frame.low-prev).abs()], axis=1).max(axis=1).rolling(period).mean()


def pullback_signal(daily, sector, intraday, now, cfg=None, trend_daily=None):
    cfg = cfg or {}
    d, s, bars = completed_bars(daily, now), completed_bars(sector, now), completed_bars(intraday, now, 15)
    if len(d)<60 or len(s)<21 or len(bars)<6:
        return None
    trend = completed_bars(trend_daily, now) if trend_daily is not None else d
    if len(trend)<60:
        return None
    # 日期内联，禁止把缺失的板块数据补成零收益。
    pair = pd.merge(trend[['date','close']], s[['date','close']], on='date', suffixes=('_stock','_sector')).tail(21)
    if len(pair)!=21 or pair.iloc[-1]['date'] != trend.iloc[-1]['date']:
        return None
    if pair.close_stock.iloc[-1]/pair.close_stock.iloc[0] <= pair.close_sector.iloc[-1]/pair.close_sector.iloc[0]:
        return None
    ma20 = d.close.rolling(20).mean()
    trend_ma20=trend.close.rolling(20).mean()
    if trend.close.iloc[-1] <= trend.close.tail(50).mean() or trend_ma20.iloc[-1] <= trend_ma20.iloc[-6]:
        return None
    if s.close.iloc[-1] < s.close.tail(20).mean():
        return None
    a = atr(d).iloc[-1]
    if not np.isfinite(a) or a<=0:
        return None
    # 明确的局部低点采用左右各2根确认，仅从回调窗口之前的已确认枢轴选取。
    pivots = [i for i in range(2,len(d)-7) if d.low.iloc[i] < d.low.iloc[i-2:i].min()
              and d.low.iloc[i] < d.low.iloc[i+1:i+3].min()]
    if not pivots:
        return None
    structure = float(d.low.iloc[pivots[-1]])
    recent = d.tail(5)
    if recent.low.min() <= structure:
        return None
    ma10 = d.close.tail(10).mean()
    if min(abs(recent.low.min()-ma10), abs(recent.low.min()-ma20.iloc[-1])) > a*float(cfg.get('ma_buffer_atr', .5)):
        return None
    consolidation = bars.iloc[-6:-1]
    trigger = float(consolidation.high.max())
    if bars.close.iloc[-1] <= trigger or bars.close.iloc[-2] > trigger:
        return None
    stop = float(min(recent.low.min(), consolidation.low.min()) - .2*a)
    entry = float(bars.close.iloc[-1])
    resistance = float(d.high.tail(20).max())
    if stop<=0 or entry-stop>a*float(cfg.get('max_stop_atr',2)):
        return None
    if resistance>entry and (resistance-entry)/(entry-stop)<float(cfg.get('min_rr',1.5)):
        return None
    return dict(initial_stop=stop, structure_low=structure, signal_atr=float(a),
                signal_time=bars.bar_end.iloc[-1].isoformat(),
                signal_id='pullback:'+bars.bar_end.iloc[-1].isoformat(), trigger=trigger)


def breakout_retest_signal(daily, intraday, now, cfg=None):
    cfg=cfg or {}
    d,b=completed_bars(daily,now),completed_bars(intraday,now,15)
    n=int(cfg.get('entry_n',55))
    if len(d)<n+21 or len(b)<6:
        return None
    highs=d.high.shift(1).rolling(n).max()
    volumes=d.volume/d.volume.shift(1).rolling(20).mean()
    hits=[i for i in range(max(n+20,len(d)-5),len(d)-1)
          if d.close.iloc[i]>highs.iloc[i] and volumes.iloc[i]>=float(cfg.get('volume_ratio',1.5))]
    if not hits:
        return None
    i=hits[-1]; level=float(highs.iloc[i]); a=float(atr(d).iloc[i])
    if not np.isfinite(a) or a<=0:
        return None
    since=d.iloc[i+1:]
    if since.close.min()<level or abs(since.low.min()-level)>.5*a:
        return None
    if b.close.iloc[-1]<=b.high.iloc[-6:-1].max():
        return None
    return dict(initial_stop=float(since.low.min()-.2*a),breakout_level=level,signal_atr=a,
                signal_time=b.bar_end.iloc[-1].isoformat(),
                signal_id='retest:'+d.date.iloc[i].isoformat()+':'+b.bar_end.iloc[-1].isoformat(),
                max_chase_atr=float(cfg.get('max_chase_atr',.5)),failure_sessions=int(cfg.get('failure_sessions',3)))


def exit_reason(record, price, daily=None, intraday=None, now=None):
    initial = float(record.get('initial_stop') or 0)
    if initial and price <= initial:
        return 'HARD_STOP'
    mode = record.get('entry_mode')
    if mode=='dip_buy':
        if record.get('target') and price>=float(record['target']):
            return 'REBOUND_TARGET'
        bars=completed_bars(intraday,now,15)
        opened=pd.Timestamp(record['opened_at'],unit='s',tz='UTC')
        held=bars[bars.date>=opened] if not bars.empty else bars
        limit=int(record.get('time_exit_bars',8))
        if len(held)>=limit and float(held.close.iloc[limit-1])<=float(record['entry_price']):
            return 'REBOUND_TIMEOUT'
    if mode in ('donchian','pullback','breakout_retest'):
        d=completed_bars(daily,now)
        if not d.empty:
            opened=pd.Timestamp(record['opened_at'],unit='s',tz='UTC').tz_convert('America/New_York').normalize()
            held=d[d.date>=opened]
            window=held.head(int(record.get('failure_sessions',3)))
            if record.get('breakout_level') and len(window) and (window.close<float(record['breakout_level'])).any():
                return 'BREAKOUT_FAILED'
            if record.get('structure_low') and float(d.close.iloc[-1])<float(record['structure_low']):
                return 'STRUCTURE_FAILED'
    return None


class ExitData:
    """只读行情缓存；退出硬价格边界不依赖缓存刷新成功。"""
    def __init__(self,pool):
        self.pool=pool
        self.cache={}

    def get(self,code,minutes=None):
        import time
        from datetime import datetime,timedelta
        from futu import KLType,RET_OK
        key=(code,minutes)
        old=self.cache.get(key)
        if old and time.time()-old[0]<60:
            return old[1]
        with self.pool.get_quote_ctx() as ctx:
            ret,rows,_=ctx.request_history_kline(code=code,
                start=(datetime.now()-timedelta(days=120 if not minutes else 10)).strftime('%Y-%m-%d'),
                end=datetime.now().strftime('%Y-%m-%d'),ktype=KLType.K_15M if minutes else KLType.K_DAY,
                max_count=1000)
        if ret!=RET_OK:
            return None
        self.cache[key]=(time.time(),rows)
        return rows
