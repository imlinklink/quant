#!/usr/bin/env python3
"""期权多标的诊断（只读）：单连接遍历，判断哪些标的有期权、拉取卡在哪步。

用途：当 run_daily_selection 里某只期权视角拉取失败时，用本脚本区分
「该标的本就没有期权」vs「有但接口调用方式不对」。
用法：
    python scripts/live_trading/probe_option_multi.py
    python scripts/live_trading/probe_option_multi.py US.SOXL US.MU
"""
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))


def main():
    import yaml
    from futu import OpenQuoteContext, RET_OK, OptionType

    cfg = yaml.safe_load((BASE_DIR / 'config.yaml').read_text(encoding='utf-8')) or {}
    fc = cfg.get('futu', {})

    args = sys.argv[1:]
    if args:
        codes = [c.upper() for c in args]
    else:
        # 默认 = 当前股票池（dip_buy.watch_list）
        codes = [str(c).upper() for c in cfg.get('dip_buy', {}).get('watch_list', [])]
    if not codes:
        print('无标的：请在参数传入或 config dip_buy.watch_list')
        return 1

    today = date.today().isoformat()
    with OpenQuoteContext(host=str(fc.get('host', '127.0.0.1')),
                          port=int(fc.get('port', 11111))) as ctx:
        for s in codes:
            ret, exp = ctx.get_option_expiration_date(s)
            if ret != RET_OK or exp is None or len(exp) == 0:
                print(f"[{s}] expiration 失败 ret={ret} msg={exp}")
                continue
            date_col = list(exp.columns)[0]
            exps = sorted({str(r[date_col])[:10] for _, r in exp.iterrows()})
            future = [e for e in exps if e >= today]
            print(f"[{s}] 到期 {len(exps)} 个, 最近={future[:2] if future else exps[:2]}")
            if not future:
                continue
            ret_c, chain = ctx.get_option_chain(
                s, start=future[0], end=future[0], option_type=OptionType.CALL)
            if ret_c == RET_OK and chain is not None and len(chain):
                print(f"    近月 CALL 链 {len(chain)} 档 ✓")
            else:
                print(f"    近月 CALL 链失败 ret={ret_c} msg={chain}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
