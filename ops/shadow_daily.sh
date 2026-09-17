#!/usr/bin/env bash
# R/L 双影子账户每日前向运行（设计 §3 的窗口：T 收盘后 → T+1 开盘前）。
#
# 排程在美股收盘后的北京早晨（与 ops 里既有的「美东收盘后(北京 08:35)」同槽）：
# 此刻 T 的行情已落定，而 T+1 的决策截止（美东 09:20）还有约 12 小时。
#
# 环境变量（都可覆盖）：
#   SHADOW_EXPERIMENT  冻结实验 id（必需，缺则只记一行日志后退出 0）
#   SHADOW_EVIDENCE    已导入的规范证据存储（可选）
#   SHADOW_LIVE_ETF    live ETF 快照目录
#   SHADOW_MODEL       real | fixture（默认 real —— 会真实计费）
#
# **未冻结实验时不报错**：定时任务不该因为还没入组就每天刷失败日志。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
US="$ROOT/quant_us-main"
PY=/usr/bin/python3

BASE="$US/data/portfolio_shadow"
EVIDENCE="${SHADOW_EVIDENCE:-$BASE/evidence/live.csv}"
LIVE_ETF="${SHADOW_LIVE_ETF:-$US/data/medium_term/LIVE-ETF-20260917}"
MODEL="${SHADOW_MODEL:-real}"
STAMP="$(date -Iseconds)"

cd "$US" || exit 1

# 实验 id：显式指定则必须已冻结（配置错就该响亮地失败）；未指定则自动发现 ——
# 硬编码一个 id 会在 id 变化时**静默 SKIP 到永远**（SKIP 是 exit 0，没有任何提示）。
if [ -n "${SHADOW_EXPERIMENT:-}" ]; then
  MANIFEST="$BASE/$SHADOW_EXPERIMENT/manifest.json"
  if [ ! -f "$MANIFEST" ]; then
    echo "$STAMP FAIL 指定实验 ${SHADOW_EXPERIMENT} 未冻结（缺 ${MANIFEST}）"
    exit 1
  fi
else
  FOUND=$(ls -d "$BASE"/*/manifest.json 2>/dev/null || true)
  COUNT=$(printf '%s' "$FOUND" | grep -c . || true)
  if [ "$COUNT" -eq 0 ]; then
    echo "$STAMP SKIP 尚无已冻结实验（${BASE}/*/manifest.json）—— 入组后自动开始"
    exit 0
  fi
  if [ "$COUNT" -ne 1 ]; then
    echo "$STAMP FAIL 发现 ${COUNT} 个已冻结实验，无法判定跑哪个 —— 请用 SHADOW_EXPERIMENT 指定："
    printf '  %s\n' $FOUND
    exit 1
  fi
  MANIFEST="$FOUND"
  EXPERIMENT=$(basename "$(dirname "$MANIFEST")")
  echo "$STAMP 自动发现实验 ${EXPERIMENT}"
fi

echo "$STAMP refresh-market-data（只追加，不改历史）"
"$PY" -m scripts.portfolio_shadow.refresh_data --live-dir "$LIVE_ETF" || {
  echo "$STAMP FAIL refresh 失败，本次不跑 —— 宁可停一天，也不在陈旧行情上做决策"
  exit 1
}

echo "$STAMP run-daily model=$MODEL"
ARGS=(--manifest "$MANIFEST" --output "$BASE" --model "$MODEL"
      --etf-raw "$LIVE_ETF/etf_raw_daily.csv.gz")
if [ -f "$EVIDENCE" ]; then
  ARGS+=(--evidence "$EVIDENCE")
else
  echo "$STAMP 注意：无证据存储（${EVIDENCE}），包会是 LLM_INSUFFICIENT ⇒ 模型不会被调用"
fi
"$PY" -m scripts.portfolio_shadow.cli run-daily "${ARGS[@]}"
