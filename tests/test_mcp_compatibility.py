"""Offline configuration and real stdio protocol regression tests."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

try:
    import tomllib
except ImportError:  # Python 3.10 test environments
    import tomli as tomllib

import mcp_config
import search_index as si

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    'database_status', 'find_tags', 'list_tags', 'search_cards', 'get_cards',
    'card_tags', 'analyze_deck', 'edhrec_recommendations', 'edhrec_average_deck',
    'rules_lookup', 'rules_search', 'rules_glossary',
    'import_collection', 'list_collections', 'collection_info', 'delete_collection',
}
# The only tools that write, and only to collections.db beside the card database.
WRITE_TOOLS = {'import_collection', 'delete_collection'}


class ConfigTests(unittest.TestCase):
    def assert_same_launch(self):
        claude = json.loads(mcp_config.claude_desktop_snippet())['mcpServers']['mtg-cards']
        codex = tomllib.loads(mcp_config.codex_snippet())['mcp_servers']['mtg-cards']
        self.assertEqual(claude['command'], codex['command'])
        self.assertEqual(claude['args'], codex['args'])
        self.assertGreaterEqual(codex['startup_timeout_sec'], 30)
        return claude

    def test_source_paths_and_escaping(self):
        for executable in ('C:\\Users\\A Name\\カード 🃏\\python.exe', '/tmp/a "quoted" path/python'):
            with self.subTest(executable=executable), patch.object(sys, 'executable', executable):
                entry = self.assert_same_launch()
                self.assertEqual(entry['command'], executable)
                self.assertEqual(entry['args'], [str(ROOT / 'mcp_server.py')])

    def test_pythonw_uses_console_interpreter(self):
        with patch.object(sys, 'executable', str(ROOT / 'pythonw.exe')):
            self.assertEqual(self.assert_same_launch()['command'], str(ROOT / 'python.exe'))

    def test_packaged_launch(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(
                mcp_config, 'packaged_mcp_executable', return_value='/Applications/Card App/MtgCardsMCP'):
            self.assertEqual(self.assert_same_launch(), {
                'command': '/Applications/Card App/MtgCardsMCP', 'args': []})

    def test_codex_home(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(ROOT / 'custom home')}):
            self.assertEqual(mcp_config.codex_config_path(), str(ROOT / 'custom home' / 'config.toml'))


class StdioTests(unittest.TestCase):
    def start_server(self, db, cwd):
        # MCP_SERVER_BINARY lets CI exercise the actual PyInstaller artifact.
        binary = os.environ.get('MCP_SERVER_BINARY')
        command = [str(Path(binary).resolve())] if binary else [sys.executable, str(ROOT / 'mcp_server.py')]
        stderr = tempfile.TemporaryFile(mode='w+', encoding='utf-8')
        self.addCleanup(stderr.close)
        proc = subprocess.Popen(command + ['--db', str(db)], cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, text=True, encoding='utf-8')
        messages = queue.Queue()
        def read():
            for line in proc.stdout:
                messages.put(line)
            messages.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def cleanup():
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            reader.join(timeout=5)
            proc.stdout.close()
        self.addCleanup(cleanup)
        def request(method, params=None, request_id=1):
            msg = {'jsonrpc': '2.0', 'method': method}
            if params is not None:
                msg['params'] = params
            if request_id is not None:
                msg['id'] = request_id
            proc.stdin.write(json.dumps(msg) + '\n')
            proc.stdin.flush()
            if request_id is None:
                return
            while True:
                try:
                    line = messages.get(timeout=30)
                except queue.Empty:
                    self.fail('MCP response timed out')
                if line is None:
                    stderr.seek(0)
                    self.fail('MCP exited: ' + stderr.read())
                response = json.loads(line)  # Any non-JSON stdout fails the test.
                if response.get('id') == request_id:
                    self.assertNotIn('error', response, response)
                    return response['result']
        return request

    def initialize(self, request, client, version):
        result = request('initialize', {'protocolVersion': version, 'capabilities': {},
                                       'clientInfo': {'name': client, 'version': '1.0'}})
        self.assertEqual(result['serverInfo']['name'], 'mtg-cards')
        self.assertIn('tools', result['capabilities'])
        request('notifications/initialized', request_id=None)

    def test_clients_discover_and_call_tools(self):
        for client, version in [('codex', '2025-06-18'), ('claude-desktop', '2024-11-05')]:
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                # Spaces and non-ASCII paths; launch outside the source directory.
                db = Path(tmp) / 'cards café.db'
                conn = si.connect(str(db))
                conn.execute('CREATE TABLE cards (name_normalized TEXT PRIMARY KEY, data TEXT)')
                card = {'name': 'Sol Ring', 'oracle_id': 'test-sol-ring', 'layout': 'normal',
                        'mana_cost': '{1}', 'cmc': 1, 'type_line': 'Artifact',
                        'oracle_text': '{T}: Add {C}{C}.', 'color_identity': [],
                        'rarity': 'uncommon', 'legalities': {'commander': 'legal'},
                        'prices': {'eur': '1.25'}, 'games': ['paper']}
                conn.execute('INSERT INTO cards VALUES (?, ?)', ('sol ring', json.dumps(card)))
                conn.commit()
                si.rebuild_card_index(conn)
                conn.close()
                original = db.read_bytes()
                request = self.start_server(db, tmp)
                try:
                    self.initialize(request, client, version)
                    tools = {tool['name']: tool for tool in request('tools/list')['tools']}
                    self.assertEqual(set(tools), EXPECTED_TOOLS)
                    for name, tool in tools.items():
                        self.assertEqual(tool['inputSchema']['type'], 'object')
                        self.assertEqual(tool['annotations']['readOnlyHint'], name not in WRITE_TOOLS)
                        self.assertEqual(tool['annotations']['openWorldHint'], name.startswith('edhrec_'))
                    self.assertEqual(tools['find_tags']['inputSchema']['required'], ['query'])
                    # Nested refinements must be discoverable to both clients,
                    # without adding another search tool or changing old calls.
                    self.assertTrue(tools['delete_collection']['annotations']['destructiveHint'])
                    for name in ('search_cards', 'edhrec_recommendations'):
                        schema = tools[name]['inputSchema']
                        self.assertIn('collection', schema['properties'])
                        self.assertIn('filters', schema['properties'])
                        self.assertIn('legalities', json.dumps(schema))
                        self.assertIn('stats', json.dumps(schema))
                    def call(name, arguments):
                        return request('tools/call', {'name': name, 'arguments': arguments})
                    status = call('database_status', {})
                    self.assertFalse(status.get('isError', False))
                    self.assertIn('Indexed cards: 1', status['content'][0]['text'])
                    result = call('search_cards', {'text': 'Sol Ring', 'limit': 1})
                    self.assertIn('Sol Ring', result['content'][0]['text'])
                    empty_page = call('search_cards', {'text': 'Sol Ring', 'offset': 1})
                    self.assertIn('1 cards match, but offset=1', empty_page['content'][0]['text'])
                    result = call('search_cards', {
                        'color_identity': 'C', 'price_max': 2,
                        'filters': {'color_identity_match': 'exact', 'price_min': 1,
                                    'legalities': {'commander': 'legal'}, 'name_contains': 'Ring'},
                    })
                    self.assertIn('Sol Ring', result['content'][0]['text'])
                    self.assertTrue(call('search_cards', {'filters': {'price_mni': 1}})['isError'])
                    self.assertTrue(call('search_cards', {'filters': {'stats': {'power': {'min': 3, 'max': 2}}}})['isError'])
                    bad_range = call('search_cards', {'mv_min': 4, 'mv_max': 1})
                    self.assertIn('Minimum mana value', bad_range['content'][0]['text'])
                    self.assertTrue(call('find_tags', {'query': 'ramp', 'limit': 0})['isError'])
                    self.assertIn('Sol Ring', call('search_cards', {'text': 'Sol Ring'})['content'][0]['text'])
                    export = Path(tmp) / 'my collection.csv'
                    export.write_text('Count,Name\n3,Sol Ring\n1,Not A Card\n', encoding='utf-8')
                    imported = call('import_collection', {'name': 'binder', 'path': str(export)})
                    self.assertIn("Imported 'binder': 1 unique cards", imported['content'][0]['text'])
                    owned = call('search_cards', {'collection': 'binder', 'text': '"add {C}"'})
                    self.assertIn('| Sol Ring |', owned['content'][0]['text'])
                    self.assertIn('| 3 |', owned['content'][0]['text'])
                    self.assertTrue((Path(tmp) / 'collections.db').exists())
                    resources = request('resources/list')['resources']
                    self.assertIn('rules://comprehensive-rules', [r['uri'] for r in resources])
                    self.assertEqual(db.read_bytes(), original)
                finally:
                    # Close the process before TemporaryDirectory removes its database (Windows).
                    self.doCleanups()

    def test_missing_database_still_initializes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'missing.db'
            request = self.start_server(db, tmp)
            try:
                self.initialize(request, 'codex', '2025-06-18')
                result = request('tools/call', {'name': 'database_status', 'arguments': {}})
                self.assertIn('Update Local DB', result['content'][0]['text'])
                self.assertFalse(db.exists())
            finally:
                self.doCleanups()


if __name__ == '__main__':
    unittest.main()
