import os
import unittest
from unittest.mock import Mock, patch

import yaml

import app


class SubscriptionFormatTests(unittest.TestCase):
    def test_clash_uses_clean_base_url_without_format_flags(self):
        with patch.dict(
            os.environ,
            {
                "UPSTREAM_SUBSCRIPTION_URL": (
                    "https://provider.example/subscribe?token=secret"
                    "&flag=shadowrocket&sr=conf"
                )
            },
        ):
            self.assertEqual(
                app.formatted_subscription_url("clash"),
                "https://provider.example/subscribe?token=secret",
            )

    def test_shadowrocket_adds_its_format_flags(self):
        with patch.dict(
            os.environ,
            {
                "UPSTREAM_SUBSCRIPTION_URL": (
                    "https://provider.example/subscribe?token=secret&flag=clash"
                )
            },
        ):
            self.assertEqual(
                app.formatted_subscription_url("shadowrocket"),
                "https://provider.example/subscribe?token=secret"
                "&flag=shadowrocket&sr=conf",
            )

    @patch("app.fetch_subscription")
    def test_clash_config_retains_upstream_proxies(self, fetch_subscription):
        response = Mock()
        response.text = yaml.safe_dump(
            {
                "proxies": [{"name": "node", "type": "vmess"}],
                "proxy-groups": [
                    {"name": "PROXY", "type": "select", "proxies": ["node"]}
                ],
                "rules": ["MATCH,PROXY"],
            }
        )
        fetch_subscription.return_value = response

        with patch.dict(
            os.environ,
            {"UPSTREAM_SUBSCRIPTION_URL": "https://provider.example/subscribe?token=secret"},
        ):
            config = app.upstream_config()

        self.assertEqual(config["proxies"][0]["name"], "node")
        requested_url = fetch_subscription.call_args.args[0]
        self.assertNotIn("flag=clash", requested_url)


if __name__ == "__main__":
    unittest.main()
