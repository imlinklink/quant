#!/usr/bin/env python3
"""
参数扫描：测试不同 RSI/BB 阈值组合对美团 2026-04-17 的效果
找到最适合捕捉日内超跌反弹的配置
"""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from futu import *

# ── 拉数据 ──────────────────────────────────────────────────────────────
ctx = OpenQuoteContext('127.0.0.1', 11111)
ret5, data5, _ = ctx.request_history_kline(
    'HK.03690', '2026-04-17', '2026-04-17', KLType.K_5M, AuType.QFQ, max_count=100)
ret1, data1, _ = ctx.request_history_kline(
    'HK.03690', '2026-04-17', '2026-04-17', KLType.K_1M, AuType.QFQ, max_count=480)
ctx.close()

def parse(data):
    if data is None or len(data) == 0:
        return None
    df = pd.DataFrame(data)
    for c in ['open', 'close', 'high', 'low', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.sort_values('time_key').reset_index(drop=True)

df5 = parse(data5)
df1 = parse(data1)
closes5 = df5['close'].values
closes1 = df1['close'].values

# ── 预计算 1分钟 RSI 和 BB ─────────────────────────────────────────────
rsi_period = 14
bb_period = 20
alpha = 1.0 / rsi_period

rsis_1m = [None] * len(closes1)
deltas = np.diff(closes1)
gains = np.where(deltas > 0, deltas, 0.0)
losses = np.where(deltas < 0, -deltas, 0.0)
for i in range(rsi_period, len(gains)):
    if i == rsi_period:
        ag = float(np.mean(gains[:rsi_period]))
        al = float(np.mean(losses[:rsi_period]))
    else:
        ag = alpha * gains[i] + (1 - alpha) * ag
        al = alpha * losses[i] + (1 - alpha) * al
    rsis_1m[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

bb_poses_1m = [None] * len(closes1)
for i in range(bb_period - 1, len(closes1)):
    w = closes1[i - bb_period + 1:i + 1]
    sma = np.mean(w)
    std = np.std(w, ddof=1)
    lo_b = sma - 2 * std
    hi_b = sma + 2 * std
    bw = hi_b - lo_b
    bb_poses_1m[i] = 50.0 if bw == 0 else (closes1[i] - lo_b) / bw * 100

# ── 扫描函数 ────────────────────────────────────────────────────────────
def calc_score(rsi_val, bb_pos, rsi_severe, rsi_oversold, bb_tight, bb_low,
               vol_score, cand_score, strong_thresh, watch_thresh):
    rsi_s = 3 if rsi_val and rsi_val < rsi_severe else \
            2 if rsi_val and rsi_val < rsi_oversold else \
            1 if rsi_val and rsi_val < 40 else 0
    bb_s = 2 if bb_pos is not None and bb_pos < bb_tight else \
           1 if bb_pos is not None and bb_pos < bb_low else 0
    total = min(rsi_s + bb_s + vol_score + cand_score, 10)
    if total >= strong_thresh:
        return total, '强买'
    elif total >= watch_thresh:
        return total, '观望'
    else:
        return total, ''


# ── 真实后续涨幅（前看3根5分钟K线 ≈ 15分钟）────────────────────────────
def future_gain(i5, n=3):
    """信号后 n 根5分钟K线的最大涨幅"""
    future_prices = closes5[i5 + 1:i5 + 1 + n]
    cur = closes5[i5]
    if len(future_prices) == 0:
        return None
    return (future_prices.max() - cur) / cur * 100


# ── 参数扫描 ───────────────────────────────────────────────────────────
configs = [
    # name, rsi_severe, rsi_oversold, bb_tight, bb_low, strong_thresh, watch_thresh
    # 基准（当前生产配置）
    ('[基准] RSI_severe=25, BB_tight=10%, 强买≥6',
     25, 30, 10, 25, 6, 4),
    # 调低RSI超卖阈值
    ('[RSI放宽] RSI_severe=28, RSI_over=32, BB_tight=10% 强买≥6',
     28, 32, 10, 25, 6, 4),
    # 放宽BB阈值
    ('[BB放宽] RSI_severe=25, BB_tight=15%, 强买≥6',
     25, 30, 15, 25, 6, 4),
    # RSI+BB都放宽
    ('[双放宽] RSI_severe=28, BB_tight=15%, 强买≥6',
     28, 32, 15, 25, 6, 4),
    # 强买阈值降到5
    ('[降阈值] RSI_severe=25, BB_tight=10%, 强买≥5',
     25, 30, 10, 25, 5, 3),
    # RSI放宽+强买5
    ('[最激进] RSI_severe=28, BB_tight=15%, 强买≥5',
     28, 32, 15, 25, 5, 3),
    # BB极度宽松
    ('[BB极度宽松] RSI_severe=28, BB_tight=20%, 强买≥6',
     28, 32, 20, 30, 6, 4),
    # RSI极度宽松
    ('[RSI极度宽松] RSI_severe=30, RSI_over=35, BB_tight=10%, 强买≥6',
     30, 35, 10, 25, 6, 4),
]

print(f"\n{'#'*110}")
print(f"#  参数扫描：美团 HK.03690  2026-04-17  |  混合模式：RSI/BB用1分钟，量/形态用5分钟")
print(f"#  评分逻辑：RSI得分({'+'*0}) + BB得分({'+'*0}) + 量(+3) + 形(+3)  强买阈值因配置而异")
print(f"{'#'*110}")
print()
print(f"  %-55s %5s %5s %6s %6s %s" % (
    "配置", "强买", "观望", "总信号", "真机会", "最佳买点"))
print(f"  {'-'*105}")

results_all = []

for name, rsi_severe, rsi_oversold, bb_tight, bb_low, strong_t, watch_t in configs:
    signals = []
    for i5, row5 in df5.iterrows():
        c5 = row5['close']
        if i5 < 20:
            continue  # 需要足够数据

        # RSI_1m
        target_i1 = min(i5 * 5 + 4, len(closes1) - 1)
        rsi_1m = rsis_1m[target_i1]
        bb_1m = bb_poses_1m[target_i1]

        # 量
        bars5 = df5.iloc[max(0, i5 - 19):i5 + 1].reset_index(drop=True)
        if len(bars5) < 5:
            vol_s, cand_s = 0, 0
        else:
            v = bars5['volume'].values
            avg5 = np.mean(v[-5:])
            prev5 = np.mean(v[-10:-5]) if len(v) >= 10 else avg5
            cv = v[-1]
            price_up = closes5[i5] > closes5[i5 - 1] if i5 > 0 else False
            vol_s = 0
            if price_up and cv > avg5 * 1.5:
                vol_s += 2
            elif not price_up and cv < avg5 * 0.7:
                vol_s += 1
            if avg5 >= prev5 * 0.5:
                vol_s += 1
            vol_s = min(vol_s, 3)

            # 形态
            cand_s = 0
            if len(bars5) >= 2:
                b2 = bars5.iloc[-1]
                b1 = bars5.iloc[-2]
                body = abs(b2['close'] - b2['open'])
                lower = min(b2['open'], b2['close']) - b2['low']
                if body > 0 and lower > body * 2:
                    cand_s += 1
                if len(bars5) >= 3:
                    if (b1['close'] < b1['open'] and
                        bars5.iloc[-3]['close'] < bars5.iloc[-3]['open'] and
                        b2['close'] > b2['open']):
                        cand_s += 1
                    c3 = bars5['close'].values[-3:]
                    if all(c3[i] <= c3[i-1] for i in range(1, 3)):
                        if b2['close'] > b2['open']:
                            cand_s += 1
            cand_s = min(cand_s, 3)

        score, sig = calc_score(
            rsi_1m, bb_1m,
            rsi_severe, rsi_oversold,
            bb_tight, bb_low,
            vol_s, cand_s,
            strong_t, watch_t
        )

        gain = future_gain(i5, 3)
        signals.append({
            'i5': i5, 'time': str(row5['time_key'])[11:19],
            'price': c5, 'rsi': rsi_1m, 'bb': bb_1m,
            'score': score, 'signal': sig,
            'vol': vol_s, 'cand': cand_s,
            'gain': gain
        })

    strong_signals = [s for s in signals if s['signal'] == '强买']
    watch_signals  = [s for s in signals if s['signal'] == '观望']

    # 真机会：信号后3根5min最大涨幅 > 0.3%
    true_strong = [s for s in strong_signals if s['gain'] is not None and s['gain'] > 0.3]
    false_strong = [s for s in strong_signals if s['gain'] is not None and s['gain'] <= 0.3]
    true_watch   = [s for s in watch_signals  if s['gain'] is not None and s['gain'] > 0.3]

    total_sig = len(strong_signals) + len(watch_signals)
    true_sig  = len(true_strong) + len(true_watch)
    false_sig = len(false_strong)
    precision = true_sig / total_sig * 100 if total_sig > 0 else 0

    # 最佳买点
    best = None
    if true_strong:
        best = max(true_strong, key=lambda s: s['gain'])
        best_str = ("%s 价格%.2f RSI=%.0f BB=%.0f%% "
                    "→ +%.2f%%" % (
                        best['time'], best['price'],
                        best['rsi'] or 0, best['bb'] or 0,
                        best['gain']))
    elif true_watch:
        best = max(true_watch, key=lambda s: s['gain'])
        best_str = ("%s 价格%.2f RSI=%.0f BB=%.0f%% "
                    "→ +%.2f%%(观望)" % (
                        best['time'], best['price'],
                        best['rsi'] or 0, best['bb'] or 0,
                        best['gain']))
    else:
        best_str = "无有效机会"

    # 标记
    if '基准' in name:
        mark = " ◀当前生产配置"
    else:
        mark = ""
    if true_sig > 0:
        tag = f"✅{true_sig}真/{false_sig}假"
    else:
        tag = f"⚠️0真/{false_sig}假"
    if '基准' in name:
        tag += " ◀"

    print(f"  %-55s %4d  %4d  %5d   %s" % (
        name, len(strong_signals), len(watch_signals), total_sig, tag))
    print(f"  %-55s  最佳: %s" % ("", best_str + mark))

    results_all.append({
        'name': name, 'strong': len(strong_signals),
        'watch': len(watch_signals), 'total': total_sig,
        'true': true_sig, 'false': false_sig,
        'precision': precision, 'best': best_str
    })

# ── 总结 ────────────────────────────────────────────────────────────────
print()
print(f"{'='*110}")
print(f"  结论总结（按精准度排序）")
print(f"{'='*110}")
results_all.sort(key=lambda x: (-x['true'], x['false']))
for r in results_all:
    precision_str = f"{r['precision']:.0f}%" if r['precision'] > 0 else "N/A"
    print(f"\n  {r['name']}")
    print(f"    强买信号 {r['strong']}个 | 观望信号 {r['watch']}个 | "
          f"精准度 {r['true']}/{r['total']}={precision_str}")
    print(f"    {r['best']}")
