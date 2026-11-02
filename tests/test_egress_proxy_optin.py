"""egress-ip-gateway (egw) integration is opt-in per account.

The only switch is a non-empty `Account.egw_token`. Without it a workspace
must behave exactly as before egw existed: no `egw` section in its YAML, no
`--proxy-server` on its Chrome, regardless of what `EGW_*` environment
variables say. An unconditional proxy flag would point every Chrome on a
machine without egw at a port nobody listens on and take every workspace
offline at once - see docs/egress-proxy-integration.md, section 0.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard_api.accounts as accounts_module
from dashboard_api.accounts import AccountRegistry
from dashboard_api.processes import AccountProcesses


def _proxy_flags(command: list[str]) -> list[str]:
    return [arg for arg in command if arg.startswith("--proxy-server")]


class _RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        accounts_dir = Path(self.tmp.name) / "accounts"
        for target, value in (
            ("ACCOUNTS_DIR", accounts_dir),
            ("REGISTRY_PATH", accounts_dir / "accounts.json"),
        ):
            patcher = mock.patch.object(accounts_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        base_config = Path(self.tmp.name) / "config.yaml"
        base_config.write_text(
            "account: {}\ncontrol: {}\nbrowser: {}\nextension_bridge: {}\n"
            "paths: {}\nlogging: {}\ngrounding: {}\nllm: {}\nproducts: {}\n",
            encoding="utf-8",
        )
        self.registry = AccountRegistry(base_config_path=base_config)

    def _config_of(self, account) -> dict:
        path = self.registry.write_config(account)
        return yaml.safe_load(path.read_text(encoding="utf-8"))


class WithoutTokenNothingChanges(_RegistryCase):
    """Guards today's behaviour. Must stay green through the whole rollout."""

    def test_write_config_has_no_egw_section(self):
        account = self.registry.create("acct-plain", "Plain")
        self.assertNotIn("egw", self._config_of(account))

    def test_chrome_command_has_no_proxy_flag(self):
        account = self.registry.create("acct-plain", "Plain")
        self.assertEqual(_proxy_flags(AccountProcesses(account).chrome.command), [])

    def test_egw_env_alone_does_not_turn_the_proxy_on(self):
        """`EGW_PROXY_URL` being set on the machine means egw is installed
        there, not that this particular account should use it."""
        account = self.registry.create("acct-plain", "Plain")
        env = {
            "EGW_ENABLED": "1",
            "EGW_PROXY_URL": "http://127.0.0.1:3128",
            "EGW_ADMIN_URL": "http://127.0.0.1:9090",
            "EGW_ADMIN_TOKEN": "admin-secret",
        }
        with mock.patch.dict(os.environ, env):
            self.assertNotIn("egw", self._config_of(account))
            self.assertEqual(
                _proxy_flags(AccountProcesses(account).chrome.command), [],
            )

    def test_importing_dashboard_api_needs_no_egw_env(self):
        """The supervisor must boot on a machine that has never heard of egw.
        Reading `EGW_ADMIN_TOKEN` at module scope would raise KeyError on
        import and take the whole dashboard down."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("EGW_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            import importlib
            importlib.reload(accounts_module)
        # Reload again with the real environment so later tests see the
        # module exactly as the test runner imported it.
        importlib.reload(accounts_module)


class MasterSwitchOffHidesEverything(_RegistryCase):
    """EGW_ENABLED unset/0: a token on file changes nothing, and is kept."""

    def test_token_without_the_switch_yields_no_proxy_and_no_yaml_section(self):
        clean = {k: v for k, v in os.environ.items() if k != "EGW_ENABLED"}
        with mock.patch.dict(os.environ, clean, clear=True):
            account = self.registry.create("acct-off", "Off", egw_token="tok-kept")
            self.assertNotIn("egw", self._config_of(account))
            self.assertEqual(_proxy_flags(AccountProcesses(account).chrome.command), [])
            data = account.as_dict()
        self.assertTrue(data["has_egw_token"])
        self.assertFalse(data["egw_active"])
        self.assertEqual(account.egw_token, "tok-kept")

    def test_zero_and_false_are_off(self):
        for value in ("0", "false", "no", "off", ""):
            with mock.patch.dict(os.environ, {"EGW_ENABLED": value}):
                self.assertFalse(accounts_module.egw_feature_enabled(), value)
        for value in ("1", "true", "yes", "on", " TRUE "):
            with mock.patch.dict(os.environ, {"EGW_ENABLED": value}):
                self.assertTrue(accounts_module.egw_feature_enabled(), value)

    def test_switching_on_later_activates_the_stored_token(self):
        clean = {k: v for k, v in os.environ.items() if k != "EGW_ENABLED"}
        with mock.patch.dict(os.environ, clean, clear=True):
            account = self.registry.create("acct-later", "Later", egw_token="tok-kept")
        with mock.patch.dict(os.environ, {"EGW_ENABLED": "1"}):
            self.assertEqual(self._config_of(account)["egw"]["token"], "tok-kept")
            self.assertTrue(account.as_dict()["egw_active"])


class WithTokenProxyIsOptedIn(_RegistryCase):
    """Switch on + token present: the proxy is wired in for this account only."""

    def setUp(self):
        super().setUp()
        self._switch = mock.patch.dict(os.environ, {"EGW_ENABLED": "1"})
        self._switch.start()
        self.addCleanup(self._switch.stop)

    def test_egw_token_never_appears_in_as_dict(self):
        account = self.registry.create("acct-egw", "Egw", egw_token="tok-123")
        data = account.as_dict()
        self.assertNotIn("egw_token", data)
        self.assertTrue(data["has_egw_token"])
        self.assertEqual(account.egw_token, "tok-123")

    def test_registry_round_trip_preserves_egw_token(self):
        self.registry.create("acct-egw", "Egw", egw_token="tok-123")
        reloaded = AccountRegistry(base_config_path=self.registry.base_config_path)
        self.assertEqual(reloaded.get("acct-egw").egw_token, "tok-123")

    def test_write_config_scopes_egw_section_to_this_account(self):
        account = self.registry.create("acct-egw", "Egw", egw_token="tok-123")
        with mock.patch.dict(os.environ, {"EGW_PROXY_URL": "http://127.0.0.1:3128"}):
            egw = self._config_of(account)["egw"]
        self.assertEqual(egw["workspace_id"], "acct-egw")
        self.assertEqual(egw["token"], "tok-123")
        self.assertEqual(egw["proxy_url"], "http://127.0.0.1:3128")

    def test_write_config_defaults_proxy_url_when_env_is_unset(self):
        account = self.registry.create("acct-egw", "Egw", egw_token="tok-123")
        clean = {k: v for k, v in os.environ.items() if k != "EGW_PROXY_URL"}
        with mock.patch.dict(os.environ, clean, clear=True):
            egw = self._config_of(account)["egw"]
        self.assertEqual(egw["proxy_url"], "http://127.0.0.1:3128")

    def test_chrome_command_points_at_the_single_egw_listener(self):
        """Every egw-enabled Chrome uses the same proxy address; the route is
        chosen by workspace_id/token at CONNECT time, not by the address."""
        account = self.registry.create("acct-egw", "Egw", egw_token="tok-123")
        self.registry.write_config(account)
        with mock.patch.dict(os.environ, {"EGW_PROXY_URL": "http://127.0.0.1:3128"}):
            flags = _proxy_flags(AccountProcesses(account).chrome.command)
        self.assertEqual(flags, ["--proxy-server=http://127.0.0.1:3128"])

    def test_proxy_flag_never_becomes_the_orphan_match_key(self):
        """`_find_running_pid` matches an orphan Chrome by the LONGEST
        path-like argument. `--proxy-server=http://...` contains a slash
        too; it must always lose to `--user-data-dir=<profile>`, otherwise
        adoption after a supervisor restart would match any egw-enabled
        Chrome on the machine."""
        account = self.registry.create("acct-egw", "Egw", egw_token="tok-123")
        self.registry.write_config(account)
        command = AccountProcesses(account).chrome.command
        longest = max((a for a in command if "/" in a), key=len)
        self.assertTrue(longest.startswith("--user-data-dir="), longest)

    def test_chrome_command_is_refreshed_when_token_is_added_later(self):
        """The AccountProcesses object is cached for the supervisor's
        lifetime; opting in after it was built must still reach Chrome."""
        account = self.registry.create("acct-late", "Late")
        procs = AccountProcesses(account)
        self.assertEqual(_proxy_flags(procs.chrome.command), [])
        self.registry.update("acct-late", egw_token="tok-late")
        self.assertEqual(_proxy_flags(procs._chrome_command()),
                         [f"--proxy-server={accounts_module.DEFAULT_EGW_PROXY_URL}"])

    def test_two_accounts_share_the_proxy_address_but_not_the_identity(self):
        a = self.registry.create("acct-a", "A", egw_token="tok-a")
        b = self.registry.create("acct-b", "B", egw_token="tok-b")
        egw_a, egw_b = self._config_of(a)["egw"], self._config_of(b)["egw"]
        self.assertEqual(egw_a["proxy_url"], egw_b["proxy_url"])
        self.assertNotEqual(egw_a["workspace_id"], egw_b["workspace_id"])
        self.assertNotEqual(egw_a["token"], egw_b["token"])


if __name__ == "__main__":
    unittest.main()
