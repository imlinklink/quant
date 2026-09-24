#!/usr/bin/env bash
# 三臂前向观察每日运行（登记 B3-FORWARD-20260921）。
#
# 排程用 LaunchAgent（`ops/install_launchd.py`），与 `shadow_daily.sh` 同样的三条实测理由：
#   · macOS 的 cron 不补跑睡过的任务，而本窗口只有约 17 小时，跳过就丢一天；
#   · LaunchAgent 的补跑是"唤醒后执行"，**不保证在截止前醒来** ⇒ 所以本脚本把每次运行的
#     终态写进流水，漏跑看得见；
#   · 个股日线在收盘后数小时才出（实测北京 10:45 时仍停在 T-1），排程取 17:40 北京。
#
# **本脚本只推进、不决策**：三臂跑同一个 B3，只差宇宙。起点由 `forward_arms start` 写死，
# 这里永远只向前走（`run-day` 自己会拒"目标早于起点"）。
#
# 环境变量（都可覆盖）：
#   FORWARD_ROOT      观察根目录（默认 $US/data/strategy_diagnostics/forward）
#                     —— **必须可覆盖**：一个不能被指向别处的生产入口等于没法预演。
#   FORWARD_LIVE_ETF  live ETF 快照目录（交易日历与市场门的来源）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
US="$ROOT/quant_us-main"
PY=/usr/bin/python3

BASE="${FORWARD_ROOT:-$US/data/strategy_diagnostics/forward}"
LIVE_ETF="${FORWARD_LIVE_ETF:-$US/data/medium_term/LIVE-ETF-20260917}"
STAMP="$(date -Iseconds)"
mkdir -p "$BASE"

# ---- 观察流水：**每次运行都留一行，包括早退路径** ----
# 与 `shadow_daily.sh` 同一条教训：只在末尾写的话，「漏跑」「刷新失败」「跳过」「从未跑过」
# 在这个文件里长得一模一样。早退分支各写一行之后，空文件只意味着一件事：**这次根本没跑**。
WATCH_LOG="$BASE/forward-watch.log"
watch() { printf '%s\t%s\n' "$STAMP" "$1" >> "$WATCH_LOG"; }

cd "$US" || exit 1

