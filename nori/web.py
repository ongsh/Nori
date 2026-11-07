"""Loopback operator UI. No remote binding, file browser, or shell endpoint."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
import webbrowser

from .store import Store, NoriError, digest
from .workflow import WorkflowController
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
            path.mkdir(mode=0o700, exist_ok=False)
            with Store(path / 'control') as store:
                WorkflowController(store, path / 'work').initialize(spec)
            return self.snapshot(name)
        if action == 'run':
            return self.start_job(name, body)
        if action in ('pause', 'cancel'):
            with self.lock:
                if name in self.jobs:
                    self.jobs[name]['stop'].set()
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
            return {'result': result, 'revision': c.state['revision']}

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


HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nori · 工作台</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#182135;font:15px system-ui}header{background:#182135;color:white;padding:24px 5%;display:flex;justify-content:space-between}main{max-width:1280px;margin:auto;padding:24px;display:grid;grid-template-columns:280px 1fr;gap:24px}section{background:white;border:1px solid #dfe4ee;border-radius:14px;padding:20px;margin-bottom:18px}h1{margin:0;font-size:25px}h2{font-size:18px;margin:0 0 14px}p{line-height:1.6}button,select,input,textarea{font:inherit;border:1px solid #c9d1df;border-radius:7px;padding:9px;margin:4px 2px}button{background:#fff;cursor:pointer}button:hover{background:#eef2ff}button.primary{background:#4058d6;color:white}button:disabled{opacity:.5;cursor:wait}input,select{max-width:100%}textarea{width:100%;min-height:230px;font:13px ui-monospace,monospace}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px ui-monospace,monospace;max-height:450px;overflow:auto}.muted{color:#657186;font-size:13px}.error{color:#a11b32;background:#fff0f2;padding:12px;border-radius:8px;white-space:pre-wrap}.badge{display:inline-block;background:#eef2ff;padding:6px 10px;border-radius:20px}article{border-top:1px solid #e0e5ef;padding:12px 0}summary{cursor:pointer;font-weight:600}.toolbar{display:flex;flex-wrap:wrap}#notice:empty{display:none}@media(max-width:780px){main{display:block}header{display:block}}</style>
<header><div><h1>Nori 工作台</h1><span>计划、执行、独立验收</span></div><span class="muted" style="color:#bdc7df">本机运行 · 默认安全模拟 · 无需终端操作</span></header>
<main><aside><section><h2>项目</h2><select id="projects" aria-label="选择项目"></select><button id="load">打开</button><hr><label>新项目 ID<input id="newid" placeholder="my-demo" value="demo"></label><button class="primary" id="create">创建模拟项目</button><button id="createFailure">创建漏项失败示例</button><p class="muted">仅在本应用数据目录创建独立工作区，不读取你的真实项目。</p></section><section><h2>使用顺序</h2><p>1. 创建或导入计划<br>2. 阅读计划并批准<br>3. 执行，查看独立验收<br>4. 整体验收通过后完成</p><p class="muted">暂停请求不等于已停止。若有运行许可，请继续核对退出并验收，再恢复或取消。</p></section></aside>
<div><div id="notice" role="alert"></div><section><h2 id="title">欢迎使用 Nori</h2><p id="status">创建一个模拟项目开始。不会调用真实模型。</p><div class="toolbar" id="controls"><button data-action="approve" class="primary">批准当前计划</button><button data-action="step">执行一步</button><button data-action="run" class="primary">连续执行</button><button data-action="pause">暂停</button><button data-action="reconcile">核对退出</button><button data-action="resume">恢复</button><button data-action="cancel">取消项目</button><button data-action="audit">审计</button><button data-action="finalize">整体验收</button></div><p id="job" class="muted"></p></section>
<section><h2>当前保存的计划（只读，批准对象）</h2><p class="muted">“批准当前计划”仅批准此处保存的内容，不包含下方未提交的草稿修改。</p><pre id="savedSpec">尚未选择项目</pre></section><section><h2>任务与依赖</h2><div id="tasks">尚未选择项目</div></section><section><details open><summary>计划草稿 / 导入 JSON（编辑不会自动保存）</summary><p><strong>草稿修改未提交不生效。</strong>请点击“按编辑器计划创建新项目”，或“提交为修订提案”后批准该提案。</p><p class="muted">批准前请检查目标、要求、写入范围、执行器和验收操作。mock 为模拟；codex / claude 可能调用已登录工具并产生成本。</p><textarea id="spec" aria-label="计划 JSON"></textarea><input type="file" id="import" accept=".json,application/json"><button id="importCreate">按编辑器计划创建新项目</button><button id="propose">提交为修订提案</button><div id="proposals"></div></details></section>
<section><details><summary>人工验收 / 返工</summary><input id="task" placeholder="任务 ID（最终人工验收可留空）"><input id="requirement" placeholder="要求 ID"><input id="reason" placeholder="实际检查结论或操作原因"><button id="binding">读取人工验收绑定</button><select id="verdict"><option>PASS</option><option>FAIL</option></select><button id="manual">提交人工验收</button><button id="rework">授权返工</button><button id="retry">重试验收</button><pre id="bindingView"></pre></details></section>
<section><details><summary>整体验收证据</summary><pre id="evidence">暂无</pre></details><details><summary>完整状态 / 运行历史</summary><pre id="raw"></pre></details></section></div></main>
<script nonce="__NONCE__">
const TOKEN=new URLSearchParams(location.hash.slice(1)).get('token')||'';let current='',snapshot=null,binding=null,busy=false;
const $=id=>document.getElementById(id);const pretty=x=>JSON.stringify(x,null,2);
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Nori-Token':TOKEN,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});const x=await r.json();if(!r.ok)throw Error(x.error);return x}
function note(e){$('notice').textContent=e?String(e):'';$('notice').className=e?'error':''}
async function guard(f){if(busy)return;busy=true;note('');try{await f()}catch(e){note(e.message)}finally{busy=false}}
async function list(){const x=await api('/api/projects');$('projects').replaceChildren(...x.projects.map(n=>{let o=document.createElement('option');o.value=n;o.textContent=n;return o}));if(current)$('projects').value=current}
async function refresh(updateSpec=false){if(!current)return;snapshot=await api('/api/project/'+current);let s=snapshot.state;$('title').textContent=current+' · '+s.spec.goal;$('status').textContent=s.project_status+' · 修订 '+s.revision+' · 执行器 '+s.spec.executor+' · 运行许可 '+(s.lease||'空闲');$('job').textContent=snapshot.job.running?'后台执行中 · 已执行 '+snapshot.job.steps+' 步':snapshot.job.error?'后台停止：'+snapshot.job.error:'后台空闲';$('raw').textContent=pretty(s);$('savedSpec').textContent=pretty(s.spec);$('evidence').textContent=pretty(s.final_evidence);if(updateSpec)$('spec').value=pretty(s.spec);$('tasks').replaceChildren();for(const [id,t] of Object.entries(s.tasks)){let a=document.createElement('article'),h=document.createElement('strong'),p=document.createElement('p'),d=document.createElement('details'),sum=document.createElement('summary'),pre=document.createElement('pre');h.textContent=id+' · '+t.status+' · 尝试 '+t.attempts;p.textContent='依赖：'+(s.spec.tasks[id].depends_on.join(', ')||'无')+' | '+snapshot.ready.find(x=>x.task_id===id).reasons.join('；');sum.textContent='查看要求与验收证据';pre.textContent=pretty({plan:s.spec.tasks[id],evidence:t.evidence});d.append(sum,pre);a.append(h,p,d);$('tasks').append(a)}$('proposals').replaceChildren();for(const [id,p]of Object.entries(s.proposals||{})){let b=document.createElement('button');b.textContent=id+' · '+p.status+' · 批准修订';b.disabled=p.status!=='PROPOSED';b.onclick=()=>guard(async()=>{if(confirm('批准以下修订提案 '+id+'？\n摘要：'+p.digest+'\n'+pretty(p.spec)))await cmd('accept-revision',{proposal:id,digest:p.digest})});$('proposals').append(b)}}
async function cmd(action,extra={}){if(!current||!snapshot)throw Error('请先打开项目');const result=await api('/api/command',{project:current,action,revision:snapshot.state.revision,...extra});await refresh(action==='accept-revision');return result}
async function create(spec){const name=$('newid').value;const created=await api('/api/command',{project:name,action:'create',spec});current=name;snapshot=created;binding=null;await list();await refresh(true)}
$('createFailure').onclick=()=>guard(async()=>{let s=await api('/api/example');s.tasks.A.mock_mode='incomplete';await create(s)});$('create').onclick=()=>guard(async()=>create(await api('/api/example')));$('importCreate').onclick=()=>guard(()=>create(JSON.parse($('spec').value)));$('load').onclick=()=>guard(async()=>{current=$('projects').value;await refresh(true)});
$('controls').onclick=e=>{const action=e.target.dataset.action;if(!action)return;guard(async()=>{let extra={};if(action==='approve'){if(!snapshot)throw Error('请先打开项目');if(!confirm('仅批准以下已保存计划，草稿编辑器中的未提交修改不包含在内。\n摘要：'+snapshot.approval_digest+'\n'+pretty(snapshot.state.spec)))return;extra.digest=snapshot.approval_digest}if(action==='cancel'){extra.reason=prompt('取消原因');if(!extra.reason)return}await cmd(action,extra)})};
$('import').onchange=()=>guard(async()=>{$('spec').value=await $('import').files[0].text();JSON.parse($('spec').value)});
$('propose').onclick=()=>guard(async()=>{let reason=prompt('修订原因');if(reason)await cmd('propose',{proposal:'revision-'+Date.now(),spec:JSON.parse($('spec').value),reason})});
function fields(){return {task:$('task').value,requirement:$('requirement').value,reason:$('reason').value}}
$('binding').onclick=()=>guard(async()=>{let x=await cmd('manual-binding',fields());binding=x.result.binding;$('bindingView').textContent=pretty(x.result)});
$('manual').onclick=()=>guard(()=>cmd('manual',{...fields(),binding,verdict:$('verdict').value}));$('rework').onclick=()=>guard(()=>cmd('rework',fields()));$('retry').onclick=()=>guard(()=>cmd('retry-verification',fields()));
guard(async()=>{await list();$('spec').value=pretty(await api('/api/example'));if($('projects').value){current=$('projects').value;await refresh(true)}});
setInterval(async()=>{if(current&&!busy){try{await refresh()}catch(e){note(e.message)}}},1500);
</script></html>'''
