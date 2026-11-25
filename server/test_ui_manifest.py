import unittest_isolated  # noqa: F401  (B290: isolate before fixtures)
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import custom_layouts as layouts
from layout_generate import built_ins
from test_custom_layouts import GOOD, changed

ROOT = Path(__file__).resolve().parent.parent


def browser_dependencies_available():
    return bool(shutil.which('node')) and subprocess.run(
        ['node', '-e', "require.resolve('typescript')"], cwd=ROOT / 'ui',
        capture_output=True, timeout=30,
    ).returncode == 0


def browser_check(data):
    """Run the actual browser validator without a DOM or a browser server."""
    script = r"""
const fs = require('fs'), path = require('path'), Module = require('module'), ts = require('typescript');
const filename = path.resolve('src/layouts.ts');
// layouts.ts and each TypeScript file it imports (for example ./tones) compile on load.
Module._extensions['.ts'] = (mod, file) => mod._compile(ts.transpileModule(fs.readFileSync(file, 'utf8'), {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true
}}).outputText, file);
const api = require(filename), data = JSON.parse(fs.readFileSync(0, 'utf8'));
const checked = data.map(item => { try { return {layout: api.validateLayout(item, 'fixture')}; }
  catch (error) { return {error: error.message}; } });
const custom = api.validateLayout({...data[0], variants: {sessions:'avatars', chat:'bubbles', tabs:'pills'}, phone:'keep', manifest:2}, 'fixture');
const restored = api.lookLayout({layout:'custom:fixture', surface:'flat', backdrop:'ember', custom:{...custom, description:undefined}});
(async () => {
  const calls = [], waits = [];
  let poll = 0, abortWait;
  global.setTimeout = (fn, ms) => { waits.push(ms); queueMicrotask(() => abortWait ? abortWait.abort() : fn()); return 1; };
  global.clearTimeout = () => {};
  const client = async (url, body, signal) => {
    signal?.throwIfAborted(); calls.push({url, body});
    if (url === 'layouts/generate') return {job:'job-1'};
    if (url.startsWith('layouts/generate?')) return ++poll === 1 ? {state:'running'} : {state:'done', layout:custom, notes:'Calm.'};
    return {layout:custom, file:'fixture.json', ok:true};
  };
  const generated = await api.generateLayout(client, 'quiet', {...custom, file:'old.json', stale:'bad'});
  await api.saveLayout(client, {...custom, file:'old.json', stale:'bad'}, true);
  await api.deleteLayout(client, custom.id);
  let failure, aborted;
  try { await api.generateLayout(async url => url === 'layouts/generate' ? {job:'bad'} : {state:'failed', error:'Bad contrast.'}, 'bad'); }
  catch (error) { failure = error.message; }
  abortWait = new AbortController();
  try { await api.generateLayout(client, 'abort', undefined, abortWait.signal); }
  catch (error) { aborted = error.name; }
  const realNow = Date.now;
  let reads = 0, timedout;
  Date.now = () => ++reads === 1 ? 0 : 420001;
  try { await api.generateLayout(client, 'timeout'); }
  catch (error) { timedout = error.message; }
  finally { Date.now = realNow; }
  console.log(JSON.stringify({builtins: api.BUILT_INS, checked, restored, attrs:api.variantAttrs(restored), defaults:api.variantAttrs(api.CLASSIC),
    clients:{generated, calls, waits, failure, aborted, timedout}}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT / 'ui', input=json.dumps(data),
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f'Node exited with code {result.returncode}\n{result.stderr}')
    return json.loads(result.stdout)


class ManifestTests(unittest.TestCase):
    def test_manifest_and_defaults(self):
        manifest = json.loads((ROOT / 'server/ui_manifest.json').read_text())
        self.assertEqual(manifest, layouts.MANIFEST)
        self.assertEqual(manifest['version'], 1)
        for spec in manifest['variants'].values():
            self.assertIn(spec['default'], spec['options'])
        self.assertIn(manifest['devices']['phone']['default'], manifest['devices']['phone']['options'])
        self.assertEqual(len(manifest['features']), 8)


@unittest.skipUnless(
    os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS') or browser_dependencies_available(),
    'Node and ui/npm ci are required (typescript not found)',
)
class BrowserManifestTests(unittest.TestCase):
    def test_builtins_match_the_browser_and_validate_as_custom_fixtures(self):
        actual = browser_check([GOOD])['builtins']
        self.assertEqual(actual, built_ins())
        for data in actual:
            with self.subTest(id=data['id']):
                self.assertNotIn('variants', data)
                self.assertNotIn('phone', data)
                self.assertNotIn('manifest', data)
                custom = {key: value for key, value in data.items() if key != 'themeName'}
                custom['id'] = 'example-' + data['id']
                self.assertEqual(layouts.validate(custom, '')['name'], data['name'])

    def test_browser_and_server_accept_the_same_v2_data(self):
        cases = [GOOD, changed(variants={'sessions': 'avatars', 'chat': 'bubbles'}, phone='keep', manifest=2)]
        for parts in ({'variants': None}, {'variants': []}, {'variants': {'sessions': 'unknown'}},
                      {'variants': {'url': 'x'}}, {'variants': {'sessions': []}}, {'phone': False},
                      {'phone': 'unknown'}, {'manifest': True}, {'manifest': 1.5}, {'manifest': 0},
                      {'manifest': '1'}, {'manifest': None}, {'manifest': 2**53},
                      {'style': {'radius': True}}, {'grid': {'columns': [{'main': 'terminal'}]}}):
            cases.append(changed(**parts))
        browser = browser_check(cases)
        for data, result in zip(cases, browser['checked']):
            with self.subTest(data=data):
                try:
                    clean = layouts.validate(data, 'fixture')
                except layouts.Bad as error:
                    self.assertEqual(result, {'error': str(error)})
                else:
                    self.assertEqual(result, {'layout': clean})
        self.assertEqual(browser['restored']['phone'], 'keep')
        self.assertEqual(browser['restored']['manifest'], 2)
        self.assertEqual(browser['attrs'], {'data-v-sessions': 'avatars', 'data-v-chat': 'bubbles'})
        self.assertEqual(browser['defaults'], {})

    def test_client_helpers_poll_and_abort_and_strip_scan_metadata(self):
        clients = browser_check([GOOD])['clients']
        self.assertEqual(clients['generated']['notes'], 'Calm.')
        self.assertEqual(clients['waits'], [1500, 1500, 1500, 1500])
        calls = clients['calls']
        self.assertEqual([call['url'] for call in calls], ['layouts/generate', 'layouts/generate?job=job-1',
                         'layouts/generate?job=job-1', 'layouts/save', 'layouts/delete', 'layouts/generate', 'layouts/generate'])
        self.assertNotIn('file', calls[0]['body']['base'])
        self.assertNotIn('stale', calls[3]['body']['layout'])
        self.assertTrue(calls[3]['body']['overwrite'])
        self.assertEqual(clients['failure'], 'Bad contrast.')
        self.assertEqual(clients['aborted'], 'AbortError')
        self.assertEqual(clients['timedout'], 'The layout job took too long.')
