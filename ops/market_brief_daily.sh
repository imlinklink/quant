#!/usr/bin/env bash
# 盘前简报每日运行（简报 + 选股建议 + 结果回填，即 `pipeline.py --mode morning`）。
#
# **为什么需要这个脚本，而不是直接跑 pipeline**：这台机器**早上 09:20 才到公司、那时才苏醒
# 并连上网络**。而 launchd 的补跑发生在**醒来那一瞬间** —— 2026-09-24 实测：作业 09:03 被
# 唤醒补跑，`run_market_brief.py` 的 LLM 调用直接 DNS 失败（`Errno 8 nodename nor servname`），
# 简报没生成、看护报了一整天红。
#
# 所以本脚本先**等网络与 OpenD 就绪**再跑：
#   · DNS 能解析 LLM 的主机（从 config.yaml 的 llm 段取，取不到就退回一个已知主机名）；
#   · OpenD（127.0.0.1:11111）能建 TCP 连接（简报要取行情）。
# 两者都就绪才继续；超时就如实失败（宁可报错，也不在半截网络上跑出一份残缺简报）。
#
# 每个早退路径都写一行流水 —— 与 `shadow_daily.sh` / `forward_arms_daily.sh` 同一条教训：
# 只在末尾写的话，「漏跑」「等超时」「跳过」在流水里长得一模一样。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/usr/bin/python3
STAMP="$(date -Iseconds)"
# 等多久：机器 09:20 醒、网络通常几秒内就绪；给足余量但不无限等（launchd 不做超时管理）
WAIT_SECONDS="${BRIEF_WAIT_SECONDS:-900}"
LOG="$ROOT/ops/logs/market_brief_watch.log"
mkdir -p "$(dirname "$LOG")"
watch() { printf '%s\t%s\n' "$STAMP" "$1" >> "$LOG"; }

cd "$ROOT" || { watch "FAIL 无法进入 ${ROOT}"; exit 1; }

# ---- 幂等：今天已经有简报就直接退出 ----
# 本作业有多次尝试（第一次撞上网络未就绪时，后面几次兜底）。**必须幂等**，否则每次重试
# 都会再调一次 LLM 生成一份新简报、覆盖当天那份 —— 那是白花钱，也让"当天简报"变成
# "最后一次尝试的产物"（评审依据会随重试漂移）。
BRIEF_PATH="$ROOT/quant_us-main/data/market_brief/latest.json"
EXISTING="$("$PY" -c "
import json
from pathlib import Path
try: print(json.loads(Path('$BRIEF_PATH').read_text(encoding='utf-8')).get('date') or '')
except Exception: print('')")"
if [ "$EXISTING" = "$(date +%F)" ]; then
  echo "$STAMP 今天已有简报（${EXISTING}），跳过"
  watch "SKIP 今天已有简报（${EXISTING}）"
  exit 0
fi

# ---- 等网络与 OpenD 就绪（与 shadow_daily / forward_arms 共用**同一份**实现）----
# 三处各写一份就是本仓库吃过多次亏的"同一件事两份定义"——所以抽成 `ops/wait_ready.py`。
READY_OUT="$("$PY" "$ROOT/ops/wait_ready.py" --seconds "$WAIT_SECONDS")"
RC=$?
echo "$STAMP $READY_OUT"
if [ "$RC" -ne 0 ]; then
  watch "FAIL 网络/OpenD 未就绪，本次不跑 —— $READY_OUT"
  exit "$RC"
fi
watch "READY $READY_OUT"

# ---- 跑简报 ----
OUT="$("$PY" ops/pipeline.py --mode morning --markets us 2>&1)"
RC=$?
printf '%s\n' "$OUT" | tail -20
if [ "$RC" -ne 0 ]; then
  watch "FAIL 简报流程退出码 ${RC}（见上方输出）"
  exit "$RC"
fi
# 简报本身有没有真的更新，以产物日期为准（退出码 0 不代表生成了今天的简报）
BRIEF_DATE="$("$PY" -c "
import json,sys
from pathlib import Path
p = Path('$ROOT/quant_us-main/data/market_brief/latest.json')
try: print(json.loads(p.read_text(encoding='utf-8')).get('date') or '?')
except Exception: print('?')")"
if [ "$BRIEF_DATE" = "$(date +%F)" ]; then
  watch "OK 简报日期=${BRIEF_DATE}"
else
  watch "FAIL 简报日期=${BRIEF_DATE} 不是今天（${BRIEF_DATE:+流程跑完了但没产出今天的简报}）"
  exit 1
fi
