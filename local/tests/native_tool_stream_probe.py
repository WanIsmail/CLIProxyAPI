"""Two native clients, synthetic upstream, one allowed read of fixture data.

Only timings, counts and verdicts are retained. No production credentials,
model calls, existing terminals, or services are used.
"""
import concurrent.futures
import datetime
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.request

import yaml

ROOT = Path(os.environ['PROBE_WORKDIR']).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
BINARY = Path(os.environ['PROBE_BINARY']).resolve()
MODEL = 'dragon-qwen3.8-flash-next-nvfp4'
FIXTURE = ROOT / 'fixture.txt'
FIXTURE.write_text('synthetic fixture\n')
completed = {}
request_count = {}


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        serialized = json.dumps(body)
        client = 1 if 'fixture-client-1' in serialized else 2
        request_count[client] = request_count.get(client, 0) + 1
        first = request_count[client] == 1
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()

        def send(delta, finish=None):
            chunk = {'id': f'fixture-{client}', 'model': MODEL,
                     'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
            if finish:
                chunk['usage'] = {'prompt_tokens': 100, 'completion_tokens': 16}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.flush()

        if first:
            args = json.dumps({'file_path': str(FIXTURE), 'limit': 1})
            split = len(args) // 2
            send({'tool_calls': [{'index': 0, 'id': 'call_fixture', 'type': 'function',
                                 'function': {'name': 'Read', 'arguments': args[:split]}}]})
            time.sleep(2)
            send({'tool_calls': [{'index': 0, 'function': {'arguments': args[split:]}}]})
            time.sleep(2)
            completed[client] = time.monotonic()
            send({}, 'tool_calls')
        else:
            send({'content': 'OK'})
            send({}, 'stop')
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()
        self.close_connection = True


def run_client(port, index):
    work = ROOT / f'client-{index}'
    work.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'CLIPROXY_', 'ZELLIJ'))
           and k != 'CLAUDECODE'}
    env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{port}',
               ANTHROPIC_API_KEY='fixture-only', CLAUDE_CONFIG_DIR=str(work / 'config'),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1')
    started = time.monotonic()
    report = {'client': index, 'argument_deltas': 0, 'first_argument_at': None}
    process = subprocess.Popen([
        '/home/gr33nl4dy/.local/bin/claude', '--bare', '-p', f'fixture-client-{index}',
        '--model', MODEL, '--tools', 'Read', '--allowedTools', 'Read',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        '--no-session-persistence', '--max-turns', '2',
        '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
    ], cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def consume():
        for line in process.stdout:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            event = record.get('event', {})
            if isinstance(event, dict) and event.get('delta', {}).get('type') == 'input_json_delta':
                report['argument_deltas'] += 1
                if report['first_argument_at'] is None:
                    report['first_argument_at'] = time.monotonic()
            if record.get('type') == 'result':
                report['result_ok'] = record.get('result', '').strip() == 'OK' and not record.get('is_error')

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        process.stdout.close()
    first = report.pop('first_argument_at')
    report.update(exit_code=process.returncode,
                  argument_before_finish=first is not None and first < completed.get(index, 0),
                  elapsed_seconds=round(time.monotonic() - started, 3))
    return report


def main():
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    config = ROOT / 'fixture.yaml'
    config.write_text(yaml.safe_dump({
        'host': '127.0.0.1', 'port': port, 'auth-dir': str(ROOT / 'auth'),
        'api-keys': ['fixture-only'], 'request-retry': 0,
        'streaming': {'keepalive-seconds': 1, 'bootstrap-retries': 0},
        'remote-management': {'disable-control-panel': True},
        'openai-compatibility': [{'name': 'fixture', 'base-url': f'http://127.0.0.1:{upstream.server_port}/v1',
            'api-key-entries': [{'api-key': 'fixture-upstream-only'}],
            'models': [{'name': MODEL, 'alias': MODEL}]}],
    }))
    config.chmod(0o600)
    proxy = subprocess.Popen([str(BINARY), '--config', str(config)], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',
                                                 headers={'Authorization': 'Bearer fixture-only'})
                with urllib.request.urlopen(request, timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('fixture proxy did not start')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda i: run_client(port, i), [1, 2]))
        report = {'observed_at': datetime.datetime.now().astimezone().isoformat(),
                  'results': results, 'gpu_requests': 0, 'upstream_requests': request_count,
                  'passed': all(r['argument_before_finish'] and r.get('result_ok') and r['exit_code'] == 0 for r in results)}
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()
        upstream.shutdown()
        upstream.server_close()
    report['disposable_processes_closed'] = proxy.poll() is not None
    (ROOT / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
