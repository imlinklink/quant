#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MT5 <-> DeepSeek 人工确认桥接服务
=================================
让大模型(DeepSeek)参与 MT5 EA 的自动交易决策，但保留"人工确认"环节。

工作方式(文件桥接，无需安装任何第三方库):
  1. MT5 EA 每根新K线把行情快照写入 MQL5/Files/llm_bridge/snapshot.txt
  2. 本服务读取快照 -> 调用 DeepSeek API -> 把建议写入 decision.txt(pending)
  3. 你在终端按 y/n 或打开网页 http://127.0.0.1:8787 确认/拒绝
  4. 确认后 EA 读到 confirmed 决策 -> 自动下单(带止盈止损) -> 写入 executed.txt

用法:
  export DEEPSEEK_API_KEY=sk-xxxx
  python3 bridge.py
"""

import http.server
import json
import csv
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "bridge_config.json")

DEFAULT_FILES_DIR = os.path.join(
    os.path.expanduser("~"),
    "Library/Application Support/net.metaquotes.wine.metatrader5",
    "drive_c/Program Files/MetaTrader 5/MQL5/Files",
)

CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "port": 8787,
    "files_dir": DEFAULT_FILES_DIR,
    "llm_folder": "llm_bridge",
    "skills_dir": "",
    "strategy": "",
    "default_strategy": "",
    "auto_open_browser": True,
    "request_timeout": 90,
}

SYSTEM_PROMPT = """\
你是资深量化交易助手，正在为 MT5 自动交易系统做"人工确认前"的决策建议。
你只能基于用户提供的市场数据做判断，禁止编造任何数据。

输出要求(硬性):
1. 只输出一个 JSON 对象，不要输出任何其他文字、解释或 Markdown。
2. JSON 字段格式:
   {"action":"buy|sell|hold|close","lot_size":0.01,"stop_loss":1.2345,"take_profit":1.2400,"confidence":0.7,"reason":"一句话理由"}
