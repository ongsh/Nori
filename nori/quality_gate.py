"""Independent deterministic local checks. Registered commands are not a sandbox."""
import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from .store import NoriError, digest

MAX_FILE_BYTES = 1048576


def _path(root, name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise NoriError('Invalid check path')
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] == '.nori.lock':
        raise NoriError('Unsafe check path')
    target = Path(root).absolute() / p
    if any(x.is_symlink() for x in [target, *target.parents]):
        raise NoriError('Symlinks are forbidden')
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise NoriError('Check path escapes workspace')
    return target


def _operation(spec, name):
    ops = spec.get('operations', {})
    if not isinstance(name, str) or not isinstance(ops, dict) or name not in ops:
        raise NoriError('Unknown operation')
    op = ops[name]
    if not isinstance(op, dict) or set(op) - {'argv', 'timeout_seconds', 'max_output_bytes', 'generated_paths'}:
        raise NoriError('Only fixed argv, bounded limits and generated_paths allowed; shell/env/cwd forbidden')
    argv = op.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and '\x00' not in a for a in argv):
        raise NoriError('Operation needs fixed argv')
    if Path(argv[0]).name.lower() in {'sh','bash','dash','zsh','fish','ksh','cmd','cmd.exe','powershell','powershell.exe','pwsh'}:
        raise NoriError('Shell operations forbidden')
    timeout, limit = op.get('timeout_seconds', 10), op.get('max_output_bytes', 65536)
    if isinstance(timeout, bool) or not isinstance(timeout, (int,float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise NoriError('Invalid timeout')
    if type(limit) is not int or not 1 <= limit <= MAX_FILE_BYTES:
        raise NoriError('Invalid output limit')
    paths = op.get('generated_paths', [])
    if not isinstance(paths, list):
        raise NoriError('Invalid generated_paths')
    for path in paths:
        _path('/tmp/nori-check-validation', path)
    return {'argv': argv[:], 'timeout_seconds': timeout, 'max_output_bytes': limit, 'generated_paths': paths[:]}


def validate_check(descriptor, spec, requirement_id=None, workspace=None):
    if isinstance(descriptor, str):
        if requirement_id not in spec.get('requirements', {}):
            raise NoriError('Legacy check needs known requirement')
        descriptor = {'kind':'exact_text','path':descriptor,'expected':spec['requirements'][requirement_id]}
    if not isinstance(descriptor, dict):
        raise NoriError('Invalid descriptor')
    d = copy.deepcopy(descriptor)
    kind = d.get('kind')
    fields = {'exact_text':{'kind','path','expected'},'json_value':{'kind','path','pointer','expected'},'command':{'kind','operation'},'manual':{'kind','prompt'}}
    if kind not in fields or set(d) != fields[kind]:
        raise NoriError('Unknown check kind or fields')
    if kind in ('exact_text','json_value'):
        _path(workspace or '/tmp/nori-check-validation', d['path'])
    if kind == 'exact_text' and not isinstance(d['expected'], str):
        raise NoriError('Expected text required')
    if kind == 'json_value':
        p = d['pointer']
        if not isinstance(p,str) or (p and not p.startswith('/')) or re.search(r'~(?![01])',p):
            raise NoriError('Invalid JSON Pointer')
        try:
            json.dumps(d['expected'], allow_nan=False)
        except (TypeError,ValueError) as exc:
            raise NoriError('Invalid JSON expected value') from exc
    if kind == 'command':
        _operation(spec,d['operation'])
    if kind == 'manual' and (not isinstance(d['prompt'],str) or not d['prompt'].strip()):
        raise NoriError('Manual prompt required')
    return d


def check_paths(descriptor, spec=None):
    if isinstance(descriptor,str):
        return [descriptor]
    return [descriptor['path']] if descriptor.get('kind') in ('exact_text','json_value') else []


def _snapshot(root):
    result = {}
    for p in sorted(root.rglob('*')):
        name = p.relative_to(root).as_posix()
        if name == '.nori.lock':
            continue
        _path(root,name)
        if p.is_file():
            h = hashlib.sha256()
            with p.open('rb') as f:
                for block in iter(lambda:f.read(65536),b''):
                    h.update(block)
            result[name] = h.hexdigest()
    return result


def _reject_constant(value):
    raise ValueError('Non-standard JSON constant: ' + value)


def _pointer(value,pointer):
    for token in pointer[1:].split('/') if pointer else []:
        token = token.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            if not re.fullmatch(r'0|[1-9][0-9]*',token):
                raise KeyError(token)
            value = value[int(token)]
        elif isinstance(value,dict):
            value = value[token]
        else:
            raise KeyError(token)
    return value


def _command(root,op):
    start = time.monotonic()
    p = subprocess.Popen(op['argv'],cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,start_new_session=True)
    sel = selectors.DefaultSelector()
    outputs = {'stdout':bytearray(),'stderr':bytearray()}
    for name in outputs:
        pipe = getattr(p,name)
        os.set_blocking(pipe.fileno(),False)
        sel.register(pipe,selectors.EVENT_READ,name)
    timeout = exceeded = False
    try:
        while sel.get_map():
            remaining = op['timeout_seconds']-(time.monotonic()-start)
            if remaining <= 0:
                timeout = True
                break
            for key,_ in sel.select(min(.05,remaining)):
                chunk = os.read(key.fileobj.fileno(),65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                available = op['max_output_bytes']-sum(map(len,outputs.values()))
                outputs[key.data].extend(chunk[:available])
                if len(chunk)>available:
                    exceeded = True
                    break
            if exceeded:
                break
        if not timeout and not exceeded:
            # Observe exit WITHOUT reaping: retain ownership of the PID/PGID
            # until group cleanup so a reused group can never be signalled.
            while os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if time.monotonic()-start >= op['timeout_seconds']:
                    timeout = True
                    break
                time.sleep(.005)
    finally:
        try:
            os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        sel.close()
        p.stdout.close()
        p.stderr.close()
    result = {'exit_code':p.returncode,'timed_out':timeout,'output_limit_exceeded':exceeded}
    for name,data in outputs.items():
        result[name] = bytes(data).decode('utf-8',errors='replace')
        result[name+'_base64'] = base64.b64encode(data).decode('ascii')
    return result


class QualityGate:
    def __init__(self,workspace,spec,manual_decisions=None):
        self.root = Path(workspace).absolute()
        _path(self.root,'__root_validation__')
        self.spec = copy.deepcopy(spec)
        self.manual_decisions = copy.deepcopy(manual_decisions or {})

    def _binding(self,rid,d,files):
        return digest({'spec':self.spec,'requirement_id':rid,'check':d,'files':files})

    def binding(self,requirement_id,descriptor):
        d = validate_check(descriptor,self.spec,requirement_id,self.root)
        return self._binding(requirement_id,d,_snapshot(self.root))

    def evaluate(self,mapping):
        checks = []
        for rid,descriptor in mapping.items():
            if rid not in self.spec.get('requirements',{}):
                raise NoriError('Unknown requirement')
            d = validate_check(descriptor,self.spec,rid,self.root)
            before = _snapshot(self.root)
            binding = self._binding(rid,d,before)
            kind = d['kind']
            limits = {'max_file_bytes':MAX_FILE_BYTES}
            verdict,raw = 'INCONCLUSIVE',{}
            try:
                if kind in ('exact_text','json_value'):
                    path = _path(self.root,d['path'])
                    if path.exists() and not path.is_file():
                        raise OSError('Check target must be a regular file')
                    with path.open('rb') as f:
                        data = f.read(MAX_FILE_BYTES+1)
                    if len(data)>MAX_FILE_BYTES:
                        raw = {'error':'File exceeds read limit'}
                    else:
                        raw = {'expected':d['expected'],'content_sha256':hashlib.sha256(data).hexdigest(),'content_base64':base64.b64encode(data).decode('ascii')}
                        text = data.decode('utf-8')
                        actual = text if kind == 'exact_text' else _pointer(json.loads(text, parse_constant=_reject_constant),d['pointer'])
                        raw['actual'] = actual
                        verdict = 'PASS' if (actual == d['expected'] if kind == 'exact_text' else digest(actual)==digest(d['expected'])) else 'FAIL'
                elif kind == 'command':
                    op = _operation(self.spec,d['operation'])
                    limits = {k:v for k,v in op.items() if k!='argv'}
                    raw = _command(self.root,op)
                    raw['argv'] = op['argv']
                    verdict = 'INCONCLUSIVE' if raw['timed_out'] or raw['output_limit_exceeded'] else ('PASS' if raw['exit_code']==0 else 'FAIL')
                else:
                    decision = self.manual_decisions.get(rid)
                    raw = {'prompt':d['prompt'],'decision':decision}
                    if (isinstance(decision,dict) and decision.get('binding')==binding and decision.get('verdict') in ('PASS','FAIL') and isinstance(decision.get('reason'),str) and decision['reason'].strip()):
                        verdict = decision['verdict']
                    else:
                        raw['error'] = 'Explicit current bound manual decision required'
            except FileNotFoundError as exc:
                verdict,raw = ('INCONCLUSIVE' if kind == 'command' else 'FAIL'),{'error':str(exc)}
            except UnicodeError as exc:
                verdict,raw = 'INCONCLUSIVE',{**raw,'error':str(exc)}
            except (KeyError,IndexError,ValueError) as exc:
                verdict,raw = 'FAIL',{**raw,'error':str(exc)}
            except OSError as exc:
                verdict,raw = 'INCONCLUSIVE',{**raw,'error':str(exc)}
            after = _snapshot(self.root)
            if after != before:
                verdict = 'INCONCLUSIVE'
                changed = sorted(p for p in before.keys()|after.keys() if before.get(p)!=after.get(p))
                raw['workspace_changed'] = changed
                allowed = _operation(self.spec,d['operation'])['generated_paths'] if kind=='command' else []
                raw['out_of_scope_changes'] = [p for p in changed if p not in allowed]
            checks.append({'requirement_id':rid,'path':d.get('path'),'verdict':verdict,'raw_output':raw,'method':d,'limits':limits,'binding':binding,'input_snapshot':before,'output_snapshot':after})
        verdict = 'FAIL' if any(c['verdict']=='FAIL' for c in checks) else ('INCONCLUSIVE' if not checks or any(c['verdict']=='INCONCLUSIVE' for c in checks) else 'PASS')
        return verdict,checks
