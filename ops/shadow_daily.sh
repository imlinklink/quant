#!/usr/bin/env bash
# R/L 双影子账户每日前向运行（设计 §3 的窗口：T 收盘后 → T+1 开盘前）。
#
# 排程用 LaunchAgent（`ops/install_launchd.py`），**不用 cron**，两条实测原因：
#   · macOS 的 cron 不补跑睡过的任务 —— 而本窗口只有约 17 小时，跳过就丢一天；
#   · 且 /usr/sbin/cron 没有 ~/Documents 的 TCC 权限（整个 ops cron 块曾因此失败一周）。
# 另注：LaunchAgent 的补跑是"唤醒后执行"，**不保证在截止前醒来** —— 所以本脚本会记录
# 每个应跑 session 的终态，漏跑看得见（见 §运行结果）。
#
# 环境变量（都可覆盖）：
#   SHADOW_EXPERIMENT  冻结实验 id；留空则自动发现（0 个→跳过，多个→报错）
#   SHADOW_EVIDENCE    已导入的规范证据存储
#   SHADOW_DIGEST_DIR  每日市场日报（HTML）的发布目录
#   SHADOW_LIVE_ETF    live ETF 快照目录
#   SHADOW_MODEL       real | fixture（默认 real —— 会真实计费）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
US="$ROOT/quant_us-main"
PY=/usr/bin/python3

BASE="$US/data/portfolio_shadow"
EVIDENCE="${SHADOW_EVIDENCE:-$BASE/evidence/live.csv}"
EVIDENCE_DIR="$(dirname "$EVIDENCE")"
INBOX="$EVIDENCE_DIR/inbox"
LIVE_ETF="${SHADOW_LIVE_ETF:-$US/data/medium_term/LIVE-ETF-20260917}"
MODEL="${SHADOW_MODEL:-real}"
# 日报由另一个项目产出，**发布到 ~ 下**（不发布到 ~/Documents：后台调度读不到，TCC）
DIGEST_DIR="${SHADOW_DIGEST_DIR:-$HOME/quant-inputs/market-digest}"
RUNS="$EVIDENCE_DIR/runs"
STAMP="$(date -Iseconds)"
TODAY=$(date +%F)
mkdir -p "$RUNS" "$INBOX"

cd "$US" || exit 1

# ---- 运行锁：避免手动运行与后台任务重叠（mkdir 是原子的）----
LOCK="$EVIDENCE_DIR/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || echo '?')
  if [ "$HOLDER" != '?' ] && kill -0 "$HOLDER" 2>/dev/null; then
    echo "$STAMP SKIP 已有运行在进行（pid $HOLDER，锁 $LOCK）"
    exit 0
  fi
  echo "$STAMP 接管过期锁（持有者 $HOLDER 已不在）"
  rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null
  mkdir "$LOCK" 2>/dev/null || { echo "$STAMP FAIL 无法获取锁"; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# ---- 实验：显式指定则必须已冻结；未指定则自动发现 ----
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
  echo "$STAMP 自动发现实验 $(basename "$(dirname "$MANIFEST")")"
fi

echo "$STAMP refresh-market-data（只追加，不改历史）"
"$PY" -m scripts.portfolio_shadow.refresh_data --live-dir "$LIVE_ETF" || {
  echo "$STAMP FAIL refresh 失败，本次不跑 —— 宁可停一天，也不在陈旧行情上做决策"
  exit 1
}

# 日报 → 市场级证据 → 追加进证据存储。`--append` 是「首次导入为准」：同一天重跑不会
# 把已有记录的 observed_at 刷新成今天。取「≤ 今天的最近一份」——日报不一定每个交易日都有。
echo "$STAMP ingest-digest session=$TODAY dir=$DIGEST_DIR"
DIGEST_JSONL="$INBOX/$TODAY.jsonl"
if [ ! -d "$DIGEST_DIR" ]; then
  echo "$STAMP 注意：日报目录不存在（$DIGEST_DIR）—— 本次不带市场级证据"
elif [ -z "$(ls -A "$DIGEST_DIR" 2>/dev/null)" ]; then
  echo "$STAMP 注意：日报目录为空 —— 本次不带市场级证据（模型会因证据不足弃权）"
fi
"$PY" -m scripts.portfolio_shadow.market_digest --session "$TODAY" --dir "$DIGEST_DIR" \
  --output "$DIGEST_JSONL" || echo "$STAMP 注意：日报抽取失败"
if [ -f "$DIGEST_JSONL" ]; then
  "$PY" -m scripts.portfolio_shadow.cli import-evidence --source "$DIGEST_JSONL" \
    --output "$EVIDENCE" --append || echo "$STAMP 注意：日报入库失败"
fi

echo "$STAMP run-daily model=$MODEL"
ARGS=(--manifest "$MANIFEST" --output "$BASE" --model "$MODEL"
      --etf-raw "$LIVE_ETF/etf_raw_daily.csv.gz")
[ -f "$EVIDENCE" ] && ARGS+=(--evidence "$EVIDENCE")
"$PY" -m scripts.portfolio_shadow.cli run-daily "${ARGS[@]}" | tee "$RUNS/$TODAY.json"
# 就绪门的故障态（源不一致 / 没人真的在请求 / 数据比日历新 / 日历不可用）让 run-daily 以
# 非 0 退出。`set -uo pipefail` 没有 `-e`，脚本不会因此中断 —— 但必须在这里留一行，
# 否则「作业正常结束」看起来和「门拦住了」一模一样（2026-09-18 事故的形态）。
RC=${PIPESTATUS[0]}
[ "$RC" -ne 0 ] && echo "$STAMP 注意：run-daily 退出码 $RC —— 数据未就绪或就绪门故障，见上方的 gate/skipped_by_gate"

# ---- 运行结果：把「今天到底完成了什么」写成一行，漏跑/无候选/无证据/模型失败能区分 ----
echo "$STAMP 状态：$("$PY" "$ROOT/ops/shadow_status.py" --manifest "$MANIFEST" --output "$BASE" \
  --run-json "$RUNS/$TODAY.json" 2>/dev/null || echo '状态判定失败')"
