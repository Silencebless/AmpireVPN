import unittest

from app.permissions import ALL_SCOPES, BASE_SCOPES, OWNER_ONLY, ROLE_ORDER, ROLES, allowed, scopes_for


class Roles(unittest.TestCase):
    def test_owner_can_everything(self):
        for scope in ALL_SCOPES:
            self.assertTrue(allowed(scope, None, owner=True), scope)

    def test_owner_only_actions_are_never_granted_to_roles(self):
        for role in ROLE_ORDER:
            self.assertFalse(OWNER_ONLY & scopes_for(role), role)
            for scope in OWNER_ONLY:
                self.assertFalse(allowed(scope, role), (role, scope))

    def test_roles_are_actually_separate(self):
        # главная претензия аудита: «любой админ может выдавать дни, создавать промо, менять цены, слать рассылки, отключать узлы»
        self.assertFalse(allowed("subs.grant", "support"))
        self.assertFalse(allowed("promos.manage", "support"))
        self.assertFalse(allowed("plans.edit", "support"))
        self.assertFalse(allowed("broadcast.send", "support"))
        self.assertFalse(allowed("infra.manage", "support"))
        self.assertFalse(allowed("plans.edit", "marketing"))
        self.assertFalse(allowed("subs.grant", "marketing"))
        self.assertFalse(allowed("infra.manage", "billing"))
        self.assertFalse(allowed("broadcast.send", "billing"))
        self.assertFalse(allowed("plans.edit", "ops"))
        self.assertFalse(allowed("broadcast.send", "ops"))
        self.assertTrue(allowed("subs.grant", "billing"))
        self.assertTrue(allowed("broadcast.send", "marketing"))
        self.assertTrue(allowed("infra.manage", "ops"))

    def test_money_and_prices_are_narrow(self):
        holders = [r for r in ROLE_ORDER if "plans.edit" in ROLES[r]]
        self.assertEqual(holders, ["admin"])                      # цены меняет только «admin» (и владелец)
        self.assertEqual([r for r in ROLE_ORDER if "payments.review" in ROLES[r]], ["billing", "admin"])

    def test_unknown_role_has_nothing(self):
        self.assertEqual(scopes_for("hacker"), frozenset())
        self.assertFalse(allowed("users.view", None))
        self.assertTrue(allowed(None, "support"))                 # scope=None — доступно любому админу

    def test_admin_role_equals_legacy_admin_minus_owner_only(self):
        self.assertEqual(scopes_for("admin"), BASE_SCOPES)


if __name__ == "__main__":
    unittest.main()
