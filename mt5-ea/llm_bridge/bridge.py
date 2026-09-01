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
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

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

state_lock = threading.Lock()
state = {
    "snapshot_id": None,
    "snapshot": None,
    "candles": [],
    "decision": None,
    "decision_status": "none",  # none/thinking/pending/confirmed/rejected/error/done
    "error": None,
    "executed": None,
    "updated_at": None,
}
server_instance = None
snapshot_path = decision_path = executed_path = ""


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
    return data, candles


def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_user_message(snapshot, candles):
    lines = ["以下是 MT5 终端刚刚生成的市场快照："]
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
        parts = [f"o{h[0]} h{h[1]} l{h[2]} c{h[3]}" for h in candles[-5:]]
        lines.append("- 最近K线(OHLC): " + " | ".join(parts))
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


def build_system_prompt(cfg):
    prompt = SYSTEM_PROMPT
    skills = load_skills(cfg)
    if skills:
        parts = [prompt, "\n\n## 你掌握的技能（每次决策前必须遵守）\n"]
        for name, content in skills:
            parts.append(f"### {name}\n{content}\n")
        prompt = "\n".join(parts)
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


def ask_deepseek(snapshot, candles, cfg):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": build_system_prompt(cfg)},
            {"role": "user", "content": build_user_message(snapshot, candles)},
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


def on_new_snapshot(data, candles):
    with state_lock:
        state.update(
            snapshot_id=data.get("ID"),
            snapshot=data,
            candles=candles,
            decision=None,
            decision_status="thinking",
            error=None,
            executed=None,
            updated_at=time.time(),
        )
    print(f"\n[新快照] {data.get('SYMBOL')} ID={data.get('ID')}，正在询问 DeepSeek…")
    threading.Thread(target=ask_and_publish, daemon=True).start()


def ask_and_publish():
    with state_lock:
        sid = state["snapshot_id"]
        snap = dict(state["snapshot"] or {})
        candles = list(state["candles"])
    try:
        decision = ask_deepseek(snap, candles, CONFIG)
        with state_lock:
            state["decision"] = decision
            state["decision_status"] = "pending"
            state["error"] = None
            state["updated_at"] = time.time()
        write_decision(sid, "pending", decision)
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


def confirm_decision():
    with state_lock:
        if state["decision_status"] != "pending" or state["snapshot_id"] is None:
            return False, "当前没有待确认的决策"
        sid = state["snapshot_id"]
        d = state["decision"]
        state["decision_status"] = "confirmed"
        state["updated_at"] = time.time()
    write_decision(sid, "confirmed", d)
    print(f"[已确认] 将按建议执行: {d['action']} SL={d['stop_loss']} TP={d['take_profit']}")
    return True, "已确认，等待 EA 执行"


def reject_decision():
    with state_lock:
        if state["decision_status"] != "pending":
            return False, "当前没有待拒绝的决策"
        sid = state["snapshot_id"]
        state["decision_status"] = "rejected"
        state["updated_at"] = time.time()
    write_decision(sid, "rejected")
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
            sid = data.get("ID")
            if sid and sid != last_id:
                last_id = sid
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
    global snapshot_path, decision_path, executed_path, server_instance
    cfg = load_config()

    files_dir = cfg["files_dir"]
    llm_dir = os.path.join(files_dir, cfg["llm_folder"])
    snapshot_path = os.path.join(llm_dir, "snapshot.txt")
    decision_path = os.path.join(llm_dir, "decision.txt")
    executed_path = os.path.join(llm_dir, "executed.txt")

    if not os.path.isdir(files_dir):
        print(f"[错误] 找不到 MT5 Files 目录: {files_dir}")
        print("请把 MT5_FILES_DIR 环境变量或 bridge_config.json 的 files_dir 改为你机器上的实际路径。")
        sys.exit(1)
    os.makedirs(llm_dir, exist_ok=True)

    if not cfg["api_key"]:
        print("[警告] 未配置 DEEPSEEK_API_KEY。可 export DEEPSEEK_API_KEY=sk-xxx 或写入 bridge_config.json。")

    skills = load_skills(cfg)
    if skills:
        print(f"  已加载技能({len(skills)}个): " + ", ".join(name for name, _ in skills))
    else:
        print("  技能: 无（可在 skills/ 目录添加 .md 文件）")

    url = f"http://127.0.0.1:{cfg['port']}"
    print("=" * 60)
    print("MT5 × DeepSeek 人工确认桥接已启动")
    print(f"  快照目录: {llm_dir}")
    print(f"  确认网页: {url}")
    print("  指令: y=确认 n=拒绝 r=重问 q=退出")
    print("=" * 60)

    threading.Thread(target=watcher_loop, daemon=True).start()

    server_instance = http.server.ThreadingHTTPServer(("127.0.0.1", cfg["port"]), BridgeHandler)
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