3. action=buy 时，stop_loss 必须低于当前价、take_profit 高于当前价；action=sell 时相反。
4. 数据不足、不确定或风险过高时输出 hold，不要勉强下单。
5. reason 使用简体中文，不超过 80 字。
6. close 表示建议平掉当前持仓；此时 stop_loss/take_profit 填 0。
"""

# ================= 策略池 v1 =================
DEFAULT_STRATEGY_ID = "ema_trend_pullback"
STRATEGIES = {
    "ema_trend_pullback": {"name": "EMA趋势回调", "label": "03-EMA趋势回调", "skill_file": "03-黄金EMA趋势回调策略.md"},
    "london_breakout":    {"name": "伦敦突破",   "label": "10-伦敦突破",    "skill_file": "10-伦敦突破.md"},
}
STRATEGY_SKILL_FILES = {v["skill_file"] for v in STRATEGIES.values()}


def _last_sunday(year, month):
    import calendar
    day = calendar.monthrange(year, month)[1]
    while datetime(year, month, day).weekday() != 6:
        day -= 1
    return datetime(year, month, day)


def _europe_dst_on(dt_utc):
    """欧洲夏令时: 3月最后一个周日01:00 UTC ~ 10月最后一个周日01:00 UTC"""
    start = _last_sunday(dt_utc.year, 3).replace(hour=1, tzinfo=timezone.utc)
    end = _last_sunday(dt_utc.year, 10).replace(hour=1, tzinfo=timezone.utc)
    return start <= dt_utc < end


def choose_strategy(snapshot):
    """确定性路由: 周一~周五伦敦开盘窗口内且亚盘区间数据齐全 -> 伦敦突破, 否则 EMA趋势回调"""
    default_id = CONFIG.get("default_strategy") or DEFAULT_STRATEGY_ID
    if default_id not in STRATEGIES:
        default_id = DEFAULT_STRATEGY_ID
    utc = safe_float(snapshot.get("UTC_EPOCH"))
    if utc <= 0:
        return default_id
    try:
        dt_utc = datetime.fromtimestamp(utc, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return default_id
    bj = dt_utc + timedelta(hours=8)
    if bj.weekday() >= 5:  # 周末黄金休市
        return default_id
    asia_h = safe_float(snapshot.get("ASIA_HIGH"))
    asia_l = safe_float(snapshot.get("ASIA_LOW"))
    if not (asia_h > 0 and asia_l > 0 and asia_h > asia_l):
        return default_id
    start_hour = 15 if _europe_dst_on(dt_utc) else 16  # 伦敦08:00 = 北京15:00(夏) / 16:00(冬)
    if start_hour <= bj.hour < start_hour + 1:
        return "london_breakout"
    return default_id


state_lock = threading.Lock()
state = {
    "snapshot_id": None,
    "snapshot": None,
    "candles": [],
    "decision": None,
    "decision_status": "none",  # none/thinking/pending/confirmed/rejected/error/done
    "error": None,
    "executed": None,
    "strategy": DEFAULT_STRATEGY_ID,
    "updated_at": None,
}
server_instance = None
snapshot_path = decision_path = executed_path = ""
ledger_path = ""
ledger_lock = threading.Lock()
ledger_seen = set()


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            CONFIG.update({k: v for k, v in user.items() if v not in (None, "")})
        except Exception as e:
            print(f"[警告] 读取配置文件失败({e})，使用默认配置")
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        CONFIG["api_key"] = env_key.strip()
    env_dir = os.environ.get("MT5_FILES_DIR")
    if env_dir:
        CONFIG["files_dir"] = env_dir
    return CONFIG


def parse_snapshot(text):
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    candles = []
    raw = data.get("CANDLES", "")
    if raw and "|" in raw:
        _, _, body = raw.partition("|")
        for chunk in body.split(","):
            fields = chunk.split(";")
            if len(fields) == 4:
                try:
                    candles.append(tuple(float(x) for x in fields))
                except ValueError:
                    pass
    candles.reverse()  # EA按新->旧写入；反转后为旧->新(时间正序)
    return data, candles


def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_user_message(snapshot, candles, strategy_id=None):
    lines = ["以下是 MT5 终端刚刚生成的市场快照："]
    info = STRATEGIES.get(strategy_id)
    if info:
        lines.append(f"- 路由激活策略: {info['label']}（只有该策略的入场规则可用）")
    utc = safe_float(snapshot.get("UTC_EPOCH"))
    if utc > 0:
        try:
            bj = datetime.fromtimestamp(utc, timezone.utc) + timedelta(hours=8)
            lines.append(f"- 北京时间(路由判断用): {bj:%Y-%m-%d %H:%M} 周{bj.isoweekday()}")
        except (OverflowError, OSError, ValueError):
            pass
    for k in ("ASIA_HIGH", "ASIA_LOW", "D1_HIGH", "D1_LOW"):
        if snapshot.get(k):
            lines.append(f"- {k}: {snapshot[k]}")
    keys = (
        "SYMBOL", "TIMEFRAME", "BID", "ASK", "SPREAD",
        "FASTMA", "SLOWMA", "LOCAL_SIGNAL",
        "POSITION", "POSITION_VOLUME", "POSITION_PROFIT",
        "BALANCE", "EQUITY",
    )
    for k in keys:
        if snapshot.get(k):
            lines.append(f"- {k}: {snapshot[k]}")
    if candles:
        recent = candles[-6:]  # 时间正序下取最近6根已收盘K线
        parts = []
        for j, h in enumerate(recent):
            k = len(recent) - j  # K-1 为最新一根已收盘
            parts.append(f"K-{k}: o{h[0]} h{h[1]} l{h[2]} c{h[3]}")
        lines.append("- 最近K线(旧->新, K-1=最新已收盘): " + " | ".join(parts))
    lines.append("请给出你的交易决策(JSON)。")
    return "\n".join(lines)


def extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def load_skills(cfg):
    """读取 skills 目录下的所有 .md 技能文件（下划线开头的忽略）"""
    skills_dir = cfg.get("skills_dir") or os.path.join(SCRIPT_DIR, "skills")
    if not os.path.isdir(skills_dir):
        return []
    result = []
    try:
        names = sorted(
            f for f in os.listdir(skills_dir)
            if f.lower().endswith(".md") and not f.startswith("_")
        )
        for name in names:
            path = os.path.join(skills_dir, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read().strip()
                if content:
                    result.append((name, content))
            except OSError as e:
                print(f"[警告] 读取技能文件失败 {name}: {e}")
    except OSError as e:
        print(f"[警告] 无法读取技能目录 {skills_dir}: {e}")
    return result


def load_skill_content(cfg, filename):
    """读取 skills/ 目录下单个技能文件内容"""
    skills_dir = cfg.get("skills_dir") or os.path.join(SCRIPT_DIR, "skills")
    path = os.path.join(skills_dir, filename)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def build_system_prompt(cfg, strategy_id=None):
    prompt = SYSTEM_PROMPT
    general = [(n, c) for n, c in load_skills(cfg) if n not in STRATEGY_SKILL_FILES]
    if general:
        parts = [prompt, "\n\n## 通用技能（每次决策前必须遵守）\n"]
        for name, content in general:
            parts.append(f"### {name}\n{content}\n")
        prompt = "\n".join(parts)
    sid = strategy_id or cfg.get("default_strategy") or DEFAULT_STRATEGY_ID
    if sid not in STRATEGIES:
        sid = DEFAULT_STRATEGY_ID
    info = STRATEGIES[sid]
    text = load_skill_content(cfg, info["skill_file"])
    if text:
        prompt += ("\n\n## 本次激活的交易策略: " + info["label"] + "\n"
                   "只允许执行本策略的入场规则；其他策略的入场规则一律禁用。持仓管理按通用技能执行。\n\n"
                   + text + "\n")
    return prompt


def normalize_decision(parsed, snapshot, cfg):
    action = str(parsed.get("action", "hold")).strip().lower()
    if action not in ("buy", "sell", "hold", "close"):
        raise ValueError(f"未知 action: {action}")
    confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
    reason = str(parsed.get("reason", ""))[:200]
    lot = max(0.0, min(100.0, float(parsed.get("lot_size", 0) or 0)))
    sl = tp = 0.0
    if action in ("buy", "sell"):
        bid = safe_float(snapshot.get("BID"))
        ask = safe_float(snapshot.get("ASK"))
        entry = ask if action == "sell" else bid
        if entry <= 0:
            raise ValueError("快照缺少有效价格，无法校验止损止盈")
        sl = float(parsed.get("stop_loss", 0) or 0)
        tp = float(parsed.get("take_profit", 0) or 0)
        if sl <= 0 or tp <= 0:
            raise ValueError("buy/sell 决策必须同时给出 stop_loss 和 take_profit")
        if action == "buy":
            if not (sl < entry < tp):
                raise ValueError("买入决策校验失败: 要求 止损 < 当前价 < 止盈")
        else:
            if not (tp < entry < sl):
                raise ValueError("卖出决策校验失败: 要求 止盈 < 当前价 < 止损")
        dist = abs(sl - entry) / entry
        if dist < 0.0001:
            raise ValueError("止损距离当前价过近，疑似无效")
        if dist > 0.30:
            raise ValueError("止损距离超过当前价 30%，风险过大")
    return {
        "action": action,
        "lot_size": round(lot, 4),
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": round(confidence, 2),
        "reason": reason,
    }


def ask_deepseek(snapshot, candles, cfg, strategy_id=None):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    sid = strategy_id or choose_strategy(snapshot)
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": build_system_prompt(cfg, sid)},
            {"role": "user", "content": build_user_message(snapshot, candles, sid)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
    )
    with urllib.request.urlopen(req, timeout=float(cfg.get("request_timeout", 90))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return normalize_decision(extract_json(content), snapshot, cfg)


def write_decision(signal_id, status, decision=None):
    if decision is None:
        decision = {"action": "hold", "lot_size": 0, "stop_loss": 0, "take_profit": 0, "confidence": 0}
    line = (
        f"{signal_id}|{status}|{decision['action']}|{decision['lot_size']}|"
        f"{decision['stop_loss']}|{decision['take_profit']}|{decision['confidence']}\n"
    )
    tmp = decision_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(line)
    os.replace(tmp, decision_path)


# ---- 决策台账: 只追加 CSV，记录每个信号的生命周期事件 ----
LEDGER_COLUMNS = ["ts", "signal_id", "status", "strategy", "action", "lot", "sl", "tp",
                  "confidence", "reason", "error", "bid", "ask", "spread",
                  "position", "balance", "equity"]


def append_ledger(signal_id, status, action="", lot="", sl="", tp="", confidence="",
                  reason="", error="", market=None, strategy=None):
    """向 ledger.csv 追加一条事件；同一 (signal_id, status) 只记一次。"""
    if not ledger_path:
        return
    key = (str(signal_id), status)
    with ledger_lock:
        if key in ledger_seen:
            return
        ledger_seen.add(key)
        if market is None:
            with state_lock:
                market = state.get("snapshot") or {}
        field_map = {"BID": "bid", "ASK": "ask", "SPREAD": "spread",
                     "POSITION": "position", "BALANCE": "balance", "EQUITY": "equity"}
        if strategy is None:
            with state_lock:
                st_id = state.get("strategy") or CONFIG.get("default_strategy") or DEFAULT_STRATEGY_ID
            st_info = STRATEGIES.get(st_id) or STRATEGIES[DEFAULT_STRATEGY_ID]
            strategy = st_info["label"]
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "signal_id": str(signal_id), "status": status,
               "strategy": strategy or "",
               "action": str(action), "lot": str(lot), "sl": str(sl), "tp": str(tp),
               "confidence": str(confidence), "reason": reason or "", "error": error or ""}
        for k, col in field_map.items():
            row[col] = str(market.get(k, ""))
        try:
            exists = os.path.exists(ledger_path) and os.path.getsize(ledger_path) > 0
            with open(ledger_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
                if not exists:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            print(f"[警告] 决策台账写入失败: {e}")


def load_ledger_history():
    """启动时读取已有 ledger.csv，把 (signal_id, status) 记入去重集合。"""
    try:
        if ledger_path and os.path.exists(ledger_path):
            with open(ledger_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("signal_id") and row.get("status"):
                        ledger_seen.add((row["signal_id"], row["status"]))
    except OSError:
        pass


def ledger_has_answer(signal_id):
    """该快照是否已经产生过答案(排除纯 error，允许重启后重试失败请求)。"""
    sid = str(signal_id)
    with ledger_lock:
        return any(st in ("pending", "confirmed", "rejected", "executed", "failed")
                   for i, st in ledger_seen if i == sid)


def on_new_snapshot(data, candles):
    active = choose_strategy(data)
    with state_lock:
        state.update(
            snapshot_id=data.get("ID"),
            snapshot=data,
            candles=candles,
            decision=None,
            decision_status="thinking",
            error=None,
            executed=None,
            strategy=active,
            updated_at=time.time(),
        )
    info = STRATEGIES.get(active, STRATEGIES[DEFAULT_STRATEGY_ID])
    print(f"\n[新快照] {data.get('SYMBOL')} ID={data.get('ID')}，正在询问 DeepSeek…")
    print(f"[路由] 激活策略: {info['label']}（{info['name']}）")
    threading.Thread(target=ask_and_publish, daemon=True).start()


def ask_and_publish():
    with state_lock:
        sid = state["snapshot_id"]
        snap = dict(state["snapshot"] or {})
        candles = list(state["candles"])
        active = state.get("strategy") or choose_strategy(snap)
    info = STRATEGIES.get(active, STRATEGIES[DEFAULT_STRATEGY_ID])
    try:
        decision = ask_deepseek(snap, candles, CONFIG, strategy_id=active)
        with state_lock:
            state["decision"] = decision
            state["decision_status"] = "pending"
            state["error"] = None
            state["updated_at"] = time.time()
        write_decision(sid, "pending", decision)
        append_ledger(sid, "pending", action=decision["action"],
                      lot=decision["lot_size"], sl=decision["stop_loss"],
                      tp=decision["take_profit"], confidence=decision["confidence"],
                      reason=decision["reason"], market=snap, strategy=info["label"])
        print(
            f"[待确认] {decision['action']} lot={decision['lot_size']} "
            f"SL={decision['stop_loss']} TP={decision['take_profit']} 置信度={decision['confidence']}"
        )
        print(f"        理由: {decision['reason']}")
        print("        输入 y=确认 n=拒绝 r=重问 (或打开网页点击按钮)")
    except Exception as e:
        with state_lock:
            state["decision"] = None
            state["decision_status"] = "error"
            state["error"] = str(e)
            state["updated_at"] = time.time()
        print(f"[错误] 询问 DeepSeek 失败: {e}")
        append_ledger(sid, "error", error=str(e)[:300], market=snap, strategy=info["label"])


def confirm_decision():
    with state_lock:
        if state["decision_status"] != "pending" or state["snapshot_id"] is None:
            return False, "当前没有待确认的决策"
        sid = state["snapshot_id"]
        d = state["decision"]
        state["decision_status"] = "confirmed"
        state["updated_at"] = time.time()
    write_decision(sid, "confirmed", d)
    append_ledger(sid, "confirmed", action=d["action"], lot=d["lot_size"],
                  sl=d["stop_loss"], tp=d["take_profit"],
                  confidence=d["confidence"], reason=d["reason"])
    print(f"[已确认] 将按建议执行: {d['action']} SL={d['stop_loss']} TP={d['take_profit']}")
    return True, "已确认，等待 EA 执行"


def reject_decision():
    with state_lock:
        if state["decision_status"] != "pending":
            return False, "当前没有待拒绝的决策"
        sid = state["snapshot_id"]
        d = state["decision"] or {}
        state["decision_status"] = "rejected"
        state["updated_at"] = time.time()
    write_decision(sid, "rejected")
    append_ledger(sid, "rejected", action=d.get("action", ""),
                  lot=d.get("lot_size", ""), sl=d.get("stop_loss", ""),
                  tp=d.get("take_profit", ""), confidence=d.get("confidence", ""),
                  reason=d.get("reason", ""))
    print("[已拒绝] 本次建议不执行")
    return True, "已拒绝"


def retry_decision():
    with state_lock:
        data = dict(state["snapshot"] or {})
        if not data:
            return False, "还没有收到快照"
    threading.Thread(target=ask_and_publish, daemon=True).start()
    return True, "已重新询问 DeepSeek"


def watcher_loop():
    last_id = None
    while True:
        try:
            with open(snapshot_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except FileNotFoundError:
            text = None
        except OSError as e:
            print(f"[警告] 读取快照失败: {e}")
            text = None

        if text and text.strip():
            data, candles = parse_snapshot(text)
            # 只处理完整写入的快照(EA 末尾写 COMPLETE=1)，避免读到半个文件
            if data.get("COMPLETE") == "1":
                sid = data.get("ID")
                if sid and sid != last_id:
                    last_id = sid
                    # 重启后跳过已记录过答案的快照，避免重复询问/重复台账
                    if ledger_has_answer(sid):
                        continue
                    on_new_snapshot(data, candles)

        try:
            with open(executed_path, "r", encoding="utf-8", errors="replace") as f:
                ex = f.read().strip()
            if ex:
                ex_id = ex.split("|")[0] if ex else ""
                with state_lock:
                    if ex_id == str(state.get("snapshot_id")):
                        state["executed"] = ex
                        if state["decision_status"] == "confirmed":
                            state["decision_status"] = "done"
                        state["updated_at"] = time.time()
                        parts_ex = ex.split("|")
                        if len(parts_ex) >= 3:
                            append_ledger(parts_ex[0], parts_ex[1].lower(), action=parts_ex[2])
        except FileNotFoundError:
            pass
        except OSError:
            pass

        time.sleep(1)


def get_state():
    with state_lock:
        s = state["snapshot"] or {}
        return {
            "snapshot_id": state["snapshot_id"],
            "decision_status": state["decision_status"],
            "error": state["error"],
            "executed": state["executed"],
            "decision": state["decision"],
            "market": {
                "symbol": s.get("SYMBOL"),
                "timeframe": s.get("TIMEFRAME"),
                "bid": s.get("BID"),
                "ask": s.get("ASK"),
                "spread": s.get("SPREAD"),
                "position": s.get("POSITION"),
                "position_volume": s.get("POSITION_VOLUME"),
                "position_profit": s.get("POSITION_PROFIT"),
                "balance": s.get("BALANCE"),
                "equity": s.get("EQUITY"),
                "local_signal": s.get("LOCAL_SIGNAL"),
            },
        }


def confirm():
    ok, msg = confirm_decision()
    return {"ok": ok, "message": msg}


def reject():
    ok, msg = reject_decision()
    return {"ok": ok, "message": msg}


def retry():
    ok, msg = retry_decision()
    return {"ok": ok, "message": msg}


INDEX_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MT5 × DeepSeek 人工确认</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;max-width:760px;margin:32px auto;padding:0 16px;color:#1f2328;line-height:1.6}
h1{font-size:22px}
.card{background:#f6f8fa;border:1px solid #d0d7de;border-radius:10px;padding:16px;margin:12px 0}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13px;background:#eaeef2;color:#57606a}
.badge.pending{background:#fff8c5;color:#9a6700}
.badge.confirmed{background:#dafbe1;color:#1a7f37}
.badge.rejected{background:#ffebe9;color:#cf222e}
.badge.error{background:#ffebe9;color:#cf222e}
.badge.done{background:#dafbe1;color:#1a7f37}
.badge.thinking{background:#ddf4ff;color:#0969da}
.row{display:flex;gap:8px;margin-top:12px}
button{flex:1;padding:10px;border:0;border-radius:8px;font-size:15px;cursor:pointer}
.ok{background:#1f883d;color:#fff}
.no{background:#cf222e;color:#fff}
.again{background:#0969da;color:#fff}
.muted{color:#57606a;font-size:13px}
pre{white-space:pre-wrap;word-break:break-all}
</style>
</head>
<body>
<h1>MT5 × DeepSeek 人工确认</h1>
<div class="card" id="market">等待快照…</div>
<div class="card" id="decision">暂无建议</div>
<div class="row" id="btns" hidden>
  <button class="ok" onclick="act('confirm')">✅ 确认下单</button>
  <button class="no" onclick="act('reject')">❌ 拒绝</button>
</div>
<div class="row">
  <button class="again" onclick="act('retry')">🔄 重新询问 LLM</button>
</div>
<script>
const $ = id => document.getElementById(id);
function badge(s){ return `<span class="badge ${s}">${s}</span>`; }
async function act(a){ await fetch('/api/'+a,{method:'POST'}); refresh(); }
async function refresh(){
  try {
    const s = await (await fetch('/api/state')).json();
    const m = s.market || {};
    $('market').innerHTML =
      `<div><b>${m.symbol||'-'}</b> ${m.timeframe||''} <span class="muted">ID=${s.snapshot_id||'-'}</span></div>
       <div class="muted">Bid=${m.bid||'-'} Ask=${m.ask||'-'} 点差=${m.spread||'-'}</div>
       <div class="muted">持仓=${m.position||'-'} ${m.position_volume?('手数 '+m.position_volume):''} 浮盈=${m.position_profit||'-'}</div>
       <div class="muted">余额=${m.balance||'-'} 净值=${m.equity||'-'} 本地信号=${m.local_signal||'-'}</div>`;
    const st = s.decision_status || 'none';
    const d = s.decision;
    let html = `状态: ${badge(st)}`;
    if(s.error) html += `<pre class="muted" style="color:#cf222e">错误: ${s.error}</pre>`;
    if(s.executed) html += `<pre class="muted">执行结果: ${s.executed}</pre>`;
    if(d) html +=
      `<div style="margin-top:8px"><b>建议: ${d.action}</b>　置信度 ${Math.round(d.confidence*100)}%</div>
       <div class="muted">手数建议(仅供参考): ${d.lot_size}　止损: ${d.stop_loss||'-'}　止盈: ${d.take_profit||'-'}</div>
       <pre class="muted">理由: ${d.reason}</pre>`;
    $('decision').innerHTML = html;
    $('btns').hidden = !(st === 'pending');
  } catch(e){ $('market').innerHTML = '桥接服务连接失败: '+e; }
}
setInterval(refresh, 1500);
refresh();
</script>
</body>
</html>"""


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, code, text):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._html(200, INDEX_HTML)
        elif self.path == "/api/state":
            self._json(200, get_state())
        else:
            self._json(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        if self.path == "/api/confirm":
            result = confirm()
        elif self.path == "/api/reject":
            result = reject()
        elif self.path == "/api/retry":
            result = retry()
        elif self.path == "/api/quit":
            if server_instance:
                threading.Thread(target=server_instance.shutdown, daemon=True).start()
            result = {"ok": True, "message": "正在退出"}
        else:
            result = {"ok": False, "message": "not found"}
        self._json(200, result)

    def log_message(self, *args):
        pass


def console_loop():
    print("控制台指令: y=确认  n=拒绝  r=重新询问  q=退出")
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("y", "yes", "确认"):
            confirm()
        elif cmd in ("n", "no", "拒绝"):
            reject()
        elif cmd in ("r", "retry", "重问"):
            retry()
        elif cmd in ("q", "quit", "exit", "退出"):
            print("正在退出…")
            if server_instance:
                threading.Thread(target=server_instance.shutdown, daemon=True).start()
            break


def main():
    global snapshot_path, decision_path, executed_path, ledger_path, server_instance
    cfg = load_config()

    files_dir = cfg["files_dir"]
    llm_dir = os.path.join(files_dir, cfg["llm_folder"])
    snapshot_path = os.path.join(llm_dir, "snapshot.txt")
    decision_path = os.path.join(llm_dir, "decision.txt")
    executed_path = os.path.join(llm_dir, "executed.txt")
    ledger_path = os.path.join(llm_dir, "ledger.csv")

    if not os.path.isdir(files_dir):
        print(f"[错误] 找不到 MT5 Files 目录: {files_dir}")
        print("请把 MT5_FILES_DIR 环境变量或 bridge_config.json 的 files_dir 改为你机器上的实际路径。")
        sys.exit(1)
    os.makedirs(llm_dir, exist_ok=True)
    load_ledger_history()

    if not cfg["api_key"]:
        print("[警告] 未配置 DEEPSEEK_API_KEY。可 export DEEPSEEK_API_KEY=sk-xxx 或写入 bridge_config.json。")

    general = [(n, c) for n, c in load_skills(cfg) if n not in STRATEGY_SKILL_FILES]
    if general:
        print(f"  已加载通用技能({len(general)}个): " + ", ".join(name for name, _ in general))
    else:
        print("  技能: 无（可在 skills/ 目录添加 .md 文件）")
    print("  策略池(" + str(len(STRATEGIES)) + "套): " + ", ".join(v["label"] for v in STRATEGIES.values()))

    url = f"http://127.0.0.1:{cfg['port']}"
    print("=" * 60)
    print("MT5 × DeepSeek 人工确认桥接已启动")
    print(f"  快照目录: {llm_dir}")
    print(f"  决策台账: {ledger_path}")
    print(f"  默认策略: {STRATEGIES[DEFAULT_STRATEGY_ID]['label']} | 伦敦突破窗口: 北京15:00-16:00(夏令时)/16:00-17:00(冬令时)，自动切换")
    print(f"  确认网页: {url}")
    print("  指令: y=确认 n=拒绝 r=重问 q=退出")
    print("=" * 60)

    threading.Thread(target=watcher_loop, daemon=True).start()

    try:
        server_instance = http.server.ThreadingHTTPServer(("127.0.0.1", cfg["port"]), BridgeHandler)
    except OSError as e:
        print(f"[错误] 启动网页服务失败: 端口 {cfg['port']} 被占用 ({e})")
        print("可能原因: 已经有一个 bridge.py 实例在运行。")
        print("解决办法: 1) 结束旧实例后重试; 2) 或修改 bridge_config.json 把 port 改成 8788。")
        print("查找并结束旧实例:")
        print(f"  lsof -nP -iTCP:{cfg['port']} -sTCP:LISTEN")
        print("  kill <上面显示的PID>")
        sys.exit(1)
    threading.Thread(target=server_instance.serve_forever, daemon=True).start()

    if cfg.get("auto_open_browser", True):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if sys.stdin.isatty():
        threading.Thread(target=console_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server_instance.shutdown()


if __name__ == "__main__":
    main()
