#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quant_us 启动前自检

检查内容：
  1. 配置文件与模式核对（REAL/SIMULATE、人工确认开关、LLM 配置）
  2. 监控股票池 / 仓位参数是否合理
  3. OpenD 是否运行（TCP 连通）
  4. 行情连接与美股行情权限
  5. 交易连接与 REAL 账户解锁状态（只读查询，绝不下单）
  6. 人工确认页在线（可选，--web 在系统启动后复检用）
  7. data/approvals 可写（人工确认模式的审计日志）

用法：
    python scripts/live_trading/preflight_check.py          # 启动前检查
    python scripts/live_trading/preflight_check.py --web    # 启动后复检（要求确认页在线）
    python scripts/live_trading/preflight_check.py --quiet  # 只看失败项
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request
from typing import List, Tuple

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, 'config.yaml')
WEB_HOST = '127.0.0.1'
WEB_PORT = 8899
OPEND_HOST = '127.0.0.1'
OPEND_PORT = 11111


class PreflightResult:
    def __init__(self):
        self.items: List[Tuple[str, str]] = []  # (level, message)  level: ok/warn/fail/info

    def ok(self, msg: str):
        self.items.append(('ok', msg))

    def warn(self, msg: str):
        self.items.append(('warn', msg))

    def fail(self, msg: str):
        self.items.append(('fail', msg))

    def info(self, msg: str):
        self.items.append(('info', msg))

    def summary(self) -> Tuple[int, int]:
        fails = sum(1 for lv, _ in self.items if lv == 'fail')
        warns = sum(1 for lv, _ in self.items if lv == 'warn')
        return fails, warns


ICONS = {'ok': '✅', 'warn': '⚠️ ', 'fail': '❌', 'info': 'ℹ️ '}


def check_config(r: PreflightResult) -> dict:
    """加载配置并核对运行模式"""
    if not os.path.exists(CONFIG_PATH):
        r.fail(f'config.yaml 不存在: {CONFIG_PATH}')
        return {}
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        r.fail(f'config.yaml 解析失败: {e}')
        return {}

    r.ok(f'配置文件已加载: {CONFIG_PATH}')

    # ---- 运行模式 ----
    trd_env = str(cfg.get('live_manager', {}).get('trd_env', 'SIMULATE')).upper()
    if trd_env not in ('SIMULATE', 'REAL'):
        r.fail(f'live_manager.trd_env 非法: {trd_env}（应为 SIMULATE/REAL）')
    else:
        r.info(f'交易环境配置: {trd_env}（注意：CLI 用 --real / --dry-run 决定本次是否真下单）')

    # ---- 人工确认 ----
    ha = cfg.get('trading', {}).get('live_trading', {}).get('human_approval', {})
    approval_on = bool(ha.get('enabled', False))
    if trd_env == 'REAL' and not approval_on:
        r.warn('trd_env=REAL 且 human_approval.enabled=false：启动后抄底评分达标会直接自动下单')
    elif trd_env == 'REAL' and approval_on:
        r.ok('人工确认已开启：买入信号只推送到确认页，点「下单」才执行')

    # ---- LLM ----
    llm_cfg = cfg.get('llm', {}) or {}
    if llm_cfg.get('enabled', False):
        api_key = os.environ.get('DEEPSEEK_API_KEY', '')
        cfg_key = str(llm_cfg.get('api_key', '') or '')
        if not api_key and (not cfg_key or cfg_key.startswith('${')):
            r.warn('llm.enabled=true 但 DEEPSEEK_API_KEY 未设置/未展开，确认页将显示"大模型无判定"')
        else:
            r.ok(f'LLM 配置就绪: {llm_cfg.get("model", "deepseek-chat")}')
    else:
        r.info('llm.enabled=false：确认页不会显示大模型判定（如需接入，开 enabled 并设置 DEEPSEEK_API_KEY）')

    return cfg


