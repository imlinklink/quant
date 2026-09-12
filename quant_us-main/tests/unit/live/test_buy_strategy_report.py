import unittest
import pandas as pd
from scripts.buy_strategy_report import (COSTS, EXIT_IDS, holm_adjust, increment_rows, metrics,
                                         render_report)


class BuyStrategyReportTests(unittest.TestCase):
    def test_increment_joint_bootstrap_keeps_shared_cluster_values(self):
        rows=[]
        for i in range(8):
            for group in ('A','B'):
                rows.append({'experiment':group,'independence_group':f'cluster-{i}',
                             'entry_time':'2026-01-05T14:30:00Z','exit_method':'E1',
                             'cost_scenario':.002,'net_pnl_pct':i/100})
        result=increment_rows(pd.DataFrame(rows),'B','A',iterations=300)[0]
        self.assertAlmostEqual(result['mean_diff'],0)
        self.assertAlmostEqual(result['ci_low'],0)
        self.assertAlmostEqual(result['ci_high'],0)
        self.assertEqual(result['p_value'],1)
    def test_increment_is_aggregate_and_holm(self):
        rows=[]
        for i in range(4):
            for exp,pnl in [('C',.01),('D',.02)]:
                rows.append({'experiment':exp,'setup_id':f's{i}','stock':f'US.{i}',
                  'strategy':'reversal_confirmed','entry_time':f'2026-01-0{i+1}T14:30:00Z',
                  'exit_method':'E7','cost_scenario':.002,'net_pnl_pct':pnl,
                  'net_pnl_usd':pnl*5000,'mfe_pct':.03,'mae_pct':-.01,
                  'portfolio_accepted':True,'data_quality':'good'})
        out=metrics(pd.DataFrame(rows))
        row=out['d_minus_c'][0]
        self.assertEqual(row['parent_trades'],4)
        self.assertEqual(row['child_trades'],4)
        self.assertAlmostEqual(row['parent_mean'],.01)
        self.assertAlmostEqual(row['child_mean'],.02)
        self.assertAlmostEqual(row['mean_diff'],.01)
        self.assertEqual(row['years_total'],1)
        self.assertEqual(row['years_positive'],1)
        adjusted=holm_adjust([.04,.01,.03]);self.assertTrue(all(0<=x<=1 for x in adjusted))

    def test_abc_groups_mark_llm_inconclusive_no_empty_table(self):
        rows=[]
        for i in range(4):
            for exp,pnl in [('A',.0),('B',.01),('C',.02)]:
                rows.append({'experiment':exp,'setup_id':f's{i}','stock':f'US.{i}',
                  'strategy':'reversal_confirmed','entry_time':f'2026-01-0{i+1}T14:30:00Z',
                  'exit_method':'E7','cost_scenario':.002,'net_pnl_pct':pnl,
                  'net_pnl_usd':pnl*5000,'mfe_pct':.03,'mae_pct':-.01,
                  'portfolio_accepted':True,'data_quality':'good'})
        out=metrics(pd.DataFrame(rows),groups=('A','B','C'))
        self.assertEqual(out['selected_groups'],['A','B','C'])
        self.assertEqual(out['d_minus_c'],[])
        self.assertEqual(out['llm_increment'],
                         {'status':'inconclusive','reason':'HISTORICAL_LLM_LABELS_UNAVAILABLE'})
        increments={i['child']:i for i in out['increments']}
        self.assertEqual(set(increments),{'B','C'})
        self.assertAlmostEqual(increments['B']['rows'][0]['mean_diff'],.01)
        self.assertAlmostEqual(increments['C']['rows'][0]['mean_diff'],.01)
        text=render_report(out,'BUY-WD-ABC-EXP-001',('A','B','C'))
        self.assertIn('LLM_INCREMENT_STATUS = inconclusive',text)
        self.assertIn('reason = HISTORICAL_LLM_LABELS_UNAVAILABLE',text)
        self.assertIn('## A/B/C × Exit 核心结果',text)
        self.assertNotIn('## D-C 配对增量',text)
        self.assertEqual(len(EXIT_IDS)*len(COSTS)*3,132)


    def test_asset_type_slices_and_concentration(self):
        rows=[]
        for i in range(4):
            for exp,pnl in [('A',.0),('B',.01),('C',.02)]:
                rows.append({'experiment':exp,'setup_id':f's{i}','stock':f'US.{i}',
                  'strategy':'reversal_confirmed','entry_time':f'2026-01-0{i+1}T14:30:00Z',
                  'exit_method':'E7','cost_scenario':.002,'net_pnl_pct':pnl,
                  'net_pnl_usd':pnl*5000,'mfe_pct':.03,'mae_pct':-.01,
                  'portfolio_accepted':True,'data_quality':'good'})
        matrix=pd.DataFrame(rows)
        at={f'US.{i}':('leveraged_etf' if i%2==0 else 'stock') for i in range(4)}
        out=metrics(matrix,groups=('A','B','C'),asset_types=at)
        self.assertEqual({s['asset_type'] for s in out['asset_slices']},{'stock','leveraged_etf'})
        for s in out['asset_slices']:
            self.assertEqual(s['cells'],s['positive'])   # 各组同向为正
        text=render_report(out,'BUY-WD-ABC-EXP-001',('A','B','C'))
        self.assertIn('## 资产类型切片（增量方向）',text)
        self.assertIn('top2股(占毛利)',text)
        for r in out['groups']:
            for k in ('top2_stock_share','top3_trade_share'):
                self.assertTrue(r[k] is None or 0.0<=r[k]<=1.0)
        empty=metrics(matrix,groups=('A','B','C'))
        self.assertEqual(empty['asset_slices'],[])
        self.assertIn('未提供证券主数据',render_report(empty,'X',('A','B','C')))
        at2=dict(at);at2['US.ZZZ']='etf'   # 该资产类型无可交易样本
        out2=metrics(matrix,groups=('A','B','C'),asset_types=at2)
        self.assertIn('etf',out2['asset_types_missing'])
        self.assertIn('未产生可交易样本的资产类型',render_report(out2,'X',('A','B','C')))


if __name__=='__main__':unittest.main()
