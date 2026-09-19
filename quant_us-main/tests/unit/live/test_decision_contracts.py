import unittest

from scripts.live_trading.decision_contracts import (ROLE_CONTRACTS, role_contract,
                                                    validate_role_action)


class DecisionRoleContractTests(unittest.TestCase):
    def test_roles_define_subject_fallback_and_actions(self):
        self.assertEqual(role_contract('entry').subject_type, 'signal')
        self.assertEqual(role_contract('entry').fallback_action, 'rule_baseline')
        validate_role_action('position', 'reduce')

    def test_unknown_or_cross_role_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '非法 role'):
            role_contract('portfolio_v2')      # 拼错/不存在的角色必须报错，而不是静默放行
        with self.assertRaisesRegex(ValueError, '不允许动作'):
            validate_role_action('entry', 'exit')
        # 五个设计角色都已注册（§6）
        for role in ('selection', 'entry', 'position', 'portfolio', 'review'):
            self.assertIn(role, ROLE_CONTRACTS)

    def test_review_may_never_affect_a_path(self):
        """Review 的定义性约束：它只能提候选，永不改变任何执行路径。"""
        self.assertFalse(role_contract('review').may_affect_shadow_path)
        self.assertEqual(role_contract('review').fallback_action, 'no_change')
        # 它的动作里没有任何一个能改动配置或权限
        self.assertEqual(role_contract('review').allowed_actions,
                         frozenset({'propose_change', 'no_change'}))

    def test_portfolio_fallbacks_to_the_rule_allocation(self):
        contract = role_contract('portfolio')
        self.assertEqual(contract.fallback_action, 'keep_rule_allocation')
        self.assertIn('keep_rule_allocation', contract.allowed_actions)


if __name__ == '__main__':
    unittest.main()
