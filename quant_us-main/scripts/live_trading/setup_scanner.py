"""Daily setup shadow 扫描编排；行情读取由调用方注入。"""
from typing import Dict, Optional

from .setup_features import compute_setup_features
from .setup_state_machine import build_setup_candidate, transition
from .setup_store import SetupStore


class SetupScanner:
    def __init__(self, registry, config: Optional[dict] = None):
        self.registry = registry
        self.config = config or {}
        self.strategy_cfg = self.config.get('buy_strategy_v2', self.config)
        self.store = SetupStore(registry)

    def scan_code(self, code, stock_bars, sector_bars, market_bars, as_of,
                  selection_decision_id='') -> dict:
        snapshot = compute_setup_features(stock_bars, sector_bars, market_bars,
                                          as_of, self.strategy_cfg)
        previous = self.store.latest_state(code, before_session=snapshot.get('session', ''))
        previous_state = (previous or {}).get('state', 'FALLING')
        state, reasons = transition(previous_state, snapshot, self.strategy_cfg)
        state_id = self.store.save_state(code, snapshot, previous_state, state, reasons)
        candidate = build_setup_candidate(code, snapshot, state, selection_decision_id,
                                          self.strategy_cfg)
        if candidate:
            self.store.save_candidate(candidate, state_id)
        return {'code': code, 'previous_state': previous_state, 'state': state,
                'reason_codes': reasons, 'state_snapshot_id': state_id,
                'candidate': candidate, 'quality': snapshot.get('quality')}

    def scan(self, stocks: Dict[str, object], sectors: Dict[str, object], market_bars,
             as_of, sector_map: Optional[dict] = None,
             selection_decision_id='') -> list:
        sector_map = sector_map or {}
        return [self.scan_code(code, bars, sectors.get(sector_map.get(code)), market_bars,
                               as_of, selection_decision_id)
                for code, bars in sorted(stocks.items())]
