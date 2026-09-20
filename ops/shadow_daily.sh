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
#   SHADOW_BASE        影子实验根目录（默认 $US/data/portfolio_shadow）
#                      —— **必须可覆盖**：一个不能被指向别处的生产入口等于没法预演，
#                      而它每天在无人看管下跑（硬编码路径/身份曾导致"每天静默 SKIP"）。
#   SHADOW_EXPERIMENT  冻结实验 id；留空则自动发现（0 个→跳过，多个→报错）
#   SHADOW_EVIDENCE    已导入的规范证据存储
#   SHADOW_DIGEST_DIR  每日市场日报（HTML）的发布目录
#   SHADOW_LIVE_ETF    live ETF 快照目录
#   SHADOW_MODEL       real | fixture（默认 real —— 会真实计费）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
US="$ROOT/quant_us-main"
PY=/usr/bin/python3

BASE="${SHADOW_BASE:-$US/data/portfolio_shadow}"
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

# ---- P0 观察流水：**每次运行都留下一行，包括早退的路径** ----
# 原先只在脚本末尾追加，于是「漏跑」「刷新失败」「跳过」「从未跑过」在这个文件里
# 长得一模一样。2026-09-20 实测：生产 `p0-watch.log` 是 0 字节，而当时无法从它
# 判断到底是哪一种 —— 与这个文件存在的理由（漏跑看得见）正好相反。
# 早退分支各写一行之后，空文件只意味着一件事：**这次运行根本没发生**。
P0_LOG="$BASE/p0-watch.log"
p0() { printf '%s\t%s\n' "$STAMP" "$1" >> "$P0_LOG"; }

cd "$US" || exit 1

# ---- 运行锁：避免手动运行与后台任务重叠（mkdir 是原子的）----
LOCK="$EVIDENCE_DIR/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || echo '?')
  if [ "$HOLDER" != '?' ] && kill -0 "$HOLDER" 2>/dev/null; then
    # **`${HOLDER}` 的花括号是必需的，不是风格**：macOS 自带 bash 3.2 会把 `$VAR`
    # 紧邻的 UTF-8 字节吞进变量名（`$HOLDER，` → 变量 `HOLDER\xef\xbc\x8c`），
    # 在 `set -u` 下直接 `unbound variable` 退出 1。实测复现过。
    echo "$STAMP SKIP 已有运行在进行（pid ${HOLDER}，锁 ${LOCK}）"
    p0 "SKIP 已有运行在进行（pid ${HOLDER}）"
    exit 0
  fi
  echo "$STAMP 接管过期锁（持有者 $HOLDER 已不在）"
  rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null
  mkdir "$LOCK" 2>/dev/null || { echo "$STAMP FAIL 无法获取锁"; p0 "FAIL 无法获取锁"; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# ---- 实验：显式指定则必须已冻结；未指定则自动发现 ----
if [ -n "${SHADOW_EXPERIMENT:-}" ]; then
  MANIFEST="$BASE/$SHADOW_EXPERIMENT/manifest.json"
  if [ ! -f "$MANIFEST" ]; then
    echo "$STAMP FAIL 指定实验 ${SHADOW_EXPERIMENT} 未冻结（缺 ${MANIFEST}）"
    p0 "FAIL 指定实验 ${SHADOW_EXPERIMENT} 未冻结"
    exit 1
  fi
else
  FOUND=$(ls -d "$BASE"/*/manifest.json 2>/dev/null || true)
  COUNT=$(printf '%s' "$FOUND" | grep -c . || true)
  if [ "$COUNT" -eq 0 ]; then
    echo "$STAMP SKIP 尚无已冻结实验（${BASE}/*/manifest.json）—— 入组后自动开始"
    p0 "SKIP 尚无已冻结实验"
    exit 0
  fi
  if [ "$COUNT" -ne 1 ]; then
    echo "$STAMP FAIL 发现 ${COUNT} 个已冻结实验，无法判定跑哪个 —— 请用 SHADOW_EXPERIMENT 指定："
    printf '  %s\n' $FOUND
    p0 "FAIL 发现 ${COUNT} 个已冻结实验，无法判定跑哪个"
    exit 1
  fi
  MANIFEST="$FOUND"
  echo "$STAMP 自动发现实验 $(basename "$(dirname "$MANIFEST")")"
fi

echo "$STAMP refresh-market-data（只追加，不改历史）"
"$PY" -m scripts.portfolio_shadow.refresh_data --live-dir "$LIVE_ETF" || {
  echo "$STAMP FAIL refresh 失败，本次不跑 —— 宁可停一天，也不在陈旧行情上做决策"
  p0 "FAIL refresh 失败，本次不跑"
  exit 1
}

# 日报 → 市场级证据 → 追加进证据存储。`--append` 是「首次导入为准」：同一天重跑不会
# 把已有记录的 observed_at 刷新成今天。取「≤ 今天的最近一份」——日报不一定每个交易日都有。
echo "$STAMP ingest-digest session=$TODAY dir=$DIGEST_DIR"
DIGEST_JSONL="$INBOX/$TODAY.jsonl"
if [ ! -d "$DIGEST_DIR" ]; then
  echo "$STAMP 注意：日报目录不存在（${DIGEST_DIR}）—— 本次不带市场级证据"
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
STATUS_LINE="$("$PY" "$ROOT/ops/shadow_status.py" --manifest "$MANIFEST" --output "$BASE" \
  --run-json "$RUNS/$TODAY.json" 2>/dev/null || echo '状态判定失败')"
echo "$STAMP 状态：$STATUS_LINE"

# ---- P0 观察：流水按日累积，并在**值得你知道**的状态上弹一次通知 ----
# 为什么不通知常态化状态：`NO_OPPORTUNITIES` 是当前常态，天天弹等于没弹，
# 真正要看见的是「链路走没走到模型」——那是 P0 的验收点。
p0 "$STATUS_LINE"
STATE="${STATUS_LINE%% *}"
case "$STATE" in
  DECIDED|SETTLED|MODEL_FAILED|DEADLINE_MISSED|NO_EVIDENCE)
    # **告警通道走 `watchdog.py --notify`，不再自己写 AppleScript**。
    # 原先这里是 `display notification` —— 那正是 2026-09-19 深夜实测证明**静默失效**
    # 的通道（rc=0、stderr 空、用户一条都没看到），而它写得比那次修复更早，于是漏掉了修复。
    # 复用同一份实现还顺带拿到两件事：字符串转义，以及**"有没有被人看到"的记录**。
    # 失败不阻断（launchd 上下文里可能无人应答）。
    "$PY" "$ROOT/ops/watchdog.py" --notify "影子账户 ${STATE}" "$STATUS_LINE" || true
    ;;
esac
