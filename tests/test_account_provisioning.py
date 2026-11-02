"""New workspace-creation fields: a per-account Gemini key and a custom
Chrome profile directory name, both introduced so `POST /api/accounts` can
take more than just id/label."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.accounts as accounts_module
from dashboard_api.accounts import AccountRegistry


class AccountRegistryProvisioningTests(unittest.TestCase):
    """`ACCOUNTS_DIR`/`REGISTRY_PATH` are module-level globals (not per
    instance), so every test here patches them to a temp directory - without
    that, `create()` would write into the real `configs/accounts/`, which
    the two live accounts (acct-a, acct-b) actually use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        accounts_dir = Path(self.tmp.name) / "accounts"
        patcher_dir = mock.patch.object(accounts_module, "ACCOUNTS_DIR", accounts_dir)
        patcher_path = mock.patch.object(
            accounts_module, "REGISTRY_PATH", accounts_dir / "accounts.json",
        )
        patcher_dir.start()
        patcher_path.start()
        self.addCleanup(patcher_dir.stop)
        self.addCleanup(patcher_path.stop)

        base_config = Path(self.tmp.name) / "config.yaml"
        base_config.write_text(
            "account: {}\ncontrol: {}\nbrowser: {}\nextension_bridge: {}\n"
            "paths: {}\nlogging: {}\ngrounding: {}\nllm: {}\nproducts: {}\n",
            encoding="utf-8",
        )
        self.registry = AccountRegistry(base_config_path=base_config)

    def test_api_key_never_appears_in_as_dict(self):
        account = self.registry.create("acct-x", "X", api_key="secret-key")
        data = account.as_dict()
        self.assertNotIn("api_key", data)
        self.assertTrue(data["has_api_key"])
        self.assertEqual(account.api_key, "secret-key")

    def test_no_api_key_reports_has_api_key_false(self):
        account = self.registry.create("acct-y", "Y")
        self.assertFalse(account.as_dict()["has_api_key"])

    def test_custom_profile_dir_name_overrides_the_account_id(self):
        account = self.registry.create(
            "acct-z", "Z", profile_dir_name="my-chrome-profile",
        )
        self.assertTrue(str(account.profile_dir).endswith("my-chrome-profile"))

    def test_empty_profile_dir_name_falls_back_to_account_id(self):
        account = self.registry.create("acct-w", "W")
        self.assertTrue(str(account.profile_dir).endswith("acct-w"))

    def test_write_config_sets_llm_api_key_only_when_provided(self):
        with_key = self.registry.create("acct-k1", "K1", api_key="k-123")
        path = self.registry.write_config(with_key)
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(data["llm"]["api_key"], "k-123")

        without_key = self.registry.create("acct-k2", "K2")
        path2 = self.registry.write_config(without_key)
        data2 = yaml.safe_load(path2.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", data2.get("llm", {}))

    def test_write_config_scopes_product_root_per_account(self):
        account = self.registry.create("acct-p", "P")
        path = self.registry.write_config(account)
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["products"]["product_root"], "data/accounts/acct-p/products",
        )

    def test_scenario_examples_round_trip_through_the_account_s_own_config_file(self):
        """Exercises exactly what dashboard_api.main's scenario endpoints do:
        read via `registry.config()`, edit in place, `save()`, then read
        again - independent of whether that account's agent process is
        running, unlike the numeric knobs behind `/control/settings`."""
        account = self.registry.create("acct-s", "S")
        self.assertEqual(
            self.registry.config(account).get("messaging.scenario_examples", []), [],
        )

        examples = [{"customer": "Hi", "agent": "Dạ chào bạn ạ."}]
        cfg = self.registry.config(account)
        cfg.set("messaging.scenario_examples", examples)
        cfg.save()

        reloaded = self.registry.config(account)
        self.assertEqual(reloaded.get("messaging.scenario_examples"), examples)
        # Editing this key must not disturb the rest of that account's config.
        self.assertEqual(reloaded.get("account.id"), "acct-s")

    def test_an_emptied_reply_style_survives_write_config(self):
        """The base config ships style rules; an operator who cleared them
        must not get them back on the next unrelated `write_config`."""
        base = self.registry.base_config_path
        base.write_text(
            base.read_text(encoding="utf-8")
            + "messaging:\n  reply_style: [\"Giọng lịch sự.\"]\n",
            encoding="utf-8",
        )
        account = self.registry.create("acct-r", "R")
        self.assertEqual(
            self.registry.config(account).get("messaging.reply_style"), ["Giọng lịch sự."],
        )
        cfg = self.registry.config(account)
        cfg.set("messaging.reply_style", [])
        cfg.save()
        self.registry.write_config(account)
        self.assertEqual(
            self.registry.config(account).get("messaging.reply_style"), [],
        )

    def test_a_chosen_reply_language_survives_write_config(self):
        """A shop's chosen language must not be reset to the base config's by
        an unrelated edit (label, API key, egw token...)."""
        account = self.registry.create("acct-l", "L")
        cfg = self.registry.config(account)
        cfg.set("messaging.reply_language", "my")
        cfg.save()
        self.registry.write_config(account)
        self.assertEqual(
            self.registry.config(account).get("messaging.reply_language"), "my",
        )

    def _base_says_myanmar(self):
        self.registry.base_config_path.write_text(
            self.registry.base_config_path.read_text(encoding="utf-8")
            + "messaging: {reply_language: my}\n",
            encoding="utf-8",
        )

    def test_a_new_workspace_takes_the_base_config_language(self):
        self._base_says_myanmar()
        account = self.registry.create("acct-new", "New")
        self.assertEqual(
            self.registry.config(account).get("messaging.reply_language"), "my",
        )

    def test_a_workspace_from_before_the_setting_keeps_vietnamese(self):
        """Its YAML is a legacy full copy without reply_language: it has always
        answered in the code fallback (Vietnamese). Converting it on the next
        write, under a base config that now says Myanmar, must not switch it."""
        import yaml
        account = self.registry.create("acct-old", "Old")
        legacy = yaml.safe_load(self.registry.base_config_path.read_text(encoding="utf-8"))
        legacy["account"] = {"id": "acct-old"}
        account.config_path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
        self._base_says_myanmar()

        self.registry.write_config(account)

        self.assertEqual(
            self.registry.config(account).get("messaging.reply_language"), "vi",
        )

    def test_followup_rules_round_trip_through_the_account_s_own_config_file(self):
        """Exercises exactly what dashboard_api.main's followup endpoints do -
        same reasoning as the scenario round-trip test above."""
        account = self.registry.create("acct-f", "F")
        self.assertEqual(
            self.registry.config(account).get("messaging.follow_up.rules", []), [],
        )

        rules = [{"id": "r1", "delay_minutes": 60, "message": "nhắc lại", "enabled": True}]
        cfg = self.registry.config(account)
        cfg.set("messaging.follow_up.rules", rules)
        cfg.save()

        reloaded = self.registry.config(account)
        self.assertEqual(reloaded.get("messaging.follow_up.rules"), rules)
        self.assertEqual(reloaded.get("account.id"), "acct-f")

    def test_the_base_config_decides_the_gemini_model(self):
        """The model is no longer pinned per workspace: whatever
        configs/config.yaml names is what every workspace runs on, so one
        edit there switches them all."""
        base_config = Path(self.tmp.name) / "config-other-model.yaml"
        base_config.write_text(
            "account: {}\ncontrol: {}\nbrowser: {}\nextension_bridge: {}\n"
            "paths: {}\nlogging: {}\ngrounding: {}\nproducts: {}\n"
            "vision:\n  model: some-other-model\n"
            "ai:\n  planner:\n    model: some-other-model\n"
            "llm:\n  model: some-other-model\n",
            encoding="utf-8",
        )
        registry = AccountRegistry(base_config_path=base_config)
        account = registry.create("acct-m", "M")
        path = registry.write_config(account)

        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertNotIn("model", data.get("llm", {}))
        config = registry.config(account)
        for key in ("llm.model", "vision.model", "ai.planner.model"):
            self.assertEqual(config.get(key), "some-other-model")

    def test_registry_round_trip_preserves_new_fields(self):
        self.registry.create(
            "acct-r", "R", api_key="round-trip-key", profile_dir_name="rt-profile",
        )
        reloaded = AccountRegistry(base_config_path=self.registry.base_config_path)
        account = reloaded.get("acct-r")
        self.assertIsNotNone(account)
        self.assertEqual(account.api_key, "round-trip-key")
        self.assertEqual(account.profile_dir_name, "rt-profile")


if __name__ == "__main__":
    unittest.main()
