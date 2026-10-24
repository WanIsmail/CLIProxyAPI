"""Isolated native-client heartbeat qualification; no real credentials or GPU use."""
import concurrent.futures
import datetime
import http.server
import json
import os
import pathlib
import socket
import subprocess
import threading
import time
import urllib.request

ROOT = pathlib.Path(os.environ.get('PROBE_WORKDIR', str(pathlib.Path(__file__).resolve().parents[2] / 'tmp')))
ROOT.mkdir(parents=True, exist_ok=True)
MODEL = 'dragon-qwen3.8-flash-next-nvfp4'
DELAY = float(os.environ.get('PROBE_DELAY', '330'))
REASONING = os.environ.get('PROBE_REASONING') == '1'
PROXY_BINARY = pathlib.Path(os.environ.get('PROBE_BINARY', str(ROOT / 'cli-proxy-api')))
CLIENT_BINARY = pathlib.Path(os.environ.get('PROBE_CLIENT_BINARY', '/home/gr33nl4dy/.local/bin/claude'))
requests = []


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        started = time.monotonic()
        record = {'at': datetime.datetime.now().astimezone().isoformat(),
                  'stream': body.get('stream'), 'model': body.get('model')}
        requests.append(record)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.flush()
        base = {'id': 'chatcmpl-fixture', 'object': 'chat.completion.chunk', 'model': MODEL}
        if REASONING:
            for _ in range(2):
                chunk = base | {'choices': [{'index': 0, 'delta': {'reasoning': 'step '}, 'finish_reason': None}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.flush()
                time.sleep(DELAY / 2)
        else:
            time.sleep(DELAY)
        chunks = [
            {'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 100, 'completion_tokens': 1, 'total_tokens': 101}},
        ]
        try:
            for chunk in chunks:
                self.wfile.write(('data: ' + json.dumps(base | chunk) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            record['completed_after_seconds'] = round(time.monotonic() - started, 3)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def unused_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def run_client(port, index):
    cwd = ROOT / f'client-{index}'
    cwd.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CLIPROXY_')) and k != 'CLAUDECODE'}
    env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{port}',
               ANTHROPIC_API_KEY='fixture-only',
               CLAUDE_CONFIG_DIR=str(cwd / 'config'),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
               DISABLE_AUTOUPDATER='1')
    started = time.monotonic()
    p = subprocess.run([
        str(CLIENT_BINARY),
        '-p', 'Reply OK.', '--model', MODEL, '--tools', '',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--no-session-persistence', '--disable-slash-commands', '--output-format', 'stream-json',
        '--verbose', '--include-partial-messages',
    ], cwd=cwd, env=env, capture_output=True, text=True, timeout=DELAY + 70)
    try:
        records = [json.loads(line) for line in p.stdout.splitlines() if line.startswith('{')]
        result = next((r for r in reversed(records) if r.get('type') == 'result'), {})
    except ValueError:
        records = []
        result = {}
    return {'client': index, 'exit_code': p.returncode,
            'elapsed_seconds': round(time.monotonic() - started, 3),
            'result_type': result.get('type'), 'subtype': result.get('subtype'),
            'is_error': result.get('is_error'), 'expected_result': result.get('result', '').strip() == 'OK',
            'thinking_deltas': sum(isinstance(r.get('event'), dict) and r['event'].get('delta', {}).get('type') == 'thinking_delta' for r in records),
            'stderr_bytes': len(p.stderr.encode())}


def main():
    import yaml
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    port = unused_port()
    config = ROOT / 'fixture-config.yaml'
    config.write_text(yaml.safe_dump({
        'host': '127.0.0.1', 'port': port, 'auth-dir': str(ROOT / 'fixture-auth'),
        'api-keys': ['fixture-only'], 'request-retry': 0, 'debug': False,
        'streaming': {'keepalive-seconds': 15, 'bootstrap-retries': 0},
        'remote-management': {'disable-control-panel': True},
        'openai-compatibility': [{'name': 'fixture',
            'base-url': f'http://127.0.0.1:{upstream.server_port}/v1',
            'api-key-entries': [{'api-key': 'fixture-upstream-only'}],
            'models': [{'name': MODEL, 'alias': MODEL}]}],
    }))
    config.chmod(0o600)
    proc = subprocess.Popen([str(PROXY_BINARY), '--config', str(config)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',
                                             headers={'Authorization': 'Bearer fixture-only'})
                with urllib.request.urlopen(req, timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('fixture proxy did not start')
        print(json.dumps({'phase': 'started', 'at': datetime.datetime.now().astimezone().isoformat(),
                          'delay_seconds': DELAY, 'clients': 2, 'gpu_requests': 0}), flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda i: run_client(port, i), [1, 2]))
        report = {'observed_at': datetime.datetime.now().astimezone().isoformat(),
                  'delay_seconds': DELAY, 'results': results, 'upstream_requests': requests,
                  'reasoning_probe': REASONING,
                  'passed': all(x['exit_code'] == 0 and x['expected_result'] and not x['is_error']
                                and (not REASONING or x['thinking_deltas'] == 2) for x in results)}
        (ROOT / f'native-heartbeat-{int(DELAY)}.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)
        return 0 if report['passed'] else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        upstream.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
