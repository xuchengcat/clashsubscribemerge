import copy
import unittest

import app


def source():
    return {
        'ipv6': True,
        'proxies': [{'name': n, 'type': 'vmess', 'server': 'example.com', 'port': 443,
                     'uuid': '00000000-0000-0000-0000-000000000000'}
                    for n in ['日本-优化', '新加坡-优化-GPT', '美国LA-优化-GPT']],
        'proxy-groups': [
            {'name': '节点选择', 'type': 'select', 'proxies': ['日本-优化']},
            {'name': 'ChatGPT', 'type': 'select', 'proxies': ['节点选择', '新加坡-优化-GPT']},
            {'name': 'Copilot', 'type': 'select', 'proxies': ['日本-优化']},
        ],
        'rules': ['DOMAIN-SUFFIX,workos.com,DIRECT', 'DOMAIN,challenges.cloudflare.com,Copilot',
                  'DOMAIN-SUFFIX,stripe.com,Copilot', 'DOMAIN-SUFFIX,example.org,DIRECT', 'MATCH,节点选择'],
        'dns': {'ipv6': True, 'enhanced-mode': 'fake-ip',
                'nameserver': ['https://dns.example/dns-query'],
                'nameserver-policy': {'+.example.org': 'system'}},
    }


class GPTPolicyTests(unittest.TestCase):
    def test_group_excludes_general_selector_and_prefers_server_order(self):
        result = app.apply_gpt_policy(source())
        g = next(g for g in result['proxy-groups'] if g['name'] == 'ChatGPT')
        self.assertEqual(g['type'], 'fallback')
        self.assertEqual(g['proxies'], ['美国LA-优化-GPT', '新加坡-优化-GPT'])
        self.assertEqual(g['url'], 'https://1.1.1.1/cdn-cgi/trace')
        self.assertFalse(g['lazy'])

    def test_gpt_dependencies_override_conflicting_provider_rules(self):
        rules = app.apply_gpt_policy(source())['rules']
        self.assertIn('DOMAIN-SUFFIX,workos.com,ChatGPT', rules)
        self.assertNotIn('DOMAIN-SUFFIX,workos.com,DIRECT', rules)
        self.assertNotIn('DOMAIN,challenges.cloudflare.com,Copilot', rules)
        self.assertLess(rules.index('DOMAIN,js.stripe.com,ChatGPT'), rules.index('DOMAIN-SUFFIX,stripe.com,Copilot'))
        self.assertIn('DOMAIN-SUFFIX,example.org,DIRECT', rules)
        self.assertEqual(rules[-1], 'MATCH,节点选择')

    def test_scoped_dns_preserves_ipv6_and_other_domain_policies(self):
        r = app.apply_gpt_policy(source())
        self.assertTrue(r['ipv6'])
        self.assertTrue(r['dns']['ipv6'])
        self.assertEqual(r['dns']['nameserver-policy']['+.example.org'], 'system')
        for key in ['+.chatgpt.com', '+.openai.com', '+.workos.com', 'challenges.cloudflare.com']:
            v = r['dns']['nameserver-policy'][key][0]
            self.assertIn('#ChatGPT&disable-ipv6=true&disable-qtype-65=true', v)
        self.assertTrue(all('#DIRECT' in x for x in r['dns']['proxy-server-nameserver']))

    def test_input_not_mutated_and_policy_is_idempotent(self):
        data = source(); before = copy.deepcopy(data)
        result = app.apply_gpt_policy(data)
        self.assertEqual(data, before)
        self.assertEqual(app.apply_gpt_policy(result), result)

    def test_missing_gpt_nodes_never_falls_back_to_arbitrary_nodes(self):
        data = source(); data['proxies'] = [data['proxies'][0]]
        with self.assertRaisesRegex(RuntimeError, 'GPT-labelled'):
            app.apply_gpt_policy(data)

    def test_gpt_group_added_when_provider_only_supplies_tagged_nodes(self):
        data = source(); data['proxy-groups'] = data['proxy-groups'][:1]
        self.assertTrue(any(g['name'] == 'ChatGPT' for g in app.apply_gpt_policy(data)['proxy-groups']))

    def test_shadowrocket_uses_eligible_nodes_and_remote_dns_with_compact_hosts(self):
        data = source()
        out = app.shadowrocket_config('2001:db8::1', data, data['proxies'])
        self.assertIn('ChatGPT = select,美国LA-优化-GPT,新加坡-优化-GPT,', out)
        self.assertIn('DOMAIN-SUFFIX,workos.com,ChatGPT,force-remote-dns', out)
        self.assertIn('DOMAIN,js.stripe.com,ChatGPT,force-remote-dns', out)
        self.assertNotIn('disable-qtype-65', out)
        hosts = out.split('[Host]\n', 1)[1].strip().splitlines()
        self.assertEqual(len(hosts), 4)
        self.assertIn('*.speedtest.xuccat.dpdns.org = 2001:db8::1', hosts)
        self.assertIn('use-local-host-item-for-proxy = true', out)
        self.assertTrue(out.index('DOMAIN-SUFFIX,speedtest.xuccat.dpdns.org,DIRECT') < out.index('DOMAIN-SUFFIX,workos.com,ChatGPT'))

    def test_shadowrocket_group_uses_its_actual_node_subscription(self):
        data = source()
        nodes = [p for p in data['proxies'] if p['name'] != '美国LA-优化-GPT']
        out = app.shadowrocket_config('2001:db8::1', data, nodes)
        line = next(l for l in out.splitlines() if l.startswith('ChatGPT ='))
        self.assertIn('新加坡-优化-GPT', line)
        self.assertNotIn('美国LA-优化-GPT', line)
        self.assertNotIn('节点选择', line)