def check_dip_config(cfg: dict, r: PreflightResult):
    """核对监控股票池与买入参数"""
    dip = cfg.get('dip_buy', {}) or {}
    watch_list = dip.get('watch_list', []) or []
    if not watch_list:
        r.warn('dip_buy.watch_list 为空：监控器启动后不会买入任何股票')
    else:
        bad = [c for c in watch_list if not str(c).upper().startswith(('US.', 'HK.', 'SH.', 'SZ.'))]
        if bad:
            r.warn(f'watch_list 存在非富途格式代码: {bad}')
        r.info(f'监控股票池: {", ".join(str(c) for c in watch_list)}')

    try:
        threshold = float(dip.get('buy_threshold', dip.get('strong_buy_threshold', 8)))
        if not (1 <= threshold <= 15):
            r.warn(f'dip_buy.buy_threshold 超出常见范围(1-15): {threshold}')
        pos_usd = float(dip.get('position_size_usd', 0))
        if pos_usd <= 0:
            r.fail('dip_buy.position_size_usd 必须 > 0')
        max_pos = int(dip.get('max_positions', 3))
        if max_pos < 1:
            r.fail('dip_buy.max_positions 必须 ≥ 1')
    except (TypeError, ValueError) as e:
        r.fail(f'dip_buy 参数非数值: {e}')


def check_opend_tcp(r: PreflightResult) -> bool:
    """检查 OpenD 是否在监听"""
    try:
        with socket.create_connection((OPEND_HOST, OPEND_PORT), timeout=3):
            r.ok(f'OpenD 端口可达: {OPEND_HOST}:{OPEND_PORT}')
        return True
    except OSError as e:
        r.fail(f'OpenD 未运行或端口不可达: {OPEND_HOST}:{OPEND_PORT} ({e})')
        r.info('请先启动富途 OpenD 并登录行情/交易权限')
        return False


def check_quote(cfg: dict, r: PreflightResult):
    """行情连接与美股行情权限（只读）"""
    try:
        from futu import OpenQuoteContext, SubType, RET_OK
    except ImportError as e:
        r.fail(f'futu-api 未安装: {e}，请 pip install futu-api')
        return

    dip = cfg.get('dip_buy', {}) or {}
    codes = dip.get('watch_list', []) or []
    code = str(codes[0]).upper() if codes else 'US.SOXL'

    try:
        with OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT) as ctx:
            ctx.subscribe([code], [SubType.QUOTE], subscribe_push=False)
            ret, snap = ctx.get_market_snapshot([code])
            if ret == RET_OK and snap is not None and len(snap) > 0:
                row = snap.iloc[0]
                price = row.get('last_price', 0) or row.get('pre_price', 0) or 0
                r.ok(f'行情连接正常，{code} 最新价 ${float(price):.2f}')
            else:
                r.warn(f'行情权限/快照获取异常: {snap}（检查 OpenD 是否已登录行情）')
    except Exception as e:
        r.fail(f'行情连接失败: {type(e).__name__}: {e}')


def check_trade_unlock(cfg: dict, r: PreflightResult):
    """交易连接与解锁状态（只读 accinfo 查询）"""
    try:
        from futu import OpenSecTradeContext, TrdMarket, TrdEnv, RET_OK
    except ImportError as e:
        r.fail(f'futu-api 未安装: {e}')
        return

    trd_env_str = str(cfg.get('live_manager', {}).get('trd_env', 'SIMULATE')).upper()
    trd_env = TrdEnv.REAL if trd_env_str == 'REAL' else TrdEnv.SIMULATE

    try:
        with OpenSecTradeContext(
            host=OPEND_HOST, port=OPEND_PORT, filter_trdmarket=TrdMarket.US
        ) as ctx:
            ret, data = ctx.accinfo_query(trd_env=trd_env)
            if ret == RET_OK:
                if trd_env == TrdEnv.REAL:
                    r.ok('REAL 交易账户已解锁（accinfo 查询通过）')
                else:
                    r.ok('SIMULATE 交易账户连接正常（无需解锁）')
                try:
                    cash = float(data['cash'][0]) if data is not None and len(data) else 0.0
                    if cash > 0:
                        r.info(f'可用资金: ${cash:,.2f}')
                except Exception:
                    pass
            else:
                msg = str(data)
                if trd_env == TrdEnv.REAL:
                    r.fail(f'REAL 交易未解锁/查询失败: {msg}')
                    r.info('请在 OpenD 界面右上角手动解锁真实交易（GUI OpenD 不支持 API 解锁）')
                else:
                    r.fail(f'SIMULATE 交易账户查询失败: {msg}')
    except Exception as e:
        r.fail(f'交易连接失败: {type(e).__name__}: {e}')


