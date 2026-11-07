"""Loopback operator UI. No remote binding, file browser, or shell endpoint."""
import json
import copy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
import webbrowser

from .store import Store, NoriError, digest, atomic_write, canonical
from .workflow import WorkflowController
from .core import validate_spec
from .demo import example_spec
from .adapter import MockAdapter
from .tool_adapters import CodexAdapter, ClaudeCodeAdapter
from .quality_gate import QualityGate


class WebApp:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(24)
        self.jobs = {}
        self.lock = threading.RLock()
        self.closing = threading.Event()

    def draft_path(self, draft_id):
        if not isinstance(draft_id, str) or not re.fullmatch(r'draft-[a-f0-9]{24}', draft_id):
            raise NoriError('Invalid draft ID')
        folder = self.root / '.drafts'
        if folder.is_symlink():
            raise NoriError('Draft symlink refused')
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / (draft_id + '.json')
        if path.is_symlink():
            raise NoriError('Draft symlink refused')
        return path

    def read_draft(self, draft_id):
        with self.lock:
            return json.loads(self.draft_path(draft_id).read_text())

    def list_drafts(self):
        folder = self.root / '.drafts'
        if not folder.exists(): return []
        if folder.is_symlink(): raise NoriError('Draft symlink refused')
        return sorted([self.read_draft(p.stem) for p in folder.glob('draft-*.json')],
                      key=lambda d: d['updated_at'], reverse=True)

    def save_draft(self, body):
        text = body.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 50000:
            raise NoriError('需求内容不能为空，最多 50000 字符')
        with self.lock:
            now = datetime.now(timezone.utc).isoformat()
            draft_id = body.get('id') or 'draft-' + uuid.uuid4().hex[:24]
            path = self.draft_path(draft_id)
            if body.get('id'):
                draft = self.read_draft(draft_id)
            else:
                draft = {'id': draft_id, 'title': text.strip().splitlines()[0][:48],
                         'original_requirement': text, 'created_at': now,
                         'status': 'DRAFT', 'messages': []}
            draft['messages'].append({'role': 'user', 'text': text, 'created_at': now})
            draft['updated_at'] = now
            atomic_write(path, canonical(draft))
            return draft

    def project(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', name):
            raise NoriError('项目 ID 只能包含字母、数字、下划线或连字符（最多 64 字符）')
        path = self.root / name
        if path.is_symlink() or any((path / x).is_symlink() for x in ('control', 'work')):
            raise NoriError('Project symlinks are forbidden')
        return path

    def snapshot(self, name):
        path = self.project(name)
        if not (path / 'control' / 'state.json').is_file():
            raise NoriError('项目不存在 / Project not found')
        with Store(path / 'control', lock_timeout=2) as store:
            if store.state['workspace'] != str(path / 'work'):
                raise NoriError('Project workspace is outside app-owned directory')
            c = WorkflowController(store, path / 'work')
            state = c.state
            result = {'state': state, 'ready': c.ready(), 'approval_digest': digest(state['spec'])}
        result['job'] = self.job_status(name)
        return result

    def job_status(self, name):
        with self.lock:
            job = self.jobs.get(name)
            return {k: v for k, v in job.items() if k not in ('thread', 'stop')} if job else {'running': False}

    def adapter(self, c):
        state = c.state
        active = state['runs'].get(state['lease'], {})
        name = active.get('adapter')
        if not name or name == 'unselected':
            name = state['spec'].get('executor')
        classes = {'mock': MockAdapter, 'codex': CodexAdapter, 'claude': ClaudeCodeAdapter}
        if name not in classes:
            raise NoriError('请在计划 executor 中选择 mock、codex 或 claude')
        return classes[name](c.store.home, c.root)

    def command(self, name, action, body, *, background=False):
        path = self.project(name)
        if action == 'create':
            spec = body.get('spec', example_spec())
            if not isinstance(spec, dict):
                raise NoriError('Spec must be a JSON object')
            validate_spec(spec)
            path.mkdir(mode=0o700, exist_ok=False)
            with Store(path / 'control') as store:
                WorkflowController(store, path / 'work').initialize(spec)
            return self.snapshot(name)
        if action == 'run':
            return self.start_job(name, body)
        if not (path / 'control' / 'state.json').exists():
            raise NoriError('Project not found')
        with Store(path / 'control', lock_timeout=2) as store:
            if store.state['workspace'] != str(path / 'work'):
                raise NoriError('Project workspace is outside app-owned directory')
            c = WorkflowController(store, path / 'work')
            rev = c.state['revision']
            if not background and body.get('revision') != rev:
                raise NoriError('Stale expected_state_revision; refresh and retry')
            cid = body.get('command_id') or str(uuid.uuid4())
            reason = body.get('reason', '')
            if action == 'approve': result = c.approve(cid, rev, body['digest'])
            elif action == 'step':
                adapter = self.adapter(c)
                if not c.state['lease']:
                    probe = adapter.probe()
                    if probe['status'] != 'available':
                        raise NoriError(json.dumps(probe, ensure_ascii=False))
                result = c.advance(adapter, cid)
            elif action == 'pause': result = c.pause(cid, rev, self.adapter(c))
            elif action == 'resume': result = c.resume(cid, rev)
            elif action == 'cancel': result = c.cancel(cid, rev, self.adapter(c), reason)
            elif action == 'reconcile':
                result = c.reconcile(cid, rev, c.state['lease'], self.adapter(c)) if c.state['lease'] else {'status': 'idle'}
            elif action == 'finalize': result = c.close_project(cid, rev)
            elif action == 'audit': result = c.audit(cid, rev)
            elif action == 'repair-example':
                spec = copy.deepcopy(c.state['spec'])
                task = spec.get('tasks', {}).get('A', {})
                if spec.get('executor') != 'mock' or task.get('mock_mode') != 'incomplete':
                    raise NoriError('仅适用于 A 任务漏项模拟示例')
                task['mock_mode'] = 'complete'
                result = c.propose_revision(cid, rev, 'repair-' + uuid.uuid4().hex[:12], spec, '补齐模拟任务漏项；只修改 mock_mode，要求和验收标准不变')
            elif action == 'propose': result = c.propose_revision(cid, rev, body['proposal'], body['spec'], reason)
            elif action == 'accept-revision': result = c.accept_revision(cid, rev, body['proposal'], body['digest'])
            elif action == 'rework': result = c.authorize_rework(cid, rev, body['task'], reason)
            elif action == 'retry-verification': result = c.retry_verification(cid, rev, body['task'])
            elif action in ('manual-binding', 'manual'):
                mapping = c.state['spec']['tasks'][body['task']]['checks'] if body.get('task') else c.state['spec']['final_checks']
                rid = body['requirement']
                desc = mapping[rid]
                if not isinstance(desc, dict) or desc.get('kind') != 'manual':
                    raise NoriError('Selected check is not manual')
                if action == 'manual-binding': result = {'binding': QualityGate(c.root, c.state['spec']).binding(rid, desc)}
                else: result = c.record_manual(cid, rev, rid, desc, body['binding'], body['verdict'], reason)
            else: raise NoriError('Unsupported action')
            response = {'result': result, 'revision': c.state['revision']}
        # Only a successfully committed pause/cancel may stop monitoring.
        # Acquire the job mutex after releasing Store to preserve lock order.
        if action in ('pause', 'cancel'):
            with self.lock:
                if name in self.jobs:
                    self.jobs[name]['stop'].set()
        return response

    def start_job(self, name, body):
        steps = body.get('max_steps', 30)
        if type(steps) is not int or not 1 <= steps <= 1000:
            raise NoriError('max_steps must be 1..1000')
        with self.lock:
            if self.jobs.get(name, {}).get('running'):
                raise NoriError('此项目已经在运行 / Already running')
            snap = self.snapshot(name)
            if body.get('revision') != snap['state']['revision']:
                raise NoriError('Stale expected_state_revision; refresh and retry')
            job = {'running': True, 'steps': 0, 'result': None, 'error': None, 'stop': threading.Event()}
            self.jobs[name] = job
            def worker():
                try:
                    for _ in range(steps):
                        if job['stop'].is_set() or self.closing.is_set(): break
                        result = self.command(name, 'step', {}, background=True)
                        with self.lock:
                            job['steps'] += 1
                            job['result'] = result
                        if result['result'].get('status') in ('blocked', 'COMPLETED', 'AWAITING_DECISION', 'CANCELLED', 'CLOSED_PARTIAL'): break
                        job['stop'].wait(.15)
                except Exception as exc:
                    with self.lock: job['error'] = str(exc)
                finally:
                    with self.lock: job['running'] = False
            job['thread'] = threading.Thread(target=worker, daemon=True)
            job['thread'].start()
            return {'started': True}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        self.app.closing.set()
        super().server_close()


def create_server(data_dir, port=0):
    app = WebApp(data_dir)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def send(self, status, data, html=False):
            raw = data.encode('utf-8') if html else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'nonce-" + app.nonce + "'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def allowed(self, mutate=False):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != origin.removeprefix('http://'):
                self.send(403, {'error': 'Invalid Host'}); return False
            if self.headers.get('Origin') not in (None, origin):
                self.send(403, {'error': 'Invalid Origin'}); return False
            if mutate and self.headers.get('Origin') != origin:
                self.send(403, {'error': 'Same-origin request required'}); return False
            if (mutate or self.path.startswith('/api/')) and not secrets.compare_digest(self.headers.get('X-Nori-Token', ''), app.token):
                self.send(403, {'error': 'Invalid CSRF token'}); return False
            return True

        def do_GET(self):
            if not self.allowed(): return
            try:
                if self.path == '/': self.send(200, HTML.replace('__NONCE__', app.nonce), html=True)
                elif self.path == '/api/projects':
                    names = [p.name for p in sorted(app.root.iterdir()) if p.is_dir() and not p.is_symlink() and (p / 'control' / 'state.json').is_file()]
                    self.send(200, {'projects': names})
                elif self.path == '/api/drafts': self.send(200, {'drafts': app.list_drafts()})
                elif self.path.startswith('/api/draft/'): self.send(200, app.read_draft(self.path[len('/api/draft/'):]))
                elif self.path == '/api/info': self.send(200, {'data_dir': str(app.root), 'workspace_policy': 'app-owned-only'})
                elif self.path == '/api/example': self.send(200, example_spec())
                elif self.path.startswith('/api/project/'): self.send(200, app.snapshot(self.path[len('/api/project/'):]))
                else: self.send(404, {'error': 'Not found'})
            except Exception as exc: self.send(400, {'error': str(exc)})

        def do_POST(self):
            if not self.allowed(mutate=True): return
            try:
                if self.headers.get('Content-Type') != 'application/json': raise NoriError('JSON required')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2 * 1024 * 1024: raise NoriError('Request must be 1..2097152 bytes')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict): raise NoriError('JSON object required')
                if self.path == '/api/draft':
                    self.send(200, app.save_draft(body)); return
                if self.path != '/api/command': self.send(404, {'error': 'Not found'}); return
                self.send(200, app.command(body['project'], body['action'], body))
            except Exception as exc: self.send(400, {'error': str(exc)})
    server = LocalServer(('127.0.0.1', port), Handler)
    server.app = app
    return server


