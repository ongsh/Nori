"""Strict, non-executable external requirement documents.

Parsing and preview are pure: no directories, projects, agents, or commands.
A requirement hierarchy is never interpreted as execution dependencies.
"""
import hashlib
import json
import re

from .prototype import PrototypeError

LIMITS = {'max_bytes': 1024 * 1024, 'max_nodes': 2000,
          'max_title_chars': 200, 'max_text_chars': 10000,
          'max_list_items': 200}
_KEYS = {'schema_version', 'title', 'original_request', 'goal', 'constraints',
         'out_of_scope', 'open_questions', 'nodes'}
_NODE_KEYS = {'id', 'parent_id', 'title', 'description', 'acceptance_criteria'}
_EXTERNAL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
STANDARD_TEMPLATE = {
    'schema_version': 1, 'title': '需求标题', 'original_request': '逐字保留用户原始需求',
    'goal': '期望达成的目标', 'constraints': [], 'out_of_scope': [],
    'open_questions': [], 'nodes': [
        {'id': 'root', 'parent_id': None, 'title': '需求标题',
         'description': '需求说明', 'acceptance_criteria': []}]}
ROLE_PROMPT = '''你是一位资深产品需求分析师，帮助用户把想法澄清为可评审的需求。先复述理解，再只提出确实影响需求的问题；逐轮整理目标、约束、范围、验收标准和未决问题。把验收标准写成用户可以观察或检查的具体结果，而不是“完善”“优化”等含糊承诺。不要代替用户决定产品功能、工具、预算或实施路径，也不要编造事实。用户明确确认需求内容并要求导出后，才仅输出标准 JSON，不要 Markdown 围栏。
使用 schema_version: 1，顶层字段严格为 title、original_request、goal、constraints、out_of_scope、open_questions、nodes 以及 schema_version。
original_request 必须逐字保留用户原始需求，不得改写。后三个列表为字符串数组。nodes 是需求脑图，不是执行计划，必须有且仅有一个 parent_id 为 null 的根节点。
每个节点仅包含 id、parent_id、title、description、acceptance_criteria；id 是稳定唯一的 1–64 位字母数字、下划线、点或短横线标识（首字符为字母数字），parent_id 引用父节点 id，acceptance_criteria 是字符串数组。
不要虚构事实；不确定内容列入 open_questions。不得输出命令、操作、执行依赖、路径规划、状态、进度、批准、执行证据或 run_id。父子关系只表示需求拆分。自然语言验收标准不表示已经验收通过。
输出会被 Nori 严格验证，预览并由用户明确确认后保存为未批准、未执行的可编辑需求脑图，不会启动 AI 或执行任务。
限制：UTF-8 JSON 最多 1 MiB，节点最多 2000，标题最多 200 字符且非空，文本最多 10000 字符，每个字符串数组最多 200 项。'''

_EXAMPLE = {
    'schema_version': 1, 'title': '个人阅读清单',
    'original_request': '我想记下待读书籍，并标记哪些已经读完。只在本机使用，不需要账号。',
    'goal': '查看待读书籍并区分已读书籍',
    'constraints': ['只在本机使用', '不需要账号'],
    'out_of_scope': ['多人共享', '在线同步'],
    'open_questions': ['是否需要按作者搜索？'],
    'nodes': [
        {'id': 'reading-list', 'parent_id': None, 'title': '管理个人阅读清单',
         'description': '记录书名，并保留用户设置的阅读状态。',
         'acceptance_criteria': ['用户可在同一清单中看到书名及对应阅读状态。']},
        {'id': 'mark-read', 'parent_id': 'reading-list', 'title': '标记已读',
         'description': '用户可将一项待读书籍标记为已读。',
         'acceptance_criteria': ['把一本待读书籍标记为已读后，该书状态显示为已读。']}
    ]}
ROLE_PROMPT += '\n以下是虚构的格式示例，不是用户已确认的需求，请用实际讨论确认的内容替换：\n' + json.dumps(_EXAMPLE, ensure_ascii=False, indent=2)