def check_approval_dir(r: PreflightResult, cfg: dict):
    """人工确认模式下的审计日志目录可写"""
    ha = cfg.get('trading', {}).get('live_trading', {}).get('human_approval', {})
    if not ha.get('enabled', False):
        return
    log_dir = os.path.join(BASE_DIR, 'data', 'approvals')
    try:
        os.makedirs(log_dir, exist_ok=True)
        probe = os.path.join(log_dir, '.preflight_write_test')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(probe)
        r.ok(f'确认页审计目录可写: {log_dir}')
    except Exception as e:
        r.fail(f'确认页审计目录不可写: {log_dir} ({e})')


def check_web(require_web: bool, cfg: dict, r: PreflightResult):
    """确认页在线检查（--web 时必检）"""
    ha = cfg.get('trading', {}).get('live_trading', {}).get('human_approval', {})
    approval_on = bool(ha.get('enabled', False))
    base = f'http://{WEB_HOST}:{WEB_PORT}'

    try:
        with urllib.request.urlopen(f'{base}/api/approvals', timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        page = urllib.request.urlopen(f'{base}/approvals', timeout=3)
        html = page.read().decode('utf-8')
        page_ok = '交易确认台' in html

        if not data.get('ok'):
            r.warn(f'确认页 API 异常: {data.get("error")}')
        else:
            env = data.get('env', '-')
            llm_on = bool(data.get('llm_enabled', False))
            r.ok(f'确认页在线: {base}/approvals (env={env}, llm={llm_on})')
            if not approval_on:
                r.warn('页面在线但 human_approval.enabled=false：系统仍会自动下单，页面只读')
        if not page_ok:
            r.warn('确认页 HTML 加载异常（页面内容不完整）')
    except Exception as e:
        if require_web:
            r.fail(f'确认页不可达: {base} ({type(e).__name__}: {e})')
            r.info('请先运行 python run_all.py 启动 Web 服务，再执行本脚本 --web 复检')
        else:
            r.info(f'Web 服务当前未运行（正常：由 run_all.py 启动）。'
                   f'启动后确认页为 {base}/approvals；可用 --web 复检')


def main():
    parser = argparse.ArgumentParser(description='quant_us 启动前自检')
    parser.add_argument('--web', action='store_true', help='要求确认页在线（系统启动后复检）')
    parser.add_argument('--quiet', action='store_true', help='只输出告警与失败项')
    args = parser.parse_args()

    r = PreflightResult()

    cfg = check_config(r)
    if cfg:
        check_dip_config(cfg, r)
        check_approval_dir(r, cfg)
        check_web(args.web, cfg, r)

        if check_opend_tcp(r):
            check_quote(cfg, r)
            check_trade_unlock(cfg, r)
    else:
        r.fail('配置加载失败，跳过 OpenD / 行情 / 交易检查')

    fails, warns = r.summary()

    print()
    print('=' * 64)
    print('  quant_us 启动前自检结果')
    print('=' * 64)
    for level, msg in r.items:
        if args.quiet and level in ('ok', 'info'):
            continue
        print(f'{ICONS.get(level, "•")} [{level.upper():<4}] {msg}')
    print('-' * 64)
    print(f'  共 {len(r.items)} 项：失败 {fails}，警告 {warns}')
    if fails:
        print('  ❌ 存在失败项，建议修复后再启动实盘')
    elif warns:
        print('  ⚠️  存在警告，请逐条确认是否可接受')
    else:
        print('  ✅ 全部通过，可以启动')
    print('=' * 64)

    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
