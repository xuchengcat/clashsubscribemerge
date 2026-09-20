import os
import unittest
from unittest.mock import patch

import yaml
from requests import Response

import app


CLASH_CONFIG = {
    "proxies": [
        {
            "name": "node",
            "type": "vmess",
            "server": "example.com",
            "port": 443,
            "uuid": "00000000-0000-0000-0000-000000000000",
        }
    ],
    "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["node"]}],
    "rules": ["MATCH,PROXY"],
}


class SubscriptionMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"SUBSCRIPTION_TOKEN": "test-token"})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_metadata_extraction_is_case_insensitive_and_whitelisted(self) -> None:
        response = Response()
        response.headers.update(
            {
                "subscription-userinfo": "upload=1; download=2; total=3; expire=",
                "PROFILE-UPDATE-INTERVAL": "24",
                "Profile-Web-Page-Url": "https://example.com/dashboard",
                "Set-Cookie": "secret=value",
                "Content-Length": "999",
            }
        )

        self.assertEqual(
            app.subscription_metadata(response),
            {
                "Subscription-Userinfo": "upload=1; download=2; total=3; expire=",
                "Profile-Update-Interval": "24",
                "Profile-Web-Page-Url": "https://example.com/dashboard",
            },
        )

    @patch.object(app, "read_domains", return_value=["managed.example"])
    @patch.object(app, "zerotier_ip_address", return_value="2001:db8::1")
    @patch.object(app, "upstream_config")
    def test_clash_endpoint_forwards_its_metadata(self, upstream, _address, _domains) -> None:
        upstream.return_value = (
            CLASH_CONFIG.copy(),
            {
                "Subscription-Userinfo": "upload=1; download=2; total=3; expire=",
                "Profile-Update-Interval": "24",
            },
        )

        response = app.subscription("test-token")

        self.assertEqual(
            response.headers["subscription-userinfo"],
            "upload=1; download=2; total=3; expire=",
        )
        self.assertEqual(response.headers["profile-update-interval"], "24")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(
            yaml.safe_load(response.body)["hosts"]["managed.example"], "2001:db8::1"
        )

    @patch.object(app, "read_domains", return_value=["managed.example"])
    @patch.object(app, "zerotier_ip_address", return_value="2001:db8::1")
    @patch.object(app, "upstream_shadowrocket_proxies")
    @patch.object(app, "upstream_config")
    def test_shadowrocket_endpoint_uses_shadowrocket_metadata(
        self, upstream_clash, upstream_shadowrocket, _address, _domains
    ) -> None:
        upstream_clash.return_value = (
            CLASH_CONFIG.copy(),
            {"Subscription-Userinfo": "upload=clash"},
        )
        upstream_shadowrocket.return_value = (
            CLASH_CONFIG["proxies"],
            {
                "Subscription-Userinfo": "upload=shadowrocket",
                "Profile-Update-Interval": "24",
            },
        )

        response = app.shadowrocket_subscription("test-token")

        self.assertEqual(response.headers["subscription-userinfo"], "upload=shadowrocket")
        self.assertEqual(response.headers["profile-update-interval"], "24")
        self.assertIn("managed.example = 2001:db8::1", response.body.decode())

    @patch.object(app, "read_domains", return_value=["managed.example"])
    @patch.object(app, "zerotier_ip_address", return_value="2001:db8::1")
    @patch.object(app, "upstream_config", return_value=(CLASH_CONFIG.copy(), {}))
    def test_missing_metadata_does_not_break_clash_endpoint(
        self, _upstream, _address, _domains
    ) -> None:
        response = app.subscription("test-token")

        self.assertNotIn("subscription-userinfo", response.headers)


if __name__ == "__main__":
    unittest.main()
