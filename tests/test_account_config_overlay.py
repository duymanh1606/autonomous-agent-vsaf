"""Workspace configs as overlays on configs/config.yaml.

The defect this guards against was real: workspace YAMLs used to be full
copies of the shared config, so a fix to a shared default (the agent's send
limit, 15 -> 60 per hour) never reached the workspaces created before it,
while an unrelated edit (renaming a workspace) could switch one's behaviour
without warning. Every test here is one half of the contract: the shared
config reaches every workspace, and what was set for one workspace stays.
"""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.accounts as accounts_module
from dashboard_api import account_config
from dashboard_api.account_config import CURRENT_CONFIG_VERSION, overlay_from_legacy
from dashboard_api.accounts import AccountRegistry
from load_config import deep_merge, load_config


BASE = {
    "account": {"id": "default", "platform": "tiktok_web"},
    "browser": {"cdp_url": "http://127.0.0.1:29222", "timeout_ms": 15000},
    "llm": {"model": "base-model"},
    "messaging": {
        "reply_language": "my",
        "reply_style": ["base rule"],
        "auto_check": {"reply_quiet_period_seconds": 8, "interval_seconds": 10},
        "follow_up": {"enabled": True, "rules": []},
    },
    "policy": {"interaction_limits": {"platform.send_message": {"max": 60}}},
    "tools": {"allowed": ["a", "b"]},
}


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


class TempTree(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base_path = write(self.root / "config.yaml", BASE)

    def set_base(self, **messaging):
        base = deep_merge(BASE, {"messaging": messaging})
        write(self.base_path, base)


class OverlayLoadingTests(TempTree):
    def overlay(self, data):
        return write(self.root / "accounts" / "w.yaml", {"extends": "../config.yaml", **data})

    def test_a_shared_default_reaches_the_workspace(self):
        path = self.overlay({"messaging": {"reply_language": "vi"}})
        self.assertEqual(load_config(path).get("browser.timeout_ms"), 15000)

        base = deep_merge(BASE, {"browser": {"timeout_ms": 30000}})
        write(self.base_path, base)
        self.assertEqual(load_config(path).get("browser.timeout_ms"), 30000)

    def test_the_overlay_wins_and_mappings_merge_key_by_key(self):
        config = load_config(self.overlay({
            "messaging": {"auto_check": {"reply_quiet_period_seconds": 3}},
            "tools": {"allowed": ["only"]},
        }))
        self.assertEqual(config.get("messaging.auto_check.reply_quiet_period_seconds"), 3)
        self.assertEqual(config.get("messaging.auto_check.interval_seconds"), 10)
        # A list is one value: replaced whole, never concatenated.
        self.assertEqual(config.get("tools.allowed"), ["only"])
        self.assertIsNone(config.get("extends"))

    def test_save_writes_only_the_overlay(self):
        """Saving the merged view would freeze a full copy again."""
        path = self.overlay({"config_version": 1, "messaging": {"reply_language": "vi"}})
        config = load_config(path)
        config.set("messaging.reply_style", [])
        config.save()

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {
            "extends": "../config.yaml",
            "config_version": 1,
            "messaging": {"reply_language": "vi", "reply_style": []},
        })
        # An emptied list is a choice: it must not fall back to the base rule.
        self.assertEqual(load_config(path).get("messaging.reply_style"), [])

    def test_save_keeps_an_owner_only_file_owner_only(self):
        path = self.overlay({"llm": {"api_key": "secret"}})
        os.chmod(path, 0o600)
        config = load_config(path)
        config.set("messaging.reply_language", "vi")
        config.save()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_setting_nothing_reads_is_reported(self):
        path = self.overlay({"extension_bridge": {"enabled": True}, "egw": {"token": "t"}})
        with self.assertLogs("load_config", level=logging.WARNING) as logs:
            load_config(path)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("extension_bridge", logs.output[0])

    def test_overlays_do_not_chain(self):
        write(self.base_path, {"extends": "other.yaml"})
        with self.assertRaises(ValueError):
            load_config(self.overlay({}))

    def test_a_missing_base_is_an_error_not_an_empty_config(self):
        path = write(self.root / "accounts" / "w.yaml", {"extends": "../nope.yaml"})
        with self.assertRaises(FileNotFoundError):
            load_config(path)

    def test_a_file_without_extends_is_still_a_complete_config(self):
        path = write(self.root / "full.yaml", {"browser": {"timeout_ms": 1}})
        config = load_config(path)
        self.assertFalse(config.is_overlay)
        self.assertIsNone(config.get("messaging.reply_language"))


