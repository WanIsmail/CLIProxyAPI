"""Run against an isolated launcher tree; all network/service/client calls are fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(os.environ['CLAUDEX_TEST_ROOT']).resolve()
FLASH = 'dragon-qwen3.8-flash-next-nvfp4'
ALIAS = 'claude-gateway-dragon-qwen3-8-flash-next-nvfp4'


class FlashCompactionTest(unittest.TestCase):
    def launch(self, *args, window=None):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as folder:
            base = Path(folder)
            config = base / 'config.yaml'
            config.write_text('api-keys:\n  - "fixture-only-not-a-credential"\n')
            output = base / 'args'
            env = dict(os.environ)
            for key in ['ANTHROPIC_MODEL', 'CLAUDE_CODE_AUTO_COMPACT_WINDOW',
                        'ZELLIJ', 'ZELLIJ_PANE_ID', 'CLIPROXY_CODEX_MODEL',
                        'CLIPROXY_CLAUDE_MODEL']:
                env.pop(key, None)
            env.update(PATH=str(ROOT / 'tests/fixtures/bin') + ':' + env['PATH'],
                       CLIPROXY_CONFIG=str(config), CLAUDEX_GATEWAY_CACHE=str(base / 'cache'),
                       CLAUDE_CONFIG_DIR=str(base / 'claude'),
                       CLIPROXY_TEST_CLAUDE_ARGS_FILE=str(output),
                       CLIPROXY_TEST_MODELS_JSON=json.dumps({'data': [{'id': FLASH}, {'id': 'gpt-6.1-sol'}, {'id': 'gpt-5.6-sol'}]}))
            if window is not None:
                env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] = window
            subprocess.run([str(ROOT / 'claude-codex'), *args], env=env, cwd=base,
                           check=True, stdout=subprocess.DEVNULL)
            argv = output.read_bytes().decode().rstrip('\0').split('\0')
            return argv, json.loads(argv[argv.index('--settings') + 1])

    def test_fresh_raw_and_alias(self):
        for model in [FLASH, ALIAS]:
            with self.subTest(model=model):
                args, settings = self.launch('--model', model)
                self.assertNotIn('--autocompact', args)
                self.assertEqual(settings['modelSettings'][model]['autoCompactWindow'], 180000)
                self.assertEqual(settings['autoCompactWindow'], 900000)

    def test_named_resume_and_continue_carry_model_policy(self):
        for args in [('--resume', 'Keywatcher'), ('--continue',),
                     ('--resume', '94e155fb-8913-42f7-9f0f-c70aee024513'),
                     ('--bg', '--model', ALIAS)]:
            with self.subTest(args=args):
                argv, settings = self.launch(*args)
                self.assertNotIn('--autocompact', argv)
                self.assertEqual(settings['modelSettings'][ALIAS]['autoCompactWindow'], 180000)
                self.assertEqual(settings['modelSettings'][FLASH]['autoCompactWindow'], 180000)

    def test_other_model_default_and_hook_integrity(self):
        _, settings = self.launch('--model', 'claude-gateway-gpt-6-1-sol')
        self.assertEqual(settings['autoCompactWindow'], 900000)
        original = json.loads((ROOT / 'claudex-settings.json').read_text())
        self.assertEqual(settings['hooks'], original['hooks'])
        self.assertEqual(settings['statusLine'], original['statusLine'])

    def test_explicit_cli_windows_pass_through(self):
        for flags in [('--autocompact', '150000'), ('--autocompact=150000',)]:
            argv, _ = self.launch('--model', ALIAS, *flags)
            for flag in flags:
                self.assertEqual(argv.count(flag), 1)
            self.assertNotIn('900000', argv)

    def test_explicit_environment_window_is_preserved(self):
        _, settings = self.launch('--model', ALIAS, window='150000')
        self.assertEqual(settings['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '150000')

    def test_prompt_terminator_does_not_override_policy(self):
        argv, settings = self.launch('--', '--autocompact', '900000')
        self.assertNotIn('--autocompact', argv[:argv.index('--')])
        self.assertEqual(settings['modelSettings'][ALIAS]['autoCompactWindow'], 180000)


if __name__ == '__main__':
    unittest.main()
