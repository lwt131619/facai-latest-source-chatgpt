"""Validate the handoff direction registry, dependencies and required sources."""
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ['AGENTS.md', 'FACAI_CODEX_HANDOFF.md', 'AGENT_HANDOFF.md',
            'DEVELOPMENT_PLAN.md', 'docs/engineering/CORE_INVARIANTS.md',
            'docs/engineering/CORE_CONTRACTS.md', 'docs/engineering/REPOSITORY_MAP.md',
            'docs/engineering/REQUIREMENTS_TRACEABILITY.md',
            'docs/engineering/ACCEPTANCE_MATRIX.md',
            'docs/engineering/ARCHITECTURE_DECISIONS.md',
            'docs/engineering/RESUME_RUNBOOK.md']


def render(tasks):
    lines = ['# 需求—源码—任务—验收对照', '',
             '由 task-registry.json 生成；状态是证据范围，不是完成百分比。', '',
             '| ID | 方向 | 状态 | 优先级 | 依赖 |', '|---|---|---|---|---|']
    for task in tasks:
        lines.append('| '+task['id']+' | '+task['title']+' | '+task['status']
                     +' | P'+str(task['priority'])+' | '+', '.join(task['depends_on'])+' |')
    for task in tasks:
        lines += ['', '## '+task['id']+' — '+task['title'], '', task['goal'], '',
                  '**现有定位**：'+'、'.join('`'+f+'`' for f in task['source_files']),
                  '', '**验收**：', '']
        lines += ['- '+a for a in task['acceptance']]
        lines += ['', '**下一步**：'+task['next_action']]
    return '\n'.join(lines)+'\n'


def validate(update=False):
    registry = json.loads((ROOT/'docs/engineering/task-registry.json').read_text())
    tasks = registry['tasks']
    ids = {t['id'] for t in tasks}
    assert len(tasks) == len(ids), 'Duplicate task IDs'
    definitions = registry['status_definitions']
    graph = {}
    for task in tasks:
        assert task['status'] in definitions, task['id']
        assert task['goal'] and task['next_action'] and task['acceptance'], task['id']
        assert task['source_files'], task['id']
        assert set(task['depends_on']) <= ids, task['id']
        graph[task['id']] = task['depends_on']
        for name in task['source_files']:
            path = (ROOT/name).resolve()
            assert path.is_relative_to(ROOT) and path.is_file(), name
    visited = set()
    def visit(id, active):
        assert id not in active, 'Dependency cycle: '+id
        if id in visited:
            return
        for dep in graph[id]:
            visit(dep, active | {id})
        visited.add(id)
    for id in graph:
        visit(id, set())
    for name in REQUIRED:
        assert (ROOT/name).is_file(), name
    trace = ROOT/'docs/engineering/REQUIREMENTS_TRACEABILITY.md'
    if update:
        trace.write_text(render(tasks))
    assert trace.read_text() == render(tasks), 'Task documentation is stale; run with --render'
    # Completing a local regression must not silently certify production acceptance.
    if registry['last_full_regression']['production_acceptance']:
        final = next(t for t in tasks if t['id'] == 'ACCEPT-001')
        assert final['status'] == 'VERIFIED_PRODUCTION', 'Final acceptance evidence is required'
    print(f'Handoff validation: {len(tasks)} directions, no cycles, all required files present')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render', action='store_true', help='Regenerate the readable task inventory')
    validate(parser.parse_args().render)