class LegacyConversionTests(unittest.TestCase):
    def legacy(self, **changes):
        return deep_merge(BASE, changes)

    def test_a_frozen_default_follows_the_shared_config_again(self):
        """The production case: a full copy still saying 15 sends per hour."""
        legacy = self.legacy(policy={"interaction_limits": {"platform.send_message": {"max": 15}}})
        legacy["messaging"]["reply_language"] = "vi"
        overlay = overlay_from_legacy(legacy, BASE)
        self.assertNotIn("policy", overlay)

    def test_an_operator_setting_that_differs_is_kept(self):
        legacy = self.legacy(messaging={
            "reply_language": "vi",
            "auto_check": {"reply_quiet_period_seconds": 20},
            "follow_up": {"rules": [{"id": "r", "delay_minutes": 60, "message": "hi"}]},
        })
        overlay = overlay_from_legacy(legacy, BASE)
        self.assertEqual(overlay["messaging"]["auto_check"], {"reply_quiet_period_seconds": 20})
        self.assertEqual(overlay["messaging"]["follow_up"]["rules"][0]["id"], "r")

    def test_a_copy_from_before_the_language_setting_keeps_vietnamese(self):
        legacy = self.legacy()
        del legacy["messaging"]["reply_language"]
        self.assertEqual(overlay_from_legacy(legacy, BASE)["messaging"]["reply_language"], "vi")

    def test_a_chosen_language_equal_to_the_default_follows_it(self):
        self.assertNotIn("messaging", overlay_from_legacy(self.legacy(), BASE))


