#!/usr/bin/env python3
"""期权链数据探针（P0，只读）。

目的：确认富途 OpenD 期权链的真实覆盖与字段名，为「完整期权链优化」定数据基线。
只读，不下单、不写库、不触发 LLM。

用法：
    python scripts/live_trading/probe_option_chain.py US.SOXL
    python scripts/live_trading/probe_option_chain.py US.SOXL US.MU --max-expiries 4
    python scripts/live_trading/probe_option_chain.py US.SOXL --dump-cols   # 打印原始列名
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

# 富途 OptionType / quote 字段使用：这里仅做只读快照
def _probe_one(symbol: str, max_expiries: int, dump_cols: bool) -> int:
    import yaml
    from futu import OpenQuoteContext, RET_OK, OptionType

    cfg_path = BASE_DIR / 'config.yaml'
    cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
    futu_cfg = cfg.get('futu') or {}

    host = str(futu_cfg.get('host', '127.0.0.1'))
    port = int(futu_cfg.get('port', 11111))
    print(f"\n===== {symbol} 期权链探针 (OpenD {host}:{port}) =====")

    with OpenQuoteContext(host=host, port=port) as ctx:
        # 1) 到期列表
        ret, exp = ctx.get_option_expiration_date(symbol)
        if ret != RET_OK or exp is None or len(exp) == 0:
            print(f"[!] get_option_expiration_date 失败: {exp}")
            return 1
        exp_cols = list(exp.columns)
        # 取第一列当作到期日；兼容富途不同列名
        date_col = exp_cols[0]
        all_exp = sorted({str(r[date_col])[:10] for _, r in exp.iterrows()})
        today = __import__('datetime').date.today().isoformat()
        future_exp = [e for e in all_exp if e >= today]
        print(f"到期总数: {len(all_exp)} | 未来到期: {len(future_exp)}")
        print(f"到期列: {exp_cols} | 样例(前6): {future_exp[:6]}")
        if dump_cols:
            print(f"expiration 原始行样例:\n{exp.head(3).to_string()}")

        # 2) 近若干到期 × call/put 拉链
        shown_exp = 0
        for exp_date in future_exp[:max_expiries]:
            for opt_type, label in ((OptionType.CALL, 'CALL'), (OptionType.PUT, 'PUT')):
                ret, chain = ctx.get_option_chain(symbol, start=exp_date, end=exp_date,
                                                  option_type=opt_type)
                if ret != RET_OK or chain is None or len(chain) == 0:
                    print(f"  [{exp_date} {label}] 无数据: {chain}")
                    continue
                chain_cols = list(chain.columns)
                print(f"  [{exp_date} {label}] 行权档数={len(chain)} | 列={chain_cols}")
                if dump_cols:
                    print(f"    chain 行样例:\n{chain.head(2).to_string()}")

                # 3) 对中段一档取期权报价（与 signal_context 相同方式），确认 Greeks/OI/成交字段
                n = len(chain)
                if 'code' in chain_cols:
                    code = str(chain.iloc[n // 2]['code'])
                    try:
                        import futu.common.constant as _C
                        import futu.quote.quote_query as _Q
                        leg = _Q.OptionStrategyLeg()
                        leg.code = code
                        leg.action = _C.StrategyLegAction.BUY
                        leg.quantity = 1
                        ret_q, q = ctx.get_option_quote([leg])
                        if ret_q == RET_OK and q is not None and len(q) > 0:
                            qcols = list(q.columns)
                            print(f"    option_quote 列: {qcols}")
                            row = q.iloc[0]
                            for f in ('code', 'option_type', 'strike_price', 'expiry_date',
                                      'last_price', 'implied_volatility', 'delta', 'open_interest',
                                      'volume'):
                                if f in qcols:
                                    print(f"      {f} = {row[f]}")
                            if dump_cols:
                                print(q.head(1).to_string())
                        else:
                            print(f"    option_quote 失败: {q}")
                    except Exception as e:
                        print(f"    option_quote 异常: {e}")
                shown_exp += 1
            if shown_exp >= max_expiries:
                break
    print(f"===== {symbol} 探针完成 =====\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='期权链数据探针（只读）')
    parser.add_argument('symbols', nargs='+', help='标的，如 US.SOXL')
    parser.add_argument('--max-expiries', type=int, default=3,
                        help='探多少个到期（默认3）')
    parser.add_argument('--dump-cols', action='store_true',
                        help='打印原始行/列明细')
    args = parser.parse_args()

    rc = 0
    for s in args.symbols:
        rc |= _probe_one(s.upper(), args.max_expiries, args.dump_cols)
    return rc


if __name__ == '__main__':
    sys.exit(main())