def serve(data_dir, port=8765, open_browser=True):
    server = create_server(data_dir, port)
    url = f'http://127.0.0.1:{server.server_port}/#token={server.app.token}'
    print(f'Nori 网页工作台：{url}', flush=True)
    if open_browser: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nori 工作台</title>
<style>
:root{--bg:#fff;--panel:#f8f9fb;--line:#e9ebef;--muted:#7a808b;--ink:#242833;--accent:#547064;--soft:#edf4ef}*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg)}button,input,textarea,select{font:inherit}button{cursor:pointer;border:0;background:none;color:inherit;border-radius:8px;padding:9px 12px;transition:background .15s}button:hover{background:#edf0f3}button:disabled{opacity:.38;cursor:not-allowed}button.primary{background:#293d35;color:white}button.primary:hover{background:#3c574b}input,textarea,select{border:1px solid #dce0e6;border-radius:8px;padding:9px;background:white;color:inherit;max-width:100%}textarea{resize:vertical;width:100%}button:focus-visible,input:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid #789d89;outline-offset:3px}h1,h2,h3,p{margin-top:0}h2{font-size:18px;font-weight:600}h3{font-size:14px}p{line-height:1.75}small,.muted{color:var(--muted);font-size:12px;line-height:1.7}.app{display:grid;grid-template-columns:220px minmax(420px,1fr) 296px;height:100dvh;min-height:600px}.app.no-context{grid-template-columns:220px minmax(420px,1fr)}.app.no-context .right{display:none}.sidebar{background:#f7f8fa;border-right:1px solid var(--line);padding:24px 14px;display:flex;flex-direction:column;min-width:0}.brand{display:flex;align-items:center;gap:9px;padding:0 10px 28px;font-size:22px;letter-spacing:-.8px;font-weight:650}.logo{background:#273d33;color:#fff;width:29px;height:29px;border-radius:10px;display:grid;place-items:center;font-size:20px}.new{border:1px solid #dce1e6;background:white;width:100%;text-align:left;margin-bottom:24px}.navlabel{font-size:11px;color:#9297a0;padding:10px;letter-spacing:1px}.navlist{overflow:auto}.navitem{display:block;width:100%;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:3px 0;font-size:13px}.navitem.active{background:#e9eeeb;color:#2e5140}.sidebottom{margin-top:auto;border-top:1px solid var(--line);padding:18px 8px 0;line-height:1.7}.dot{display:inline-block;width:6px;height:6px;background:#6d947e;border-radius:100%;margin-right:6px}.center{display:flex;flex-direction:column;min-width:0;min-height:0}.topbar{height:65px;flex-shrink:0;display:flex;align-items:center;justify-content:space-between;padding:0 30px;border-bottom:1px solid var(--line)}.crumb{display:flex;gap:12px;align-items:center;font-size:13px}.pill{border:1px solid #dfe6e1;border-radius:20px;color:#5e7769;background:#f7fbf8;font-size:11px;padding:4px 9px;white-space:nowrap}.scroll{overflow:auto;flex:1;padding:34px 42px 20px}.welcome{max-width:650px;margin:65px auto 40px}.eyebrow{color:#8b958e;letter-spacing:2px;font-size:11px;margin-bottom:16px}.welcome h1{font-size:34px;font-weight:550;line-height:1.4;letter-spacing:-1px;margin-bottom:16px}.welcome>p{color:#8a8f98;max-width:480px}.cards{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:28px}.starter{text-align:left;border:1px solid var(--line);border-radius:12px;padding:18px;background:#fff}.starter strong{display:block;font-weight:550;margin:12px 0 6px}.starter span{display:block;color:#9096a0;font-size:12px;line-height:1.6}.icon{color:#718779;font-size:19px}.message{max-width:720px;margin:0 auto 25px;line-height:1.8}.message.user{border-left:2px solid #d2ddd6;padding:8px 0 8px 18px}.messagehead{font-size:11px;color:#8e949e;margin-bottom:8px;display:flex;gap:10px;align-items:center}.messagebody{white-space:pre-wrap;overflow-wrap:anywhere}.reply{background:#f6f8f7;border:1px solid #e7ece9;border-radius:12px;padding:18px}.composerwrap{padding:16px 36px 24px;flex-shrink:0}.composer{max-width:780px;margin:auto;border:1px solid #dce2df;border-radius:16px;padding:14px 16px;box-shadow:0 4px 18px #17282006;background:white}.composer textarea{border:0;outline:0;min-height:70px;max-height:180px;padding:3px;resize:none;font-size:14px;line-height:1.65}.composerbar{display:flex;align-items:center;justify-content:space-between;margin-top:7px}.send{border-radius:9px;background:#293d35;color:white;font-size:16px;padding:7px 12px}.composernote{text-align:center;font-size:10px;color:#9ba0a8;margin-top:10px}.right{border-left:1px solid var(--line);background:#fcfcfd;padding:25px 22px;overflow:auto}.right h2{font-size:12px;color:#7c838f;letter-spacing:.7px;margin:0 0 20px}.rsection{padding:22px 0;border-top:1px solid var(--line)}.empty{font-size:12px;color:#969da6;line-height:1.9}.progressrow{display:flex;gap:10px;margin:17px 0;font-size:12px;align-items:center}.circle{width:19px;height:19px;border:1px solid #dfe4e0;border-radius:50%;font-size:10px;display:grid;place-items:center;color:#a2aaa4}.circle.done{background:#e6f0e9;color:#54715e;border-color:#d6e6da}.circle.now{border-color:#7f9d88;color:#496750}.file{font:12px ui-monospace,monospace;padding:8px 10px;border:1px solid var(--line);border-radius:7px;margin:6px 0;background:white}.tip{font-size:12px;background:#f0f4f1;border-radius:10px;padding:13px;color:#6e8074;line-height:1.8}.panel{border:1px solid var(--line);border-radius:12px;padding:20px;margin:0 auto 18px;max-width:720px;background:white}.panel h2{font-size:15px}.planrow{border-top:1px solid var(--line);padding:14px 0}.planrow:last-child{padding-bottom:0}.toolbar{display:flex;gap:6px;flex-wrap:wrap}.toolbar button{font-size:12px;border:1px solid #e0e5e1}.result{font-size:13px;color:#50745d;padding:12px 0}.error{background:#fff2ef;color:#a64332;font-size:12px;padding:12px 16px;white-space:pre-wrap;margin:0 20px;border-radius:8px}#notice:empty{display:none}details{font-size:12px;line-height:1.8;margin:10px 0}summary{cursor:pointer;color:#7c838c}pre{font:11px ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:350px;overflow:auto;background:#f8f9fa;padding:12px;border-radius:8px}label{display:block;font-size:12px;margin:12px 0}label input{display:block;width:100%;margin-top:6px}[hidden]{display:none!important}.advanced{max-width:720px;margin:auto}.footerlabel{font-size:10px;color:#b1b5bc;margin-top:24px}.statusline{font-size:12px;color:#7b838e;margin-bottom:13px}#conversationTitle{max-width:330px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}@media(min-width:1500px){.scroll{padding-left:70px;padding-right:70px}}@media(max-width:1120px){.app{grid-template-columns:190px minmax(380px,1fr)}.right{display:none}}@media(max-width:700px){.app,.app.no-context{grid-template-columns:1fr}.sidebar{display:none}.scroll{padding:20px}.composerwrap{padding:12px 18px}.topbar{padding:0 18px}.welcome{margin-top:30px}.welcome h1{font-size:28px}.cards{grid-template-columns:1fr}.topbar .muted{display:none}}

/* Project-manager workspace: persisted records, not a simulated live feed. */
.app{grid-template-columns:210px minmax(500px,1fr) 290px;background:#f5f6f8}.sidebar{background:#f7f8fa}.center{background:#f5f6f8}.topbar{background:#fff;height:61px}.scroll{padding:26px;}.right{background:#fff}.brand{padding-bottom:26px}.welcome{margin:0;max-width:none}.welcome h1{font-size:25px;letter-spacing:-.6px}.panel,.advanced{max-width:none}.panel{border-color:#e1e5eb;border-radius:10px;padding:20px;box-shadow:0 2px 3px #18203002}.dashboardhead{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin:0 0 24px}.dashboardhead h1{font-size:25px;font-weight:600;margin:0 0 7px}.sectionhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.sectionhead h2{margin:0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}.metric{border:1px solid #e1e5eb;background:white;border-radius:10px;padding:18px}.metric span{color:#8a919c;font-size:11px}.metric strong{font-size:27px;display:block;margin:7px 0 4px;font-weight:550;letter-spacing:-1px}.metric small{font-size:10px}.pm-table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}.pm-table th{color:#8a919c;font-size:10px;font-weight:500;padding:12px 9px;background:#f8f9fb;border-bottom:1px solid #e7ebef;white-space:nowrap}.pm-table td{padding:16px 9px;border-bottom:1px solid #edf0f3;vertical-align:top;line-height:1.65}.pm-table tr:last-child td{border-bottom:0}.pm-table .smallid{font-size:10px;color:#9ca3ad;display:block}.pm-table button{text-align:left;padding:0;font-weight:550;color:#344b69}.tablewrap{overflow:auto}.worktitle{font-weight:550;max-width:230px}.badge{font-size:10px;border-radius:5px;padding:4px 7px;display:inline-block;background:#f0f2f5;color:#747d8b;white-space:nowrap}.badge.green{background:#edf5ee;color:#52775b}.badge.red{background:#fff1ec;color:#a76a48}.emptyboard{text-align:center;padding:42px 20px;color:#8e97a3;border:1px dashed #dce2e8;border-radius:8px;background:#fcfdfe;line-height:1.9;font-size:12px}.previewtag{font-size:10px;color:#8e96a1}.requirementtext{font-size:14px;line-height:1.95;white-space:pre-wrap;overflow-wrap:anywhere;color:#3b4654}.requirementmeta{font-size:11px;color:#8c95a1;margin:12px 0 22px}.doclabel{font-size:10px;color:#99a1ad;letter-spacing:1px;margin-bottom:10px}.composerwrap{padding:0;flex-shrink:initial}.composer{max-width:none;box-shadow:none;border:0;border-radius:0;padding:0}.composer textarea{border:1px solid #dfe5eb;border-radius:8px;padding:12px;min-height:110px;resize:vertical}.composernote{text-align:left}.send{font-size:12px}.message{max-width:none;margin:0}.message.user{border:0;padding:0}.rsection h2{font-size:11px;margin-bottom:14px}.queuerow{font-size:12px;border-left:2px solid #d9e3db;padding:8px 10px;background:#f7faf8;margin:10px 0;line-height:1.8}.riskrow{font-size:12px;border-left:2px solid #e7c8b3;padding:8px 10px;background:#fffaf6;margin:10px 0;line-height:1.8}.timeline{border-left:1px solid #e1e6ec;margin:20px 6px;padding:0 0 0 20px}.timeline article{margin:15px 0}.linkbtn{font-size:11px;color:#7d8b9a}.dashboardtabs{display:flex;gap:20px;border-bottom:1px solid #dfe4ea;margin-bottom:22px;font-size:12px;color:#8a93a0}.dashboardtabs span{padding:0 0 12px}.dashboardtabs .selected{border-bottom:2px solid #557564;color:#3d5a49}.pill{border-radius:6px;font-size:10px}.progressrow{margin:12px 0}.contextmini{font-size:10px;color:#9aa2ae;line-height:1.8}.app.no-context{grid-template-columns:210px minmax(500px,1fr)}
@media(max-width:1120px){.app,.app.no-context{grid-template-columns:190px minmax(380px,1fr)}}@media(max-width:700px){.app,.app.no-context{grid-template-columns:1fr}.scroll{padding:18px}.dashboardhead{display:block}.metrics{gap:6px}.metric{padding:12px}.metric strong{font-size:22px}}

.mindboard{padding:0;overflow:hidden}.mindheader{padding:18px 20px;border-bottom:1px solid #e5eaf0;background:#fff;display:flex;justify-content:space-between;align-items:center;gap:10px}.mindheader h2{margin:0 0 6px}.prototypewarning{font-size:10px;color:#9a7644;background:#fff8e9;border:1px solid #f0e2c1;padding:6px 10px;border-radius:6px;display:inline-block}.graph-toolbar{display:flex;gap:3px;align-items:center;flex-wrap:wrap}.graph-toolbar button{padding:6px 9px;font-size:11px;border:1px solid #e1e6ed;background:#fff}.mindviewport{overflow:auto;min-height:500px;background-color:#fcfdfd;background-image:radial-gradient(#dce3e8 .8px,transparent .8px);background-size:18px 18px;padding:28px 22px}.mindtree{display:flex;align-items:center;gap:0;min-width:760px;transform-origin:top left;transition:transform .16s}.mindroot{flex:0 0 172px;position:relative;margin-right:32px}.mindroot:after{content:'';position:absolute;width:32px;height:1px;background:#aab9c7;right:-32px;top:50%}.mindbranches{display:flex;flex-direction:column;gap:20px;position:relative;padding-left:28px;border-left:1px solid #b8c5d0}.mindbranch{display:flex;align-items:center;position:relative;min-height:80px;gap:28px}.mindbranch:before{content:'';position:absolute;width:28px;height:1px;background:#b8c5d0;left:-28px;top:50%}.mind-node{display:block;text-align:left;padding:12px 13px;width:183px;border:1px solid #d8e2ea;border-left:3px solid var(--branch,#9eafbd);border-radius:8px;background:white;box-shadow:0 2px 5px #24374805;line-height:1.45;position:relative;flex-shrink:0}.mind-node:hover{background:#f8fbfd;border-color:#9eb6c9}.mind-node.selected{outline:2px solid #a0b7c9;outline-offset:2px}.mind-node strong{font-size:12px;font-weight:550;display:block}.mind-node .nodemeta{display:block;font-size:9px;color:#929da9;margin-top:6px}.mind-node .nodestatus{font-size:9px;color:#6b8775;background:#f0f5f1;display:inline-block;padding:2px 5px;border-radius:4px;margin-top:7px}.mind-node.risk .nodestatus{background:#fff0e5;color:#aa754b}.mind-node.root-node{background:#314c43;color:#fff;border-color:#314c43;width:172px;padding:18px}.root-node .nodemeta{color:#bfd0c7}.root-node .nodestatus{background:#ffffff15;color:#d8e5dd}.mindleaves{padding-left:22px;border-left:1px solid #c5d0da;display:flex;flex-direction:column;gap:12px;position:relative}.mindleaves:before{content:'';position:absolute;width:28px;height:1px;left:-28px;top:50%;background:#b8c5d0}.leafwrap{position:relative}.leafwrap:before{content:'';position:absolute;left:-22px;top:50%;height:1px;width:22px;background:#c5d0da}.mindleaves .mind-node{width:176px;padding:9px 11px}.graphlegend{padding:12px 20px;border-top:1px solid #e8edf2;font-size:10px;color:#85929e;display:flex;gap:20px;flex-wrap:wrap;background:white}.dep-label{font-size:9px;color:#929ca7;margin-top:6px;border-top:1px dashed #b5c1cd;padding-top:5px;display:block}.node-detail{font-size:12px;line-height:1.9}.node-detail h3{font-size:15px;line-height:1.6;margin:10px 0}.node-detail .detailrow{padding:10px 0;border-bottom:1px solid #edf0f3}.node-detail small{display:block;font-size:10px}.prototype-note{font-size:11px;line-height:1.8;color:#8a795e}.collapse-toggle{font-size:9px;margin:5px;color:#8f9ba7;padding:2px 6px}.right{padding:23px 18px}.pm-table{font-size:11px}.dashboardhead{margin-bottom:16px}.dashboardtabs{margin-bottom:16px}.dashboardhead h1{font-size:23px}.metrics{margin:12px 0}.metric{padding:12px 16px}.metric strong{font-size:23px}.scroll{padding:22px}.mindboard .panel{border:0}.prototype-hidden{display:none!important}
.mindleaves .nodemeta{display:none}.mindleaves .mind-node{padding:8px 11px}.mindleaves .mind-node strong{font-size:11px}.mindleaves .nodestatus{margin-top:4px}.mindviewport{min-height:460px}.mindbranch{min-height:70px}.prototypegoal{font-size:11px;color:#87949e;margin:9px 0 0}
</style></head><body><div class="app">
<aside class="sidebar"><div class="brand"><span class="logo">n</span>nori<span class="pill" style="margin-left:auto">预览</span></div><button class="new" id="newChat">＋ &nbsp; 发起需求</button><div class="navlabel">原始需求</div><div id="draftList" class="navlist"><small style="padding:10px">还没有需求记录</small></div><div class="navlabel" style="margin-top:16px">执行项目</div><div id="projectList" class="navlist"></div><div class="sidebottom"><small><span class="dot"></span>本机工作区</small><br><small>项目经理工作台 · 本地预览</small><div class="footerlabel">NORI / LOCAL WORKSPACE</div></div></aside>
<main class="center"><header class="topbar"><div class="crumb"><span style="color:#a6adb5">⌘</span><span id="conversationTitle">需求中心</span><span class="pill" id="modeLabel">需求草稿</span></div><div style="display:flex;align-items:center;gap:12px"><span class="muted">需求 / 工作包 / 监督 / 验收</span><button id="toggleContext" aria-label="显示或隐藏任务概览" title="任务概览">◫</button></div></header><div id="notice" role="alert"></div><div class="scroll" id="feed">
<div class="dashboardhead"><div><div class="eyebrow">PROJECT CONTROL CENTER</div><h1>需求与任务图谱</h1><p class="muted" id="dashboardSubtitle">从一个大目标展开工作包，把分工、依赖和验收放在同一张图里。</p></div><div class="toolbar"><button id="showPrototype">原型脑图</button><button id="showDemo">体验模拟项目</button><button class="primary" id="writeIdea">＋ 发起需求</button></div></div>
<div class="dashboardtabs"><span class="selected">概览</span><span>需求 · 工作包 · 风险与证据</span></div>
<section id="prototypeBoard" class="panel mindboard"><div class="mindheader"><div><h2>Nori 自我迭代</h2><p class="prototypegoal">第一个目标：用 Nori 内核完善 Nori Web 版</p><span class="prototypewarning">交互原型示例 · 不代表真实执行 · 不会自动修改自身</span></div><div class="graph-toolbar"><button id="graphZoomOut" aria-label="缩小脑图">−</button><span id="zoomLabel" class="muted">100%</span><button id="graphZoomIn" aria-label="放大脑图">＋</button><button id="graphFit">适配</button><button id="collapseGraph">收起子任务</button></div></div><div class="mindviewport" id="prototypeViewport"><div id="prototypeGraph" class="mindtree"></div></div><div class="graphlegend"><span>── 实线：需求分解（不是执行顺序）</span><span>┄ 依赖：单独以文字标签说明</span><span>已有框架：串行调度 / 状态持久化 / 独立验收；本图分配与状态是示例，真实自我迭代未启动。</span></div></section><div id="welcome"><details><summary>本地需求清单（已保存记录）</summary><section class="panel"><div class="sectionhead"><h2>需求清单</h2><span id="requirementCount" class="muted">正在加载本地记录</span></div><div class="tablewrap"><table class="pm-table"><thead><tr><th>需求 / 目标</th><th>状态</th><th>最近更新</th><th>操作</th></tr></thead><tbody id="requirementRows"></tbody></table></div><div id="requirementsEmpty" class="emptyboard" hidden>还没有需求。<br>点击“发起需求”，保存目标、交付物和约束。<br>自动拆解与指派 Agent 尚未接入，不会隐式启动任务。</div></section></details></div>
<div id="messages" aria-live="polite"></div>
<section class="panel" id="requirementEditor" hidden><div class="sectionhead"><h2 id="editorTitle">发起需求</h2><button id="closeEditor" class="linkbtn">收起</button></div><p class="muted">填写背景、期望成果、范围和验收条件。保存后可持续补充，原始需求不会被覆盖。</p><div class="composerwrap"><div class="composer"><textarea id="prompt" aria-label="任务描述" placeholder="我们希望完成什么？交付什么？哪些事情不在范围内？怎样算完成？"></textarea><div class="composerbar"><span class="muted">原始需求 · 持久保存在本地</span><button class="send" id="saveDraft" aria-label="保存需求">保存需求</button></div></div><div class="composernote">当前输入只保存原始需求，尚未连接自动规划，不会自动开始执行。</div></div></section>
<section class="panel" id="unplannedBoard"><div class="sectionhead"><h2>工作包</h2><div class="toolbar"><button disabled title="界面预览，自动拆解尚未接入">拆分工作包 · 未接入</button><button disabled title="界面预览，Agent 指派尚未接入">指派 Agent · 未接入</button></div></div><div class="emptyboard">尚未形成可执行计划<br><span class="muted">这里将显示子功能、负责的执行器、依赖关系和验收证据。<br>当前不展示虚构工作包；可打开独立模拟项目体验真实状态流转。</span></div></section>
<section class="panel" id="demoPanel" hidden><h2>体验一个独立的模拟项目</h2><p class="muted">这是固定的两任务文本示例，不是根据任意需求自动生成的计划。它只在应用目录中写入 a.txt 和 b.txt。</p><label>项目名称 / ID<input id="newid" value="demo"></label><div class="toolbar"><button id="create" class="primary">创建模拟项目</button><button id="createFailure">创建漏项失败示例</button></div><p class="muted">正常示例：A 写正文 → 验收通过 → B 写汇总。漏项示例会让 A 故意漏掉一行，展示失败与修复。</p></section>
<div id="projectView" hidden><div class="metrics" aria-label="当前选择范围的真实指标"><div class="metric"><span>工作包总数</span><strong id="metricTotal">—</strong><small id="metricScope">请选择执行项目</small></div><div class="metric"><span>已通过验收</span><strong id="metricDone">—</strong><small>基于当前验收状态</small></div><div class="metric"><span>阻塞 / 待返工</span><strong id="metricBlocked">—</strong><small>不是 Agent 自报完成数</small></div></div>
<section class="panel"><div class="messagehead">NORI <span class="pill" id="executorLabel">安全模拟</span></div><h2 id="title">计划已准备好</h2><div id="status" class="statusline"></div><p id="nextStep">请先查看计划，再批准执行。</p><div class="toolbar" id="controls"><button class="primary" data-action="approve">确认并批准计划</button><button class="primary" data-action="run">开始模拟</button><button data-action="pause">暂停</button><button data-action="resume">恢复</button><details><summary>更多操作</summary><button data-action="step">执行一步 / 验收停止结果</button><button data-action="reconcile">核对退出</button><button data-action="cancel">取消项目</button><button data-action="audit">审计</button><button data-action="finalize">整体验收</button></details></div><p id="job" class="muted"></p><button id="repairExample" hidden>修复示例并提交修订</button><div id="proposals"></div></section>
<section class="panel" id="workBoard"><div class="sectionhead"><h2>工作包与依赖</h2><div class="toolbar"><button disabled>拆分工作包 · 未接入</button><button disabled>指派 Agent · 未接入</button></div></div><div id="readablePlan"></div><div class="mindviewport"><div id="tasks" class="mindtree"></div></div><p class="contextmini">实线表示计划层级，不表示先后顺序。依赖另以文字标注。末级节点是实际验收项，不是新造的执行子任务。</p><details><summary>当前保存的计划（只读，批准对象）</summary><pre id="savedSpec"></pre></details></section><section class="panel" id="resultPanel" hidden><h2>整体验收结果</h2><div id="resultSummary" class="result"></div><details><summary>查看验收证据</summary><pre id="evidence"></pre></details></section>
<div class="advanced"><details><summary>高级 · 编辑计划、人工验收与运行历史</summary><p class="muted">草稿修改未提交不生效。提交修订后需要再次批准；不会自动覆盖当前计划。</p><textarea id="spec" aria-label="计划 JSON" rows="10"></textarea><input type="file" id="import" accept=".json,application/json"><button id="propose">提交为修订提案</button><details><summary>人工验收 / 返工</summary><input id="task" placeholder="任务 ID"><input id="requirement" placeholder="要求 ID"><input id="reason" placeholder="实际检查结论或操作原因"><button id="binding">读取验收绑定</button><select id="verdict"><option>PASS</option><option>FAIL</option></select><button id="manual">提交人工验收</button><button id="rework">授权返工</button><button id="retry">重试验收</button><pre id="bindingView"></pre></details><details><summary>完整状态 / 运行历史</summary><pre id="raw"></pre></details></details></div></div>
</div></main>
<aside class="right"><h2>节点详情</h2><div id="nodeDetails" class="node-detail">选择脑图节点查看详情。</div><div class="rsection"><h2>决策与监督</h2></div><span class="pill" id="overviewState">等待输入</span><div class="rsection"><h2>待决策</h2><div id="decisionQueue" class="empty">选择需求或项目后显示真实待办。</div></div><div class="rsection"><h2>偏离风险</h2><div id="riskList" class="empty">尚无执行记录，不能判断是否跑偏。</div><p class="contextmini">只展示已记录检查与验收发现。不代表持续语义监控，也不保证不存在偏离。</p></div><details><summary>阶段状态</summary><div class="rsection" style="border-top:0"><div class="progressrow"><span class="circle now" id="p1">1</span>记录需求</div><div class="progressrow"><span class="circle" id="p2">2</span>确认计划</div><div class="progressrow"><span class="circle" id="p3">3</span>执行任务</div><div class="progressrow"><span class="circle" id="p4">4</span>独立验收</div></div></details><div class="rsection"><h2>计划输出</h2><div id="files" class="empty">计划确认后，这里会显示<br>任务允许生成的文件。</div></div><div class="rsection"><h2>验收证据</h2><div id="acceptance" class="empty">还没有执行结果。<br>需求草稿不代表任务已经完成。</div></div><div class="rsection"><h2>工作区</h2><div id="workspace" class="empty">独立本机目录<br>当前不能选择任意已有工程。</div><details><summary>本地数据位置</summary><div id="dataLocation" class="empty" style="overflow-wrap:anywhere"></div></details></div><div class="tip" id="contextTip">先说清楚你想得到什么。<br>原始需求会保留下来，后续实现可以逐步完善。</div></aside>
</div><script nonce="__NONCE__">
const TOKEN=new URLSearchParams(location.hash.slice(1)).get('token')||'';let current='',snapshot=null,draft=null,binding=null,busy=false;
const $=id=>document.getElementById(id),pretty=x=>JSON.stringify(x,null,2),labels={AWAITING_APPROVAL:'等待批准计划',ACTIVE:'已批准',PAUSED:'已暂停',COMPLETED:'全部验收通过',AWAITING_DECISION:'需要你决定下一步',CANCELLED:'已取消',CLOSED_PARTIAL:'已部分结束',PENDING:'等待执行',REWORK:'验收失败，待返工',DONE:'验收通过',IN_PROGRESS:'执行中',IMPLEMENTED:'待验收',BLOCKED:'暂时阻塞',READY:'准备就绪',PASS:'通过',FAIL:'失败',INCONCLUSIVE:'尚不能确定'};const label=x=>labels[x]||x;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Nori-Token':TOKEN,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});const x=await r.json();if(!r.ok)throw Error(x.error);return x}
function note(e){$('notice').textContent=e?String(e):'';$('notice').className=e?'error':''}
async function guard(f){if(busy)return;busy=true;note('');$('saveDraft').disabled=true;try{await f()}catch(e){note(e.message)}finally{busy=false;$('saveDraft').disabled=false}}
function textNode(tag,text,cls){let el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;return el}
function reset(){current='';snapshot=null;draft=null;binding=null;$('welcome').hidden=false;$('prototypeBoard').hidden=false;$('unplannedBoard').hidden=false;$('requirementEditor').hidden=true;$('metricTotal').textContent='—';$('metricDone').textContent='—';$('metricBlocked').textContent='—';$('metricScope').textContent='请选择执行项目';$('decisionQueue').textContent='选择需求或项目后显示真实待办';$('riskList').textContent='尚无执行记录，不能判断是否跑偏';$('projectView').hidden=true;$('demoPanel').hidden=true;$('messages').replaceChildren();$('conversationTitle').textContent='需求中心';$('modeLabel').textContent='需求草稿';$('overviewState').textContent='等待输入';$('files').textContent='暂无计划输出';$('acceptance').textContent='未执行；尚无验收结果';$('workspace').textContent='本机应用目录；不读取其他项目';for(let i=1;i<=4;i++)$('p'+i).className='circle'+(i===1?' now':'')}
async function list(){let [p,d]=await Promise.all([api('/api/projects'),api('/api/drafts')]);$('projectList').replaceChildren();$('draftList').replaceChildren();for(const name of p.projects){let b=textNode('button','▱  '+name,'navitem'+(name===current?' active':''));b.onclick=()=>guard(()=>openProject(name));$('projectList').append(b)}for(const item of d.drafts){let b=textNode('button','◌  '+item.title,'navitem'+(draft?.id===item.id?' active':''));b.onclick=()=>guard(()=>openDraft(item.id));$('draftList').append(b)}renderRequirements(d.drafts);if(!d.drafts.length)$('draftList').append(textNode('small','尚无需求会话','muted'));if(!p.projects.length)$('projectList').append(textNode('small','尚无本地项目','muted'))}
function renderRequirements(drafts){$('requirementCount').textContent=drafts.length+' 条本地需求';$('requirementRows').replaceChildren();$('requirementsEmpty').hidden=!!drafts.length;for(const d of drafts){let row=document.createElement('tr');row.append(textNode('td',d.title),textNode('td','需求草稿 · 未规划'),textNode('td',new Date(d.updated_at).toLocaleDateString()));let cell=document.createElement('td'),button=textNode('button','查看需求');button.onclick=()=>guard(()=>openDraft(d.id));cell.append(button);row.append(cell);$('requirementRows').append(row)}}
function renderDraft(d){draft=d;$('prototypeBoard').hidden=true;$('nodeDetails').textContent='当前是已保存需求，没有自动拆分或指派 Agent。';$('welcome').hidden=true;$('projectView').hidden=true;$('demoPanel').hidden=true;$('conversationTitle').textContent=d.title;$('modeLabel').textContent='需求草稿 · 已保存';$('messages').replaceChildren();let card=textNode('section','','panel');let head=textNode('div','','sectionhead');head.append(textNode('h2',d.title),textNode('span','尚未规划','badge'));card.append(head,textNode('p','创建于 '+new Date(d.created_at).toLocaleString()+' · 最近更新 '+new Date(d.updated_at).toLocaleString(),'requirementmeta'),textNode('div','原始需求 · 保留原文','doclabel'),textNode('div',d.original_requirement,'requirementtext'));let extra=textNode('button','＋ 补充需求','linkbtn');extra.onclick=()=>showEditor();card.append(extra);if(d.messages.length>1){let history=textNode('details','');history.append(textNode('summary','需求补充记录 · '+(d.messages.length-1)+' 条'));let timeline=textNode('div','','timeline');for(const m of d.messages.slice(1)){let item=textNode('article','');item.append(textNode('div',new Date(m.created_at).toLocaleString(),'messagehead'),textNode('div',m.text,'requirementtext'));timeline.append(item)}history.append(timeline);card.append(history)}$('messages').append(card);$('overviewState').textContent='需求已保存';$('p1').className='circle done';$('p2').className='circle now';$('metricTotal').textContent='0';$('metricDone').textContent='0';$('metricBlocked').textContent='0';$('metricScope').textContent='当前需求：尚未拆解';$('files').textContent='尚无计划输出';$('acceptance').textContent='暂无验收条件与执行证据';$('workspace').textContent='需求记录保存在本机应用目录';$('decisionQueue').replaceChildren(textNode('div','需求已登记。下一步需要人工形成计划；自动拆解和 Agent 指派尚未接入。','queuerow'));$('riskList').textContent='未执行，尚不能评估偏离风险';$('contextTip').textContent='原始需求保持不变。工作包只有形成计划、批准后才可进入执行。当前记录没有启动任何 Agent。'}
function showEditor(){$('requirementEditor').hidden=false;$('editorTitle').textContent=draft?'补充需求（不覆盖原文）':'发起需求';$('requirementEditor').scrollIntoView({behavior:'smooth'});$('prompt').focus()}
async function openDraft(id){const d=await api('/api/draft/'+id);reset();renderDraft(d);await list()}
async function saveDraft(){const text=$('prompt').value;if(!text.trim())throw Error('先写下你想完成的事。');const saved=await api('/api/draft',{text,...(draft?{id:draft.id}:{})});reset();renderDraft(saved);$('prompt').value='';await list();$('feed').scrollTop=0}
async function openProject(name){const loaded=await api('/api/project/'+name);reset();current=name;snapshot=loaded;renderProject(true);await list()}
function makeMindNode(item,prototype=false){let node=textNode('button','','mind-node'+(item.root?' root-node':'')+(item.risk?' risk':''));node.dataset.nodeId=item.id;node.style.setProperty('--branch',item.color||'#8fa6b7');node.append(textNode('strong',item.title),textNode('span',(prototype?'示例 · ':'')+item.owner,'nodemeta'),textNode('span',item.status,'nodestatus'));if(item.dependency)node.append(textNode('span','依赖：'+item.dependency,'dep-label'));node.onclick=()=>selectMindNode(node,item,prototype);return node}
function selectMindNode(node,item,prototype){document.querySelectorAll('.mind-node.selected').forEach(n=>n.classList.remove('selected'));node.classList.add('selected');$('nodeDetails').replaceChildren();$('nodeDetails').append(textNode('span',prototype?'交互原型 · 示例信息':'真实记录 · 当前快照','pill'),textNode('h3',item.title));for(const [name,value]of [['状态',item.status],['执行器 / 负责人',item.owner],['目标',item.description],['依赖',item.dependency||'无前置依赖'],['风险',item.risk||'未提供额外风险；不代表没有偏离'],['验收依据',item.acceptance]]){let row=textNode('div','','detailrow');row.append(textNode('small',name),textNode('div',value||'尚未提供'));$('nodeDetails').append(row)}if(prototype)$('nodeDetails').append(textNode('p','所有节点、分配和状态均为设计示例。此处不会创建真实任务、调用 Agent 或修改 Nori。','prototype-note'))}
function renderTree(target,root,branches,prototype){const selectedId=target.querySelector('.mind-node.selected')?.dataset.nodeId;target.replaceChildren();let rootwrap=textNode('div','','mindroot');rootwrap.append(makeMindNode({...root,root:true},prototype));let list=textNode('div','','mindbranches');for(const branch of branches){let row=textNode('div','','mindbranch'),box=textNode('div','');box.append(makeMindNode(branch,prototype));let leaves=textNode('div','','mindleaves');for(const child of branch.children||[]){let wrap=textNode('div','','leafwrap');wrap.append(makeMindNode({...child,color:branch.color},prototype));leaves.append(wrap)}let toggle=textNode('button','− 收起分解','collapse-toggle');toggle.onclick=()=>{leaves.hidden=!leaves.hidden;toggle.textContent=leaves.hidden?'＋ 展开分解':'− 收起分解'};box.append(toggle);row.append(box,leaves);list.append(row)}target.append(rootwrap,list);if(!prototype){const selected=[...target.querySelectorAll('.mind-node')].find(n=>n.dataset.nodeId===selectedId)||target.querySelector('.mind-node');selected?.click()}}
function readablePlan(s){$('readablePlan').replaceChildren(textNode('p','当前图来自已保存计划；'+(s.spec.executor==='mock'?'明确标记为本地模拟，不调用真实模型。':'执行器以实际配置为准。'),'muted'));const branches=Object.entries(s.tasks).map(([id,t])=>{let plan=s.spec.tasks[id];return {id:'task-'+id,title:id+' · '+plan.why,status:label(t.status),owner:s.spec.executor==='mock'?'模拟执行器':s.spec.executor,description:'工作包来自当前批准版本，执行次数 '+t.attempts,dependency:plan.depends_on.join('、'),risk:t.evidence?.verdict==='FAIL'?'实际输出未满足验收要求；后续依赖不能通过。':t.evidence?.stale?'验收证据已过期，需要重新核对。':'',acceptance:plan.requirements.join('、'),children:plan.requirements.map(rid=>({id:'check-'+id+'-'+rid,title:rid+' · 验收项',status:t.evidence?label(t.evidence.verdict):'待验收',owner:'独立验收',description:typeof s.spec.requirements[rid]==='string'?s.spec.requirements[rid]:pretty(s.spec.requirements[rid]),acceptance:pretty(plan.checks[rid])}))}});renderTree($('tasks'),{id:'project-root',title:s.spec.goal,status:label(s.project_status),owner:s.spec.executor==='mock'?'模拟项目':'真实工具项目',description:'计划根节点，实线仅表示分解层级。',acceptance:'所有任务及整体验收均通过'},branches,false)}
const prototypeBranches=[
{id:'ux',title:'界面体验',status:'示例 · 设计中',owner:'UX Agent（示例）',color:'#91aebf',description:'让项目经理一眼理解目标、工作包、风险与验收。',acceptance:'原型可点击、缩放和查看节点详情',children:[{id:'ux-map',title:'任务脑图与交互',status:'示例 · 设计中',owner:'UI Agent（示例）',description:'大需求→工作包→子任务，实线只表示分解关系。',acceptance:'展开收起、选中详情、缩放适配可体验'},{id:'ux-detail',title:'需求与决策详情',status:'示例 · 待设计',owner:'UX Agent（示例）',description:'保留原始需求，显示待决策项与验收证据。',acceptance:'区分原型示例与真实记录'}]},
{id:'planning',title:'需求 / 任务拆解',status:'示例 · 待接入',owner:'Planner Agent（示例）',color:'#a49dc2',description:'从原始需求形成可审查计划，不自动批准。',acceptance:'所有工作包覆盖原始要求',children:[{id:'plan-scope',title:'目标与范围澄清',status:'示例 · 待实现',owner:'Planner Agent（示例）',description:'识别交付物、约束与不做的事项。',acceptance:'输出可由项目经理确认的范围'},{id:'plan-deps',title:'工作包与依赖',status:'示例 · 待实现',owner:'Planner Agent（示例）',description:'拆分大功能，明确依赖和验收条件。',dependency:'目标与范围澄清',acceptance:'禁止把父子关系当成执行顺序'}]},
{id:'supervision',title:'Agent 执行与监督',status:'示例 · 有待决策',owner:'PM Agent（示例）',color:'#c5aa7c',description:'把批准的任务交给执行器，并检查偏离迹象。',risk:'示例风险：如何界定语义跑偏仍需设计，不能仅凭无错误就宣称正常。',acceptance:'指派、运行与风险证据可追溯',children:[{id:'agent-assign',title:'执行器指派与交接',status:'示例 · 待接入',owner:'PM Agent（示例）',description:'为每个工作包明确执行器与工作边界。',acceptance:'没有授权不启动真实执行'},{id:'risk-drift',title:'跑偏检查与升级',status:'示例 · 需决策',owner:'Supervisor（示例）',description:'对照目标、范围和证据识别偏离，交由项目经理处理。',risk:'示例：范围检查不等于完整语义监督。需要明确证据、误报与升级策略。',dependency:'目标与范围澄清',acceptance:'每条告警说明依据和能力边界'}]},
{id:'acceptance',title:'独立验收',status:'示例 · 待设计',owner:'Reviewer（示例）',color:'#86ad96',description:'不把执行 Agent 的完成声明当作验收结论。',acceptance:'每个要求有独立验收证据',children:[{id:'accept-checks',title:'逐项检查与证据',status:'示例 · 待设计',owner:'Reviewer（示例）',description:'逐项对照原始要求，记录实际检查输出。',acceptance:'证据缺失时标注未确定而非通过'},{id:'accept-rework',title:'返工与最终验收',status:'示例 · 待设计',owner:'Reviewer（示例）',description:'验收失败保留历史，修订批准后再执行。',dependency:'逐项检查与证据',acceptance:'整体验收通过才确认项目完成'}]}
];
let graphScale=1;
function prototypeRoot(){return {id:'nori-root',title:'Nori 自我迭代',status:'首个任务 · 原型愿景',owner:'项目经理',description:'核心目标：用 Nori 内核完善 Nori Web。现有内核已有计划审批、串行模拟执行、依赖检查、验收和返工。自动拆解/派单与真实自主迭代仍待实现。',acceptance:'本轮只验收原型体验，不启动自我修改'}}
function renderPrototype(){renderTree($('prototypeGraph'),prototypeRoot(),prototypeBranches,true);const root=$('prototypeGraph').querySelector('.mind-node');selectMindNode(root,{...prototypeRoot(),root:true},true);$('unplannedBoard').hidden=true;$('riskList').textContent='当前为交互原型。示例风险不来自实时监控，不代表真实 Agent 已执行。';$('decisionQueue').textContent='原型示例：请确认任务拆解、Agent 指派和监督能力的产品形态。';$('overviewState').textContent='交互原型示例';requestAnimationFrame(fitGraph)}
function fitGraph(){const viewport=$('prototypeViewport'),graph=$('prototypeGraph');const available=Math.max(300,window.innerHeight-viewport.getBoundingClientRect().top-85);zoomGraph(Math.min(1,(viewport.clientWidth-44)/graph.scrollWidth,(available-56)/graph.scrollHeight))}
function zoomGraph(value){graphScale=Math.max(.5,Math.min(1.4,value));$('prototypeGraph').style.transform='scale('+graphScale+')';$('zoomLabel').textContent=Math.round(graphScale*100)+'%';$('prototypeViewport').style.height=Math.max(350,$('prototypeGraph').scrollHeight*graphScale+56)+'px'}
function renderSupervision(s){const tasks=Object.values(s.tasks);$('metricTotal').textContent=tasks.length;$('metricDone').textContent=tasks.filter(t=>t.status==='DONE'&&t.evidence?.verdict==='PASS').length;$('metricBlocked').textContent=tasks.filter(t=>['BLOCKED','REWORK'].includes(t.status)).length;$('metricScope').textContent='当前执行项目 · '+current;$('decisionQueue').replaceChildren();let decisions=[];if(s.project_status==='AWAITING_APPROVAL')decisions.push('待批准：检查目标、范围和验收条件后批准当前计划。');for(const [id,p]of Object.entries(s.proposals||{}))if(p.status==='PROPOSED')decisions.push('待批准修订：'+id);for(const [id,t]of Object.entries(s.tasks))if(['BLOCKED','REWORK'].includes(t.status))decisions.push('需处理工作包 '+id+'：'+label(t.status));if(s.project_status==='PAUSED')decisions.push('项目已暂停；退出与验收确认后才能恢复。');if(snapshot.job.error)decisions.push('调度错误：'+snapshot.job.error);if(!decisions.length)$('decisionQueue').append(textNode('div','当前没有记录到待决策事项。','empty'));for(const text of decisions)$('decisionQueue').append(textNode('div',text,'queuerow'));
$('riskList').replaceChildren();let risks=[];for(const [id,t]of Object.entries(s.tasks)){if(t.evidence?.verdict==='FAIL')risks.push(id+'：实际产物未满足验收要求，尚未通过。');if(t.evidence?.stale)risks.push(id+'：验收证据已过期，需要重新核对。');const run=s.runs[t.run_id];if(run?.scope_violation)risks.push(id+'：记录到写入范围违规。')}const findings=(s.findings||[]).filter(f=>f.blocking);if(findings.length)risks.push('历史监督记录中有 '+findings.length+' 条阻塞发现，请查看完整状态核对是否已处理。');if(!risks.length)$('riskList').append(textNode('div',Object.keys(s.runs).length?'现有记录未显示当前偏离告警；这不代表全面检查或没有风险。':'尚未执行，没有可用于判断偏离的运行记录。','empty'));for(const text of risks)$('riskList').append(textNode('div',text,'riskrow'))}
function renderProject(updateSpec=false){let s=snapshot.state,running=snapshot.job.running,lease=!!s.lease,terminal=['COMPLETED','CANCELLED','CLOSED_PARTIAL'].includes(s.project_status),failed=Object.values(s.tasks).some(t=>t.evidence?.verdict==='FAIL'),pending=Object.values(s.proposals||{}).some(p=>p.status==='PROPOSED');$('welcome').hidden=true;$('projectView').hidden=false;$('prototypeBoard').hidden=true;$('unplannedBoard').hidden=true;$('requirementEditor').hidden=true;$('demoPanel').hidden=true;$('conversationTitle').textContent=current;$('modeLabel').textContent=s.spec.executor==='mock'?'模拟项目':'真实工具项目';$('executorLabel').textContent=s.spec.executor==='mock'?'安全模拟':'真实工具';$('title').textContent=s.spec.goal;$('status').textContent=label(s.project_status)+' · '+s.project_status+' · 修订 '+s.revision;$('job').textContent=running?'正在执行与验收 · 已执行 '+snapshot.job.steps+' 步':snapshot.job.error?'执行停止：'+snapshot.job.error:'当前没有后台调度';$('raw').textContent=pretty(s);$('savedSpec').textContent=pretty(s.spec);$('evidence').textContent=pretty(s.final_evidence);if(updateSpec)$('spec').value=pretty(s.spec);readablePlan(s);renderSupervision(s);
$('nextStep').textContent=s.project_status==='AWAITING_APPROVAL'?'计划已保存。请检查下方目标、输出范围和验收条件，然后批准。':running?'正在按顺序执行并独立验收，结果会自动刷新。':s.project_status==='COMPLETED'?'完成。所有任务和整体验收均已通过，可以查看结果证据。':pending?'修订已提交。请检查修改内容，单独批准后再继续。':failed?'验收发现漏项，后续任务不会开始。可以提交修复示例的修订，再批准执行。':s.project_status==='PAUSED'?lease?'已暂停调度。请在更多操作中核对退出并验收停止结果，再恢复。':'已暂停，可以恢复。':terminal?'项目已结束。':'计划已批准。点击开始模拟或执行，结果会自动更新。';
const allowed={approve:s.project_status==='AWAITING_APPROVAL',run:s.project_status==='ACTIVE'&&!pending&&!failed&&!running,pause:!terminal&&s.project_status==='ACTIVE',resume:s.project_status==='PAUSED'&&!lease,cancel:!terminal,step:!terminal&&!running&&s.project_status!=='AWAITING_APPROVAL',reconcile:lease&&!running,audit:!running,finalize:s.project_status==='ACTIVE'&&!lease&&!running};$('controls').querySelectorAll('[data-action]').forEach(b=>{b.disabled=!allowed[b.dataset.action];if(b.dataset.action==='run')b.textContent=s.spec.executor==='mock'?'开始模拟':'开始执行（真实工具）'});$('repairExample').hidden=!(s.spec.executor==='mock'&&s.spec.tasks.A?.mock_mode==='incomplete');$('repairExample').disabled=!failed||lease||running||pending;
$('proposals').replaceChildren();for(const [id,p]of Object.entries(s.proposals||{})){if(p.status!=='PROPOSED')continue;let wrap=document.createElement('div'),b=textNode('button','批准修订 · '+id,'primary'),details=document.createElement('details');details.append(textNode('summary','查看修订计划（批准对象）'),textNode('pre',pretty(p.spec)));b.onclick=()=>guard(async()=>{if(confirm('批准以下修订？\n'+pretty(p.spec)))await cmd('accept-revision',{proposal:id,digest:p.digest})});wrap.append(details,b);$('proposals').append(wrap)}
$('resultPanel').hidden=!s.final_evidence;$('resultSummary').textContent=s.final_evidence?'整体验收：'+label(s.final_evidence.verdict):'';$('overviewState').textContent=label(s.project_status);$('files').replaceChildren();for(const name of [...new Set(Object.values(s.spec.tasks).flatMap(t=>t.write_scope))])$('files').append(textNode('div','▱ '+name+' · 计划范围','file'));$('acceptance').textContent=s.final_evidence?'整体验收 '+label(s.final_evidence.verdict):Object.entries(s.tasks).map(([id,t])=>id+'：'+(t.evidence?label(t.evidence.verdict):'待验收')).join(' / ');$('workspace').textContent=s.workspace;$('workspace').style.overflowWrap='anywhere';$('contextTip').textContent='任务自报完成不等于验收通过。每次验收都会对照已批准的要求和实际输出。';for(let i=1;i<=4;i++)$('p'+i).className='circle'+(i===1||i===2&&s.approval||s.project_status==='COMPLETED'?' done':i===2&&!s.approval||i===3&&s.approval?' now':'')}
async function refresh(updateSpec=false){if(!current)return;const name=current;const loaded=await api('/api/project/'+name);if(current!==name)return;snapshot=loaded;renderProject(updateSpec)}
async function cmd(action,extra={}){if(!current||!snapshot)throw Error('请先打开项目');const result=await api('/api/command',{project:current,action,revision:snapshot.state.revision,...extra});await refresh(action==='accept-revision');return result}
async function create(spec){const name=$('newid').value;const created=await api('/api/command',{project:name,action:'create',spec});reset();current=name;snapshot=created;binding=null;renderProject(true);await list()}
$('graphZoomIn').onclick=()=>zoomGraph(graphScale+.1);$('graphZoomOut').onclick=()=>zoomGraph(graphScale-.1);$('graphFit').onclick=fitGraph;$('collapseGraph').onclick=()=>{const leaves=$('prototypeGraph').querySelectorAll('.mindleaves');const collapse=[...leaves].some(n=>!n.hidden);leaves.forEach(n=>n.hidden=collapse);$('collapseGraph').textContent=collapse?'展开子任务':'收起子任务';$('prototypeGraph').querySelectorAll('.collapse-toggle').forEach(n=>n.textContent=collapse?'＋ 展开分解':'− 收起分解')};$('showPrototype').onclick=()=>guard(async()=>{reset();renderPrototype();await list()});
$('toggleContext').onclick=()=>document.querySelector('.app').classList.toggle('no-context');
$('newChat').onclick=()=>guard(async()=>{reset();await list();showEditor()});$('writeIdea').onclick=()=>guard(async()=>{reset();await list();showEditor()});$('closeEditor').onclick=()=>{$('requirementEditor').hidden=true};$('showDemo').onclick=()=>{$('demoPanel').hidden=false;$('demoPanel').scrollIntoView({behavior:'smooth'})};$('saveDraft').onclick=()=>guard(saveDraft);$('prompt').onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();guard(saveDraft)}};
async function demoSpec(){let s=await api('/api/example');s.goal='生成完整正文，再按依赖生成汇总副本';s.tasks.A.why='生成正文，并精确核对是否包含全部要求';s.tasks.B.why='读取已验收正文并生成汇总副本';return s}
$('create').onclick=()=>guard(async()=>create(await demoSpec()));$('createFailure').onclick=()=>guard(async()=>{let s=await demoSpec();s.tasks.A.mock_mode='incomplete';await create(s)});
$('controls').onclick=e=>{const action=e.target.dataset.action;if(!action)return;guard(async()=>{let extra={};if(action==='run'&&snapshot?.state.spec.executor==='mock'&&snapshot.state.spec.tasks.A?.mock_mode==='incomplete')extra.max_steps=2;if(action==='approve'){if(!confirm('仅批准以下已保存计划，不包含未提交草稿。\n'+pretty(snapshot.state.spec)))return;extra.digest=snapshot.approval_digest}if(action==='cancel'){extra.reason=prompt('取消原因');if(!extra.reason)return}await cmd(action,extra)})};
$('repairExample').onclick=()=>guard(()=>cmd('repair-example'));$('propose').onclick=()=>guard(async()=>{const reason=prompt('修订原因');if(reason)await cmd('propose',{proposal:'revision-'+Date.now(),spec:JSON.parse($('spec').value),reason})});$('import').onchange=()=>guard(async()=>{$('spec').value=await $('import').files[0].text();JSON.parse($('spec').value)});
function fields(){return {task:$('task').value,requirement:$('requirement').value,reason:$('reason').value}}
$('binding').onclick=()=>guard(async()=>{let x=await cmd('manual-binding',fields());binding=x.result.binding;$('bindingView').textContent=pretty(x.result)});$('manual').onclick=()=>guard(()=>cmd('manual',{...fields(),binding,verdict:$('verdict').value}));$('rework').onclick=()=>guard(()=>cmd('rework',fields()));$('retry').onclick=()=>guard(()=>cmd('retry-verification',fields()));
renderPrototype();
guard(async()=>{const info=await api('/api/info');$('dataLocation').textContent=info.data_dir;await list()});setInterval(async()=>{if(current&&!busy){try{await refresh()}catch(e){note(e.message)}}},1500);
</script></body></html>'''
