"""Isolated illustrative trees. Never schedules agents or touches kernel state.

Nodes use flat adjacency: nesting is not limited to three (or any fixed) levels.
Practical limits are returned with every project. POSIX file locking protects
multiple store instances/processes; atomic replacement preserves prior saves.
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

try:
    import fcntl
except ImportError:  # Thread safety on non-POSIX platforms.
    fcntl = None

LIMITS = {'max_nodes': 2000, 'max_projects': 100, 'max_title_chars': 200,
          'max_text_chars': 10000, 'max_file_bytes': 32 * 1024 * 1024}
_MUTEX = threading.RLock()
_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')


class PrototypeError(ValueError):
    pass


class RevisionConflict(PrototypeError):
    pass


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PrototypeError('Invalid prototype identifier')
    return value


def _text(value, title=False):
    limit = LIMITS['max_title_chars' if title else 'max_text_chars']
    if not isinstance(value, str) or len(value) > limit or (title and not value.strip()):
        raise PrototypeError('Invalid text or text exceeds limit')
    return value.strip() if title else value


def _node(title, parent=None):
    return dict(id=uuid.uuid4().hex, parent_id=parent, title=_text(title, True),
                description='', agent='', progress=0, status='未开始',
                agent_report='', acceptance='未验收', collapsed=False)


class PrototypeStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir).absolute() / '.prototypes'
        for part in [self.path, *self.path.parents]:
            if part.is_symlink():
                raise PrototypeError('Symlink prototype paths are not allowed')
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def _locked(self):
        with _MUTEX:
            # Recheck ancestry on every operation, not just construction.
            for part in [self.path, *self.path.parents]:
                if part.is_symlink():
                    raise PrototypeError('Symlink prototype paths are not allowed')
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            directory = os.open(self.path, flags)
            lock = None
            try:
                lock = os.open('.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=directory)
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise PrototypeError('Invalid prototype lock')
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                yield directory
            except OSError as exc:
                raise PrototypeError('Cannot safely access prototype storage') from exc
            finally:
                if lock is not None:
                    os.close(lock)
                os.close(directory)

    def _read(self, directory, project_id):
        filename = _id(project_id) + '.json'
        try:
            fd = os.open(filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=directory)
        except FileNotFoundError as exc:
            raise PrototypeError('Prototype project not found') from exc
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMITS['max_file_bytes']:
                raise PrototypeError('Invalid prototype file')
            try:
                project = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise PrototypeError('Invalid prototype JSON; original file preserved') from exc
        self._validate(project, project_id)
        project['limits'] = dict(LIMITS)
        return project

    def _validate(self, p, project_id):
        try:
            if (p['id'] != project_id or type(p['revision']) is not int or p['revision'] < 0
                    or p.get('illustrative_only') is not True):
                raise PrototypeError('Invalid project metadata')
            _text(p['title'], True)
            nodes = p['nodes']
            if not isinstance(nodes, list) or not 1 <= len(nodes) <= LIMITS['max_nodes']:
                raise PrototypeError('Invalid node count')
            by_id = {}
            children = {}
            for n in nodes:
                nid = _id(n['id'])
                if nid in by_id:
                    raise PrototypeError('Duplicate node ID')
                by_id[nid] = n
                _text(n['title'], True)
                for field in ('description', 'agent', 'status', 'agent_report', 'acceptance'):
                    _text(n[field])
                if 'acceptance_criteria' in n:
                    from .intake import _strings
                    _strings(n['acceptance_criteria'])
                if type(n['progress']) is not int or not 0 <= n['progress'] <= 100 or type(n['collapsed']) is not bool:
                    raise PrototypeError('Invalid progress or collapse value')
                if n['parent_id'] is not None:
                    _id(n['parent_id'])
                children.setdefault(n['parent_id'], []).append(nid)
            if children.get(None) != [p['root_id']]:
                raise PrototypeError('Tree must have exactly one root')
            seen, todo = set(), [p['root_id']]
            while todo:
                nid = todo.pop()
                if nid in seen:
                    raise PrototypeError('Cycle in prototype tree')
                seen.add(nid)
                todo.extend(children.get(nid, []))
            if seen != set(by_id):
                raise PrototypeError('Disconnected prototype tree')
            if 'intake' in p:
                from .intake import validate_metadata
                validate_metadata(p['intake'])
            proposal = p.get('proposal')
            if proposal is not None:
                _id(proposal['id'])
                if proposal['node_id'] not in by_id or proposal['revision'] != p['revision']:
                    raise PrototypeError('Invalid decomposition proposal')
                if not isinstance(proposal['suggestions'], list) or not 1 <= len(proposal['suggestions']) <= 10:
                    raise PrototypeError('Invalid decomposition suggestions')
                for title in proposal['suggestions']:
                    _text(title, True)
        except (KeyError, TypeError, AttributeError) as exc:
            raise PrototypeError('Invalid prototype schema; original file preserved') from exc

    def _write(self, directory, project):
        self._validate(project, project['id'])
        data = json.dumps(project, ensure_ascii=False, separators=(',', ':')).encode()
        if len(data) > LIMITS['max_file_bytes']:
            raise PrototypeError('Prototype file size limit reached')
        target = _id(project['id']) + '.json'
        try:
            if stat.S_ISLNK(os.stat(target, dir_fd=directory, follow_symlinks=False).st_mode):
                raise PrototypeError('Symlink prototype files are not allowed')
        except FileNotFoundError:
            pass
        tmp = '.' + uuid.uuid4().hex + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(tmp, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _create(self, directory, title, project_id=None, *, example=False):
        if len([n for n in os.listdir(directory) if n.endswith('.json')]) >= LIMITS['max_projects']:
            raise PrototypeError('Prototype project limit reached')
        root = _node(title)
        p = dict(id=project_id or uuid.uuid4().hex, title=root['title'], revision=0,
                 root_id=root['id'], nodes=[root], proposal=None,
                 illustrative_only=True, limits=dict(LIMITS))
        if example:
            root['description'] = '可编辑原型示例，用于演示需求、拆解、监督与验收；未执行任何任务。'
            packages = [
                ('界面体验', '让大目标、任务层级和当前状态便于查看。', [
                    ('梳理三栏工作台', '演示需求列表、递归脑图与节点详情的布局。'),
                    ('完善节点交互', '演示添加、改名、折叠和删除子树。')]),
                ('任务拆解', '将目标递归细分为可说明、可验证的工作项。', [
                    ('明确目标与边界', '记录要解决的问题、约束和不做的事项。'),
                    ('审核拆解建议', '先查看固定示例建议，明确确认后才加入子节点。')]),
                ('执行监督', '演示分工、进度与自报信息，不启动真实执行器。', [
                    ('记录 Agent 分工', '填写示意负责人及任务说明，不调用 Agent。'),
                    ('跟踪进度与风险', '人工编辑示意进度和自报；不代表真实执行结果。')]),
                ('独立验收', '将 Agent 自报与独立验收分开呈现。', [
                    ('定义验收条件', '说明需要检查的结果与证据；当前尚无执行证据。'),
                    ('记录独立复核', '人工记录模拟验收意见，不把自报完成视为通过。')]),
            ]
            for title, description, tasks in packages:
                package = _node(title, root['id'])
                package['description'] = description
                p['nodes'].append(package)
                for task_title, task_description in tasks:
                    task = _node(task_title, package['id'])
                    task['description'] = task_description
                    p['nodes'].append(task)
            for node in p['nodes']:
                node['status'] = '原型未执行'
                node['agent_report'] = '仅为可编辑原型示例，未运行 Agent。'
                node['acceptance'] = '模拟验收：未验收'
        self._write(directory, p)
        return p

    def list_projects(self):
        with self._locked() as directory:
            names = sorted(n for n in os.listdir(directory) if n.endswith('.json'))
            if not names:
                self._create(directory, 'Nori 自我迭代', 'nori-example', example=True)
                names = ['nori-example.json']
            result = []
            for name in names:
                p = self._read(directory, name[:-5])
                summary = {k: p[k] for k in ('id', 'title', 'revision', 'root_id', 'illustrative_only')}
                summary['intake'] = 'intake' in p
                result.append(summary)
            return result

    def create_project(self, title):
        with self._locked() as directory:
            return self._create(directory, title)

    def import_intake(self, raw_text, *, confirmed=False, digest=None, command_id=None):
        """Atomically save an explicitly confirmed draft, never a kernel spec.

        Canonical-document identity prevents duplicate imports across processes
        and restarts. Only the initial create command ID is retained. Provenance
        and the editable graph live in one record.
        """
        from .intake import IntakeError, preview_intake, validate_command_id
        preview = preview_intake(raw_text)
        if confirmed is not True or digest != preview['digest']:
            raise IntakeError('请先预览当前 JSON，再明确确认导入；内容变化后需要重新预览')
        validate_command_id(command_id)
        with self._locked() as directory:
            names = sorted(n for n in os.listdir(directory) if n.endswith('.json'))
            existing = None
            for name in names:
                project = self._read(directory, name[:-5])
                provenance = project.get('intake')
                if not provenance:
                    continue
                if provenance['command_id'] == command_id and provenance['digest'] != digest:
                    raise IntakeError('此请求标识已用于其他内容，请重新预览后导入')
                if provenance['digest'] == digest:
                    existing = project
            if existing is not None:
                return copy.deepcopy(existing)
            if len(names) >= LIMITS['max_projects']:
                raise IntakeError('Prototype project limit reached')
            doc = preview['document']
            mapping = {n['id']: uuid.uuid4().hex for n in doc['nodes']}
            nodes = []
            for source in doc['nodes']:
                node = _node(source['title'], mapping.get(source['parent_id']))
                node['id'] = mapping[source['id']]
                node['description'] = source['description']
                node['status'] = '需求草稿：未执行'
                node['acceptance'] = '未验收'
                # Criteria describe future checks, never an acceptance result.
                node['acceptance_criteria'] = copy.deepcopy(source['acceptance_criteria'])
                nodes.append(node)
            root = next(n['id'] for n in nodes if n['parent_id'] is None)
            project = dict(id=uuid.uuid4().hex, title=doc['title'], revision=0,
                           root_id=root, nodes=nodes, proposal=None,
                           illustrative_only=True, limits=dict(LIMITS),
                           intake=dict(raw_text=raw_text, digest=digest,
                                       document=copy.deepcopy(doc), id_map=mapping,
                                       command_id=command_id,
                                       approval_status='UNAPPROVED',
                                       execution_status='UNEXECUTED'))
            self._write(directory, project)
            return copy.deepcopy(project)

    def get_project(self, project_id):
        with self._locked() as directory:
            return self._read(directory, project_id)

    def mutate(self, project_id, action, *, expected_revision, node_id=None,
               proposal_id=None, confirmed=False, **fields):
        with self._locked() as directory:
            p = self._read(directory, project_id)
            if type(expected_revision) is not int or expected_revision != p['revision']:
                raise RevisionConflict('Prototype changed; reload before confirming or editing')
            by_id = {n['id']: n for n in p['nodes']}
            _id(node_id)
            if node_id not in by_id:
                raise PrototypeError('Prototype node not found')
            n = by_id[node_id]
            prior_proposal = p.get('proposal')
            p['proposal'] = None  # Any edit invalidates a pending approval.
            if action in ('add_child', 'add_sibling'):
                if set(fields) != {'title'}:
                    raise PrototypeError('A title is required')
                parent = node_id if action == 'add_child' else n['parent_id']
                if parent is None:
                    raise PrototypeError('The root cannot have siblings')
                p['nodes'].append(_node(fields['title'], parent))
            elif action in ('rename', 'update'):
                allowed = {'title'} if action == 'rename' else {'title', 'description', 'agent', 'progress', 'status', 'agent_report', 'acceptance', 'collapsed', 'acceptance_criteria'}
                if not fields or not set(fields) <= allowed:
                    raise PrototypeError('Invalid prototype fields')
                for key, value in fields.items():
                    if key == 'acceptance_criteria':
                        from .intake import _strings
                        _strings(value)
                        value = copy.deepcopy(value)
                    elif key == 'progress':
                        if type(value) is not int or not 0 <= value <= 100:
                            raise PrototypeError('Progress must be an integer from 0 to 100')
                    elif key == 'collapsed':
                        if type(value) is not bool:
                            raise PrototypeError('Collapsed must be boolean')
                    else:
                        value = _text(value, key == 'title')
                    n[key] = value
                if node_id == p['root_id']:
                    p['title'] = n['title']
            elif action == 'delete':
                if fields or confirmed is not True:
                    raise PrototypeError('Confirm subtree deletion explicitly')
                if node_id == p['root_id']:
                    raise PrototypeError('The root cannot be deleted')
                children = {}
                for child in p['nodes']:
                    children.setdefault(child['parent_id'], []).append(child['id'])
                removed, todo = set(), [node_id]
                while todo:
                    item = todo.pop()
                    removed.add(item)
                    todo.extend(children.get(item, []))
                p['nodes'] = [child for child in p['nodes'] if child['id'] not in removed]
            elif action == 'propose_decomposition':
                if fields:
                    raise PrototypeError('Unexpected proposal fields')
                if any(child['parent_id'] == node_id for child in p['nodes']):
                    raise PrototypeError('Node already has children; example decomposition will not replace them')
                p['proposal'] = dict(id=uuid.uuid4().hex, node_id=node_id,
                    revision=p['revision'] + 1,
                    suggestions=['明确目标与验收条件', '设计最小可验证改动', '独立验证并记录结果'])
            elif action == 'confirm_decomposition':
                if fields or not prior_proposal or proposal_id != prior_proposal['id'] or node_id != prior_proposal['node_id'] or prior_proposal['revision'] != p['revision']:
                    raise RevisionConflict('Proposal missing or stale; generate and review a new proposal')
                if any(child['parent_id'] == node_id for child in p['nodes']):
                    raise PrototypeError('Existing children cannot be replaced')
                p['nodes'].extend(_node(title, node_id) for title in prior_proposal['suggestions'])
            else:
                raise PrototypeError('Unknown prototype action')
            p['revision'] += 1
            self._write(directory, p)
            return copy.deepcopy(p)