class RegistryOverlayTests(TempTree):
    def setUp(self):
        super().setUp()
        accounts_dir = self.root / "accounts"
        for name, value in (("ACCOUNTS_DIR", accounts_dir),
                            ("REGISTRY_PATH", accounts_dir / "accounts.json")):
            patcher = mock.patch.object(accounts_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = AccountRegistry(base_config_path=self.base_path)

    def raw(self, account):
        return yaml.safe_load(account.config_path.read_text(encoding="utf-8"))

    def test_a_new_workspace_holds_only_its_own_identity(self):
        account = self.registry.create("acct-new", "New")
        raw = self.raw(account)
        self.assertEqual(raw["extends"], "../config.yaml")
        self.assertEqual(raw["config_version"], CURRENT_CONFIG_VERSION)
        self.assertNotIn("messaging", raw)
        self.assertNotIn("tools", raw)

        config = self.registry.config(account)
        self.assertEqual(config.get("account.id"), "acct-new")
        self.assertEqual(config.get("account.platform"), "tiktok_web")
        self.assertEqual(config.get("messaging.reply_language"), "my")
        self.assertEqual(config.get("browser.cdp_url"), f"http://127.0.0.1:{account.cdp_port}")

    def test_a_shared_change_reaches_an_existing_workspace(self):
        account = self.registry.create("acct-a", "A")
        write(self.base_path, deep_merge(
            BASE, {"policy": {"interaction_limits": {"platform.send_message": {"max": 90}}}},
        ))
        limits = self.registry.config(account).get("policy.interaction_limits")
        self.assertEqual(limits["platform.send_message"]["max"], 90)

    def test_an_operator_setting_survives_an_unrelated_edit(self):
        account = self.registry.create("acct-b", "B")
        config = self.registry.config(account)
        config.set("messaging.auto_check.reply_quiet_period_seconds", 25)
        config.save()

        self.registry.update("acct-b", label="Renamed")

        config = self.registry.config(account)
        self.assertEqual(config.get("messaging.auto_check.reply_quiet_period_seconds"), 25)
        self.assertEqual(config.get("account.label"), "Renamed")

    def test_a_revoked_key_or_token_leaves_the_file(self):
        account = self.registry.create("acct-c", "C", api_key="k-1")
        self.assertEqual(self.raw(account)["llm"]["api_key"], "k-1")
        self.registry.update("acct-c", api_key="")
        self.assertNotIn("api_key", self.raw(account).get("llm", {}))

    def test_a_legacy_copy_is_converted_on_the_next_write(self):
        account = self.registry.create("acct-d", "D")
        legacy = deep_merge(BASE, {
            "account": {"id": "acct-d"},
            "policy": {"interaction_limits": {"platform.send_message": {"max": 15}}},
            "extension_bridge": {"enabled": True},
        })
        del legacy["messaging"]["reply_language"]
        write(account.config_path, legacy)
        self.assertEqual([a.id for a in self.registry.legacy_configs()], ["acct-d"])

        self.registry.write_config(account)

        self.assertEqual(self.registry.legacy_configs(), [])
        config = self.registry.config(account)
        self.assertEqual(
            config.get("policy.interaction_limits")["platform.send_message"]["max"], 60,
        )
        self.assertEqual(config.get("messaging.reply_language"), "vi")
        self.assertNotIn("extension_bridge", self.raw(account))

    def test_an_older_overlay_is_migrated_once(self):
        account = self.registry.create("acct-e", "E")
        raw = self.raw(account)
        raw["config_version"] = 0
        write(account.config_path, raw)

        self.assertEqual(self.registry.migrate_overlays(), ["acct-e"])
        self.assertEqual(self.raw(account)["config_version"], CURRENT_CONFIG_VERSION)
        self.assertEqual(self.registry.config(account).get("messaging.reply_language"), "vi")
        self.assertEqual(self.registry.migrate_overlays(), [])

    def test_the_model_comes_from_the_shared_config(self):
        """Models used to be pinned into every workspace file, so changing
        llm.model in configs/config.yaml reached no workspace at all."""
        account = self.registry.create("acct-m", "M")
        self.assertNotIn("model", self.raw(account).get("llm", {}))
        self.assertEqual(self.registry.config(account).get("llm.model"), "base-model")

        write(self.base_path, deep_merge(BASE, {"llm": {"model": "gemini-next"}}))
        self.assertEqual(self.registry.config(account).get("llm.model"), "gemini-next")

    def test_models_pinned_by_an_older_version_are_removed(self):
        account = self.registry.create("acct-p", "P", api_key="k")
        raw = self.raw(account)
        raw["config_version"] = 1
        raw["llm"]["model"] = "gemini-3.1-flash-lite"
        raw["ai"] = {"planner": {"model": "gemini-3.1-flash-lite"}}
        raw["vision"] = {"gemini": {"model": "gemini-3.1-flash-lite"}}
        write(account.config_path, raw)

        self.assertEqual(self.registry.migrate_overlays(), ["acct-p"])

        raw = self.raw(account)
        self.assertEqual(raw["llm"], {"api_key": "k"})  # the key stays, the model goes
        self.assertNotIn("ai", raw)
        self.assertNotIn("vision", raw)
        self.assertEqual(self.registry.config(account).get("llm.model"), "base-model")

    def test_a_new_workspace_never_runs_an_old_migration(self):
        account = self.registry.create("acct-f", "F")
        self.registry.migrate_overlays()
        self.assertEqual(self.registry.config(account).get("messaging.reply_language"), "my")


class RealBaseConfigTests(unittest.TestCase):
    def test_what_render_config_writes_is_all_known_to_the_real_base(self):
        """Every path write_config owns must be either in configs/config.yaml
        or declared account-only, or every agent start logs false warnings."""
        from load_config import DEFAULT_CONFIG_PATH
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        accounts_dir = Path(tmp.name) / "accounts"
        with mock.patch.object(accounts_module, "ACCOUNTS_DIR", accounts_dir), \
                mock.patch.object(accounts_module, "REGISTRY_PATH", accounts_dir / "a.json"):
            registry = AccountRegistry(base_config_path=DEFAULT_CONFIG_PATH)
            account = registry.create("acct-real", "Real", api_key="k")
            with self.assertNoLogs("load_config", level=logging.WARNING):
                config = registry.config(account)
        self.assertTrue(config.is_overlay)


class MigrationListTests(unittest.TestCase):
    def test_versions_are_strictly_increasing(self):
        """Append-only: a reordered or reused number would skip or re-run one."""
        numbers = [number for number, _ in account_config.CONFIG_MIGRATIONS]
        self.assertEqual(numbers, sorted(set(numbers)))


if __name__ == "__main__":
    unittest.main()
