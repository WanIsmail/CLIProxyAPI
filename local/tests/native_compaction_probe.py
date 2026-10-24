"""Read the native compaction dialog in an isolated terminal; no inference."""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import time

import pexpect

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(os.environ.get('CLAUDEX_TEST_ROOT', '/home/gr33nl4dy/dev/cliproxyapi/runtime')).resolve()
CLIENT = Path(os.environ.get('PROBE_CLIENT_BINARY', '/home/gr33nl4dy/.local/bin/claude')).resolve()
WORK = ROOT / 'tmp/native-180k'
WORK.mkdir(parents=True, exist_ok=True)
(WORK / 'bin').mkdir(exist_ok=True)
for name in ['curl', 'systemctl', 'tput']:
    shutil.copy2(LAUNCHER / 'tests/fixtures/bin' / name, WORK / 'bin' / name)
claude = WORK / 'bin/claude'
if claude.is_symlink():
    claude.unlink()
claude.symlink_to(CLIENT)
(WORK / 'config').mkdir(exist_ok=True)
(WORK / 'config/.claude.json').write_text(json.dumps({
    'hasCompletedOnboarding': True, 'theme': 'dark',
    'bypassPermissionsModeAccepted': True,
    'projects': {str(WORK): {'hasTrustDialogAccepted': True}},
}))
config = WORK / 'fixture.yaml'
config.write_text('api-keys:\n  - "fixture-only-not-a-credential"\n')
config.chmod(0o600)
env = {k: v for k, v in os.environ.items()
       if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CLIPROXY_', 'ZELLIJ'))
       and k != 'CLAUDECODE'}
env.update(PATH=str(WORK / 'bin') + ':' + env['PATH'],
           CLIPROXY_CONFIG=str(config), CLAUDE_CONFIG_DIR=str(WORK / 'config'),
           CLAUDEX_GATEWAY_CACHE=str(WORK / 'gateway-models.json'),
           CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1',
           CLIPROXY_TEST_MODELS_JSON=json.dumps({'data': [{'id': 'dragon-qwen3.8-flash-next-nvfp4'}]}))
child = pexpect.spawn(str(LAUNCHER / 'claude-codex'),
                      ['--bare', '--model', 'claude-gateway-dragon-qwen3-8-flash-next-nvfp4',
                       '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'],
                      env=env, cwd=str(WORK), encoding='utf-8', dimensions=(42, 140), timeout=50)
child.logfile_read = (WORK / 'setup-ui.log').open('w')
report = {'native_binary': str(CLIENT), 'inference_requests': 0}
try:
    # Only this newly created terminal receives keystrokes.
    handled = set()
    screen = ''
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            screen += child.read_nonblocking(32768, timeout=1)
        except pexpect.TIMEOUT:
            continue
        clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', screen)
        compact = re.sub(r'\s+', '', clean).lower()
        if 'bypasspermissionson' in compact:
            break
        if 'no,disableexternalimports' in compact and 'external' not in handled:
            handled.add('external')
            time.sleep(.5)
            child.send('\r')
            screen = ''
        elif 'yes,iaccept' in compact and 'bypass' not in handled:
            handled.add('bypass')
            time.sleep(.5)
            child.send('\x1b[B\r')
            screen = ''
        elif 'yes,itrust' in compact and 'trust' not in handled:
            handled.add('trust')
            time.sleep(.5)
            child.send('\r')
            screen = ''
    else:
        report['setup_tail'] = clean[-1400:]
        raise pexpect.TIMEOUT('setup did not finish')
    child.send('/autocompact')
    child.send('\r')
    child.expect(r'180k(?:\x1b\[[0-?]*[ -/]*[@-~]|\s)*tokens')
    report.update(window=180000, dialog_verified=True,
                  observed_at=datetime.datetime.now().astimezone().isoformat())
    child.send('\x1b')
    child.sendcontrol('d')
    child.sendcontrol('d')
    try:
        child.expect(pexpect.EOF, timeout=5)
    except pexpect.TIMEOUT:
        pass
except (pexpect.TIMEOUT, pexpect.EOF):
    # No prompt/completion payloads are logged; the terminal contains only setup UI.
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', child.before or '')
    report.update(dialog_verified=False, setup_tail=report.get('setup_tail', clean[-1400:]))
finally:
    child.close(force=True)
    report['disposable_process_closed'] = not child.isalive()
    (WORK / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
raise SystemExit(0 if report.get('dialog_verified') else 1)
