"""决策可视化的只读路由（需求 `docs/decision-visibility-product-requirements-2026-09-22.md`）。

**这个模块只读 JSON 快照文件，别的什么都不做**，而且这条由三道机制保证：

1. 数据来源只有 `SNAPSHOT_DIR` 下的快照，路径由 `latest/index.json` 的 `file` 字段给出 ——
   **不接受请求里的任意路径**（顺手免掉目录穿越），也**不列举目录猜**；
2. **不 import 任何 `scripts.*`**（有测试钉死）⇒ `ShadowStore` / `PositionRegistry` /
   `ProposalStore` / `ExecutionService` 在 web 进程里**根本调不到**。这不是洁癖：实测
   `PositionRegistry.transaction()` 退出时无条件 `INSERT OR REPLACE INTO books`
   ⇒ `registry.all()` 其实是一次写操作；
3. 全部 GET，没有 POST；筛选/排序只改返回顺序，不改数。

「图表与表格的同一指标必须一致」（需求 §9 末段）靠**单一来源**保证：路由不重算任何指标、
不改任何值，页面拿到的就是快照里的那一份。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

bp = Blueprint('analytics', __name__)

US_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = Path(os.environ.get(
    'US_ANALYTICS_SNAPSHOT_DIR',
    str(US_ROOT / 'data' / 'portfolio_shadow' / 'web_snapshots')))
# 快照「过时」的判据：数据截止（as_of）是数据事实，快照年龄是另一件事，两者必须分开显示
# （需求场景 6：不因网页刚刷新就显示「实时」）。
STALE_AFTER_SECONDS = int(os.environ.get('US_ANALYTICS_STALE_SECONDS', '5400'))


def _index():
    path = SNAPSHOT_DIR / 'latest' / 'index.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def _read_latest(rel: str):
    """只允许读 `latest/` 下的相对路径，且**路径不得逃出快照目录**。"""
    base = (SNAPSHOT_DIR / 'latest').resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f'PATH_ESCAPES_SNAPSHOT_DIR:{rel}')
    if not target.exists():
        raise FileNotFoundError(f'SNAPSHOT_NOT_FOUND:{rel}')
    return json.loads(target.read_text(encoding='utf-8'))


def _server_block():
    idx = _index() or {}
    age = None
    gen = idx.get('generated_at')
    if gen:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds()
        except ValueError:
            age = None
    return {'snapshot_generation': idx.get('generation_id'),
            'snapshot_age_seconds': age,
            'stale': (age is not None and age > STALE_AFTER_SECONDS),
            'stale_threshold_seconds': STALE_AFTER_SECONDS,
            'snapshot_dir': str(SNAPSHOT_DIR)}


def _scope_entry(scope_id):
    idx = _index()
    if not idx:
        raise FileNotFoundError('NO_SNAPSHOT_GENERATION')
    for s in idx.get('scopes', []):
        if s['scope_id'] == scope_id:
            if not s.get('file'):
                raise FileNotFoundError(
                    f"SOURCE_READ_FAILED:{s.get('error') or '该范围本次导出失败'}")
            return s
    raise KeyError(f'UNKNOWN_SCOPE:{scope_id}')


# ---- 页面 ---------------------------------------------------------------------
@bp.route('/overview')
def page_overview():
    return render_template('overview.html')


@bp.route('/positions')
def page_positions():
    return render_template('positions.html')


@bp.route('/opportunities')
def page_opportunities():
    return render_template('opportunities.html')


@bp.route('/llm-impact')
def page_llm_impact():
    return render_template('llm_impact.html')


@bp.route('/experiments')
def page_experiments():
    return render_template('experiments.html')


@bp.route('/decisions/<path:decision_id>')
def page_decision(decision_id):
    # id 只在快照里存在；不存在就报错，绝不回退到「按代码找相似的一笔」
    return render_template('decision_detail.html')


# ---- JSON --------------------------------------------------------------------
@bp.route('/api/analytics/scopes')
def api_scopes():
    idx = _index()
    if not idx:
        return jsonify({'ok': True, 'generated_at': None, 'scopes': [],
                        'note': '还没有快照；先跑 ops/build_web_snapshots.py'})
    return jsonify({'ok': True, 'generated_at': idx.get('generated_at'),
                    'generation_id': idx.get('generation_id'),
                    'scopes': idx.get('scopes', []),
                    'unregistered_run_dirs': idx.get('unregistered_run_dirs', [])})


@bp.route('/api/analytics/experiments')
def api_experiments():
    try:
        data = _read_latest('experiments.json')
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    return jsonify({'ok': True, 'server': _server_block(), **data})


@bp.route('/api/analytics/decisions/<path:decision_id>')
def api_decision(decision_id):
    try:
        trace = _read_latest(f'decisions/{decision_id.replace("/", "_")}.json')
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    return jsonify({'ok': True, 'server': _server_block(), 'trace': trace})


@bp.route('/api/analytics/artifact/<path:artifact_key>')
def api_artifact(artifact_key):
    """研究产物的**全量**原文（逐笔数组等）。首屏用的是压缩视图，全量按需取。"""
    key = artifact_key.replace(':', '__').replace('/', '_')
    base = (SNAPSHOT_DIR / 'latest').resolve()
    target = (base / 'artifacts' / f'{key}.json').resolve()
    if base not in target.parents or not target.exists():
        return jsonify({'ok': False, 'error': f'ARTIFACT_NOT_FOUND:{artifact_key}'}), 404
    return jsonify({'ok': True, 'artifact': json.loads(target.read_text(encoding='utf-8'))})


@bp.route('/api/analytics/report/<path:report_key>')
def api_report(report_key):
    """研究报告正文（`report.md`）。单独一条路由，免得把 2MB 塞进 experiments.json。"""
    key = report_key.replace(':', '__').replace('/', '_')
    base = (SNAPSHOT_DIR / 'latest').resolve()
    target = (base / 'reports' / f'{key}.md').resolve()
    if base not in target.parents or not target.exists():
        return jsonify({'ok': False, 'error': f'REPORT_NOT_FOUND:{report_key}'}), 404
    return jsonify({'ok': True, 'report': target.read_text(encoding='utf-8')})


@bp.route('/api/analytics/<scope_id>')
def api_scope(scope_id):
    try:
        entry = _scope_entry(scope_id)
        data = _read_latest(entry['file'])
    except KeyError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    except FileNotFoundError as exc:
        # 来源坏了：如实报错 + 精确路径，**不返回伪造的空账户**（需求场景 10）
        return jsonify({'ok': False, 'error': str(exc),
                        'hint': '其他范围仍可切换查看'}), 503
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    envelope = {k: v for k, v in data.items() if k != 'sections'}
    return jsonify({'ok': True, 'server': _server_block(),
                    'envelope': envelope, 'sections': data.get('sections', {})})