# ---- 运行锁（mkdir 原子）----
LOCK="$BASE/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER=$(cat "$LOCK/pid" 2>/dev/null || echo '?')
  if [ "$HOLDER" != '?' ] && kill -0 "$HOLDER" 2>/dev/null; then
    # `${HOLDER}` 的花括号是必需的：macOS 自带 bash 3.2 会把紧邻的 UTF-8 字节吞进变量名，
    # 在 `set -u` 下直接 unbound variable 退出 1（实测复现过）。
    echo "$STAMP SKIP 已有运行在进行（pid ${HOLDER}）"
    watch "SKIP 已有运行在进行（pid ${HOLDER}）"
    exit 0
  fi
  echo "$STAMP 接管过期锁（持有者 $HOLDER 已不在）"
  rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null
  mkdir "$LOCK" 2>/dev/null || { echo "$STAMP FAIL 无法获取锁"; watch "FAIL 无法获取锁"; exit 1; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# ---- 观察是否已起步：未起步跳过；显式指定却不存在则失败 ----
if [ -n "${FORWARD_ROOT:-}" ] && [ ! -f "$BASE/arms.json" ]; then
  echo "$STAMP FAIL 指定观察根 ${BASE} 未起步（缺 arms.json）—— 这是配置错误，不是「今天没事」"
  watch "FAIL 指定观察根未起步"
  exit 1
fi
if [ ! -f "$BASE/arms.json" ]; then
  FOUND=$(ls -d "$BASE"/*/arms.json 2>/dev/null || true)
  COUNT=$(printf '%s' "$FOUND" | grep -c . || true)
  if [ "$COUNT" -eq 0 ]; then
    echo "$STAMP SKIP 三臂前向观察尚未起步（${BASE}）—— 跑 forward_arms start 后才开始计时"
    watch "SKIP 尚未起步"
    exit 0
  fi
  if [ "$COUNT" -ne 1 ]; then
    echo "$STAMP FAIL 发现 ${COUNT} 个观察根，无法判定跑哪个 —— 请用 FORWARD_ROOT 指定："
    printf '  %s\n' $FOUND
    watch "FAIL 发现 ${COUNT} 个观察根"
    exit 1
  fi
  BASE="$(dirname "$FOUND")"
  echo "$STAMP 自动发现观察根 ${BASE}"
fi

# ---- 刷新公司行动（**只追加**；改了历史就停）----
# **必须先于行情**：观察期内新出现的拆股不被应用会让价格"假摔"（3:1 表现为 −67%），
# 直接触发**假止损** —— 那不是偏差，是会污染结论的雷。
ACTIONS="$("$PY" -c "import json,sys; print(json.load(open('$BASE/arms.json')).get('actions',''))")"
if [ -z "$ACTIONS" ]; then
  echo "$STAMP FAIL arms.json 里没有 actions 路径 —— 观察根是旧版本起的？"
  watch "FAIL arms.json 缺 actions 路径"
  exit 1
fi
CODES="$("$PY" -c "
import sys; sys.path.insert(0, '.')
from scripts.portfolio_shadow.refresh_data import master_codes
from scripts.strategy_diagnostics.forward_arms import arm_names
print(' '.join(sorted(master_codes(arm_names()['B']))))")"
TMP_ACTIONS="$(mktemp -d)"
# ---- 先等网络与 OpenD 就绪 ----
# 这台机器会频繁进出睡眠，每次醒来都把网络与 Futu 连接切断（服务日志同一刻成批
# `Disconnected: … reason=KeepAliveFail`）。实测 2026-09-23 21:08:54 本作业就是撞上
# 那个窗口：公司行动导入 22~25 只取数失败 ⇒ 三臂**连续两天没推进**。
# 而手动重跑同一个导入器是 0 失败 ⇒ 不是代码/数据问题，是时刻问题。
echo "$STAMP wait-ready"
READY_OUT="$("$PY" "$ROOT/ops/wait_ready.py" $( [ "${FORWARD_WAIT_SECONDS:-}" ] && echo "--seconds ${FORWARD_WAIT_SECONDS}" ))"
RC=$?
echo "$STAMP $READY_OUT"
if [ "$RC" -ne 0 ]; then
  watch "FAIL 网络/OpenD 未就绪，本次不推进 —— $READY_OUT"
  rm -rf "$TMP_ACTIONS"; exit "$RC"
fi

echo "$STAMP refresh-actions codes=$(printf '%s' "$CODES" | wc -w) 只追加"
# **导入器的输出要留着**：原先 `>/dev/null` 把 `fetch_failures` 的明细（每个代码的
# 确切错误）一起丢了，只剩一句"22 只失败"，无法诊断。现在落文件并在失败时回显。
IMPORT_LOG="$BASE/actions_import.log"
"$PY" -m scripts.data.import_corporate_actions_from_futu --codes $CODES \
  --output-dir "$TMP_ACTIONS" --archive-root "$BASE/actions_archive" \
  >> "$IMPORT_LOG" 2>&1 || {
  echo "$STAMP FAIL 行动导入失败，本次不推进（明细见 $IMPORT_LOG 末行）"
  tail -c 1200 "$IMPORT_LOG" | sed 's/^/    /'
  watch "FAIL 行动导入失败"
  rm -rf "$TMP_ACTIONS"; exit 1
}
tail -c 400 "$IMPORT_LOG" | sed 's/^/    行动导入: /'
# 同键异内容 ⇒ `ACTION_REVISED`，非 0 退出。**必须停**：行动被修订属于输入变更，
# 要人来决定（与 frozen_code 守卫同一立场 —— 改了尺子就不是同一件事了）。
"$PY" -m scripts.data.merge_corporate_actions --base "$ACTIONS" \
  --new "$TMP_ACTIONS/corporate_actions.csv" --out "$ACTIONS" || {
  echo "$STAMP FAIL 行动表合并被拒（修订或冲突）—— 本次不推进，见上方 ACTION_REVISED"
  watch "FAIL 行动表合并被拒"
  rm -rf "$TMP_ACTIONS"; exit 1
}
rm -rf "$TMP_ACTIONS"

# ---- 刷新行情（只追加，不改历史）----
# **必须先刷**：`run-day --session auto` 取的是"面板覆盖到的最后一个交易日"，不刷就只会
# 反复准备同一个陈旧 session（与 shadow_daily 同一形态）。
echo "$STAMP refresh --universe forward-arms"
REFRESH_OUT="$("$PY" -m scripts.portfolio_shadow.refresh_data --live-dir "$LIVE_ETF" \
  --universe forward-arms)" || {
  echo "$STAMP FAIL refresh 失败，本次不推进 —— 宁可停一天，也不在陈旧行情上记账"
  watch "FAIL refresh 失败，本次不推进"
  exit 1
}
printf '%s\n' "$REFRESH_OUT"
# 刷新被**别的作业**持锁跳过（影子日作业也在刷同一批面板）⇒ 不能当失败，也不能继续：
# 对方可能正写到一半。留一行，等下一次尝试。
if printf '%s' "$REFRESH_OUT" | grep -q 'REFRESH_IN_PROGRESS'; then
  echo "$STAMP SKIP 另一个作业正在刷新行情 —— 本次不推进，等下一次尝试"
  watch "SKIP 刷新被别的作业持锁（等下一次尝试）"
  exit 0
fi

# ---- 推进三臂 ----
echo "$STAMP run-day --session auto"
OUT="$BASE/runs/$(date +%F).json"
mkdir -p "$BASE/runs"
"$PY" -m scripts.strategy_diagnostics.forward_arms run-day --root "$BASE" \
  --session auto --etf-raw "$LIVE_ETF/etf_raw_daily.csv.gz" | tee "$OUT"
# `set -uo pipefail` 没有 `-e`，脚本不会因非 0 中断 —— 但必须留一行，否则
# 「作业正常结束」与「被拒了」看起来一模一样。
RC=${PIPESTATUS[0]}
[ "$RC" -ne 0 ] && {
  echo "$STAMP 注意：run-day 退出码 $RC —— 见上方的 OBSERVATION_CODE_CHANGED / TARGET_BEFORE_START 等"
  watch "FAIL run-day 退出码 ${RC}"
  exit "$RC"
}

# ---- 每次运行写一行状态：三臂当日净值与成交笔数 ----
LINE="$("$PY" - "$OUT" <<'PYEOF' 2>/dev/null || echo '状态判定失败'
import json, sys
d = json.load(open(sys.argv[1]))
arms = {k: d[k] for k in ('A', 'B', 'C') if k in d}
print(f"session={d.get('_session')} 重放={d.get('_sessions_replayed')} " + ' '.join(
    f"{k}:eq={v.get('equity', 0) / 1e6:,.0f}/pos={v.get('positions')}"
    f"/fill={v.get('filled')}/missed={v.get('missed')}"
    for k, v in arms.items()))
PYEOF
)"
echo "$STAMP 状态：$LINE"
watch "$LINE"

# ---- 只在"值得你知道"的状态上弹一次通知 ----
# 前向观察的常态是"净值小动、常常没有新成交" —— 把常态也弹出来等于没弹。
# 真正要看的是**运行出了故障**（三臂之一被拒、有 missed 执行）。
if printf '%s' "$LINE" | grep -q 'missed=[1-9]'; then
  "$PY" "$ROOT/ops/watchdog.py" --notify "三臂前向观察：有未执行的机会" "$LINE" || true
fi
