"""证据引用校验器：§7.1 的归属白名单与 §7.2 的引用要求。

**这条边界必须钉死。** §7.1 允许的归属是「当前证券 + MARKET + packet 声明的
sector/risk_group」，并且明写「**其他证券或未声明板块一律拒绝**」。

当前证据供给里没有任何来源把证据标注成板块级（市场日报标的是 `MARKET`，财报日历与
期权视角都按证券标），所以「板块归属」那条允许在实践中是**惰性**的。惰性不等于可以删，
更不等于可以改成「允许引用同板块**同行**的证据」—— §7.1 恰恰把「其他证券」列为拒绝项，
而这类以"顺手放宽"为名的改动正是本仓库反复出问题的形态（2026-09-19 的一次改动几乎如此）。
本文件的存在就是为了让下一次这类改动必须先撞到测试。
"""
import unittest

from mutifactor.llm.validators.evidence import (require_counterevidence_or_missing,
                                               validate_claims)

AS_OF = '2026-09-19T00:00:00+00:00'


def ev(evidence_id, subject_code, **over):
    item = {'evidence_id': evidence_id, 'subject_code': subject_code, 'summary': 's',
            'quality': 'ok'}
    item.update(over)
    return item


def claim(evidence_ids, text='t', claim_type='inference'):
    return {'text': text, 'claim_type': claim_type, 'evidence_ids': list(evidence_ids)}


class AttributionWhitelistTests(unittest.TestCase):
    """§7.1：当前证券 / MARKET / 声明的板块，三者放行；其他证券拒绝。"""

    GROUPS = {'semis'}

    def check(self, index, ids):
        return validate_claims([claim(ids)], index, 'US.MU', 'selection', AS_OF,
                               self.GROUPS)

    def test_own_security_is_allowed(self):
        index = {'e1': ev('e1', 'US.MU')}
        self.assertEqual(self.check(index, ['e1']), [])

    def test_market_is_allowed(self):
        index = {'e1': ev('e1', 'MARKET')}
        self.assertEqual(self.check(index, ['e1']), [])

    def test_declared_group_label_is_allowed(self):
        """板块标签归属是放行的 —— 当前没有来源这么标，但契约上它必须成立。"""
        index = {'e1': ev('e1', 'semis')}
        self.assertEqual(self.check(index, ['e1']), [])

    def test_peer_security_is_rejected_even_in_the_same_group(self):
        """同板块**同行**的证券归属证据仍然拒绝：§7.1 明写「其他证券一律拒绝」。

        这条是防止把「板块归属」误读成「同板块的同行」。放宽它需要另立设计并说明影响。
        """
        index = {'e1': ev('e1', 'US.SNDK')}          # 与 US.MU 同属 semis
        errors = self.check(index, ['e1'])
        self.assertTrue(any('跨股票引用' in e for e in errors), errors)

    def test_undeclared_group_label_is_rejected(self):
        index = {'e1': ev('e1', 'china')}            # 不在本 packet 声明的板块里
        errors = self.check(index, ['e1'])
        self.assertTrue(any('跨股票引用' in e for e in errors), errors)

    def test_missing_attribution_is_rejected(self):
        index = {'e1': ev('e1', None)}
        errors = self.check(index, ['e1'])
        self.assertTrue(any('缺少归属' in e for e in errors), errors)

    def test_unknown_id_is_rejected(self):
        errors = self.check({}, ['e-missing'])
        self.assertTrue(any('引用不存在' in e for e in errors), errors)


class CitationQualityTests(unittest.TestCase):
    """§7.2：质量、有效期与时间泄漏仍然逐条把关。"""

    def check(self, item, ids=('e1',)):
        return validate_claims([claim(ids)], {'e1': item}, 'US.MU', 'position', AS_OF,
                               None)

    def test_invalid_or_stale_quality_is_rejected(self):
        for quality in ('invalid', 'stale'):
            with self.subTest(quality=quality):
                errors = self.check(ev('e1', 'US.MU', quality=quality))
                self.assertTrue(any('引用' in e and quality in e for e in errors), errors)

    def test_expired_evidence_is_rejected(self):
        errors = self.check(ev('e1', 'US.MU', expires_at='2026-09-18T00:00:00+00:00'))
        self.assertTrue(any('已过期' in e for e in errors), errors)

    def test_future_evidence_is_rejected(self):
        for field in ('effective_at', 'published_at', 'observed_at'):
            with self.subTest(field=field):
                errors = self.check(ev('e1', 'US.MU', **{field: '2026-09-20T00:00:00+00:00'}))
                self.assertTrue(any('未来证据' in e for e in errors), errors)

    def test_fact_text_must_quote_the_summary_verbatim(self):
        errors = self.check(ev('e1', 'US.MU', summary='原文'), ids=('e1',))
        self.assertEqual(errors, [])   # claim_type=inference 不要求逐字
        strict = validate_claims([claim(['e1'], text='改写过的', claim_type='fact')],
                                 {'e1': ev('e1', 'US.MU', summary='原文')}, 'US.MU',
                                 'position', AS_OF, None)
        self.assertTrue(any('未逐字匹配' in e for e in strict), strict)


class CounterevidenceRuleTests(unittest.TestCase):
    def test_requires_counterevidence_or_missing_information(self):
        self.assertTrue(require_counterevidence_or_missing([], []))
        self.assertEqual(require_counterevidence_or_missing([], ['缺口']), [])
        self.assertEqual(
            require_counterevidence_or_missing(
                [{'claim_type': 'counterevidence', 'text': 'x', 'evidence_ids': ['e']}], []),
            [])


if __name__ == '__main__':
    unittest.main()
