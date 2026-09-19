import json
import unittest

from mutifactor.llm.contracts.selection_v4 import build_selection_prompt


class SelectionPromptTests(unittest.TestCase):
    def test_prompt_schema_enumerates_only_frozen_evidence_ids(self):
        packet = {'stocks': [
            {'code': 'US.A', 'evidence': [{'evidence_id': 'e-a'}]},
            {'code': 'US.B', 'evidence': [{'evidence_id': 'e-b'}]},
        ]}
        schema = json.loads(build_selection_prompt(packet))['output_schema']
        ranked = schema['properties']['ranked']['items']['properties']
        self.assertEqual(
            ranked['thesis']['items']['properties']['evidence_ids']['items']['enum'],
            ['e-a', 'e-b'])
        self.assertEqual(
            ranked['counterevidence']['items']['properties']['evidence_ids']['items']['enum'],
            ['e-a', 'e-b'])
        market = schema['properties']['market_view']['properties']['claims']['items']
        self.assertEqual(market['properties']['evidence_ids']['items']['enum'], ['e-a', 'e-b'])


if __name__ == '__main__':
    unittest.main()


class PerStockScopeTests(unittest.TestCase):
    """提示词的闭集必须**按股票分立**，与校验层的每股票索引一致。

    回归背景：原先只有一个跨股票扁平合并的 enum，而校验按每只股票独立索引（`evidence_id`
    由 `stable_id('evidence', code, source_id)` 按代码命名空间）。两者矛盾 ⇒ 模型从别的股票
    的 ID 里挑，逐条被拒为「引用不存在」；校验 fail-closed，一条即足以让整批决策失败。
    生产实测：US.SNDK 的证据被用在 US.MU 的 counterevidence 上。
    """

    PACKET = {'stocks': [
        {'code': 'US.A', 'evidence': [{'evidence_id': 'e-a'}]},
        {'code': 'US.B', 'evidence': [{'evidence_id': 'e-b'}]},
    ]}

    def item(self, code, evidence_ids):
        return {'code': code, 'standalone_rank': 1, 'portfolio_rank': 1,
                'decision': 'watch', 'confidence': 'low', 'horizon': '1_4w',
                'setup_type': 'breakout',
                'thesis': [{'text': 't', 'claim_type': 'inference',
                            'evidence_ids': list(evidence_ids)}]}

    def ranked_schema(self):
        schema = json.loads(build_selection_prompt(self.PACKET))['output_schema']
        return schema['properties']['ranked']['items']

    def test_same_stock_citation_is_accepted(self):
        import jsonschema
        jsonschema.validate(self.item('US.A', ['e-a']), self.ranked_schema())

    def test_cross_stock_citation_is_rejected_by_the_prompt_schema(self):
        import jsonschema
        # 这条以前是"合法"的（扁平 enum 收录了 e-b），正是它让决策在下一层被拒
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(self.item('US.A', ['e-b']), self.ranked_schema())
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(self.item('US.B', ['e-a']), self.ranked_schema())

    def test_unknown_id_still_rejected(self):
        import jsonschema
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(self.item('US.A', ['e-never']), self.ranked_schema())