class IntakeError(PrototypeError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntakeError('JSON 字段重复，请删除重复字段：' + key)
        result[key] = value
    return result


def _text(value, title=False):
    limit = LIMITS['max_title_chars' if title else 'max_text_chars']
    if not isinstance(value, str) or len(value) > limit or (title and not value.strip()):
        raise IntakeError('需求文本类型不正确或超过长度限制，请检查标题和正文')
    # Reject escaped lone surrogates as well as invalid UTF-8 input.
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise IntakeError('需求文本包含无效字符，请使用有效 UTF-8 文本') from exc


def _strings(value):
    if not isinstance(value, list) or len(value) > LIMITS['max_list_items']:
        raise IntakeError('需求列表必须是字符串数组，每项最多 10000 字符、每个数组最多 200 项')
    for item in value:
        _text(item)


def _identifier(value):
    if not isinstance(value, str) or not _EXTERNAL_ID.fullmatch(value):
        raise IntakeError('节点标识无效：请使用 1–64 位字母数字、下划线、点或短横线，首字符须为字母数字')


def _check_field(label, validator, *values):
    try:
        validator(*values)
    except IntakeError as exc:
        raise IntakeError(label + '：' + str(exc)) from exc


def preview_intake(raw_text):
    """Validate raw text without writing anything; preserve all strings exactly."""
    if not isinstance(raw_text, str):
        raise IntakeError('请提供 UTF-8 编码的需求 JSON 文本')
    try:
        raw_bytes = raw_text.encode('utf-8')
    except UnicodeError as exc:
        raise IntakeError('请提供 UTF-8 编码的需求 JSON 文本') from exc
    if not raw_bytes or len(raw_bytes) > LIMITS['max_bytes']:
        raise IntakeError('需求 JSON 不能为空且不能超过 1 MiB')
    try:
        doc = json.loads(raw_text, object_pairs_hook=_pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(IntakeError('JSON 不允许 NaN 或 Infinity')))
    except json.JSONDecodeError as exc:
        raise IntakeError(f'JSON 第 {exc.lineno} 行、第 {exc.colno} 列格式错误：{exc.msg}。请只粘贴 JSON，不要 Markdown 围栏。') from exc
    except (ValueError, RecursionError) as exc:
        raise IntakeError('需求 JSON 格式无效，请检查括号、引号和重复字段：' + str(exc)) from exc
    if not isinstance(doc, dict) or set(doc) != _KEYS:
        raise IntakeError('顶层字段必须与版本 1 模板完全一致，不能缺少或增加字段；这里只导入需求，不接受执行/批准状态')
    if type(doc['schema_version']) is not int or doc['schema_version'] != 1:
        raise IntakeError('只支持整数 schema_version: 1，请使用当前模板')
    _check_field('title', _text, doc['title'], True)
    for key in ('original_request', 'goal'):
        _check_field(key, _text, doc[key])
    for key in ('constraints', 'out_of_scope', 'open_questions'):
        _check_field(key, _strings, doc[key])
    nodes = doc['nodes']
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= LIMITS['max_nodes']:
        raise IntakeError('nodes 必须包含 1–2000 个节点')
    by_id, children, roots = {}, {}, []
    for node in nodes:
        if not isinstance(node, dict) or set(node) != _NODE_KEYS:
            raise IntakeError('节点字段必须与模板完全一致：id、parent_id、title、description、acceptance_criteria；这里只导入需求，不接受执行/批准状态')
        nid, parent = node['id'], node['parent_id']
        _identifier(nid)
        if nid in by_id:
            raise IntakeError('节点 id 重复，请为每个节点设置唯一标识')
        by_id[nid] = node
        if parent is None:
            roots.append(nid)
        else:
            _identifier(parent)
        children.setdefault(parent, []).append(nid)
        _check_field(f'节点 {nid} 的 title', _text, node['title'], True)
        _check_field(f'节点 {nid} 的 description', _text, node['description'])
        _check_field(f'节点 {nid} 的 acceptance_criteria', _strings, node['acceptance_criteria'])
    if len(roots) != 1:
        raise IntakeError('脑图必须且只能有一个 parent_id 为 null 的根节点')
    for node in nodes:
        if node['parent_id'] is not None and node['parent_id'] not in by_id:
            raise IntakeError('父节点不存在，请检查 parent_id 是否引用有效节点 id')
    seen, todo = set(), roots[:]
    while todo:
        nid = todo.pop()
        if nid in seen:
            raise IntakeError('脑图存在循环，请修正节点父子关系')
        seen.add(nid)
        todo.extend(children.get(nid, []))
    if seen != set(by_id):
        raise IntakeError('脑图存在断开的节点或循环，所有节点必须连接到唯一根节点')
    return {'digest': hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(), 'document': doc,
            'node_count': len(nodes), 'limits': dict(LIMITS),
            'approval_status': 'UNAPPROVED', 'execution_status': 'UNEXECUTED'}


def validate_metadata(metadata):
    """Validate persisted provenance independently of the editable tree."""
    keys = {'raw_text', 'digest', 'document', 'id_map', 'command_id',
            'approval_status', 'execution_status'}
    if not isinstance(metadata, dict) or set(metadata) != keys:
        raise IntakeError('保存的需求来源结构无效，原文件已保留')
    preview = preview_intake(metadata['raw_text'])
    if metadata['digest'] != preview['digest'] or metadata['document'] != preview['document']:
        raise IntakeError('保存的需求来源与原始 JSON 不一致，原文件已保留')
    if metadata['approval_status'] != 'UNAPPROVED' or metadata['execution_status'] != 'UNEXECUTED':
        raise IntakeError('导入需求不能授予批准或执行状态')
    validate_command_id(metadata['command_id'])
    mapping = metadata['id_map']
    if not isinstance(mapping, dict) or set(mapping) != {n['id'] for n in preview['document']['nodes']}:
        raise IntakeError('保存的外部节点映射无效')
    if any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{32}', v) for v in mapping.values()):
        raise IntakeError('保存的本地节点映射无效')
    if len(set(mapping.values())) != len(mapping):
        raise IntakeError('保存的本地节点映射重复')


def validate_command_id(command_id):
    if not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', command_id):
        raise IntakeError('导入请求标识无效，请重新预览后再确认')
