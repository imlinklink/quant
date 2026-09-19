"""测试全局隔离。

**要解决的问题**：`ledger.record()`（人工评估账本 JSONL `data/decision_ledger/signals.jsonl`）
写的是**项目固定路径**，与调用方用哪个 `registry` 无关。于是"建了 `ProposalStore` 却没
patch `_record_ledger`"的测试会往**生产账本**追加 `proposal_created`。

实测（2026-09-19）：该文件 515 行里 **466 行是测试代码 `US.A`（90%）**，跨 11 天累积 ——
每次跑测试都往里加。而周报（`weekly_report.py`）把它当"最近提案样例"读出来，
于是**周报在说谎**：它列的"提案"大多从未发生过。

两个已知肇事文件（`test_mainline_v2_integration.py` / `test_deployment_acceptance.py`）
已按其余 7 个文件的既有做法补上 patch；但**光靠每个文件自觉是防不住的** ——
下一个新建 ProposalStore 的测试照样会污染。这里用 autouse fixture 把账本目录整体指到
临时目录：**未设 `QUANT_LEDGER_DIR` 时生产行为一字不变**（`ledger._shared_dir()` 只在
该变量存在时才改道）。
"""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope='session')
def _isolate_shared_ledger():
    """把项目级 JSONL 评估账本重定向到临时目录（session 级，autouse）。"""
    with tempfile.TemporaryDirectory(prefix='quant-ledger-') as tmp:
        old = os.environ.get('QUANT_LEDGER_DIR')
        os.environ['QUANT_LEDGER_DIR'] = tmp
        try:
            yield tmp
        finally:
            if old is None:
                os.environ.pop('QUANT_LEDGER_DIR', None)
            else:
                os.environ['QUANT_LEDGER_DIR'] = old
