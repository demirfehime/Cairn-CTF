"""Record observed calls only; never record tool arguments, results or secrets."""
from contextvars import ContextVar
import json
import os
from pathlib import Path

current = ContextVar('extension_trace', default=None)


def record(kind, name, tool, status):
    path = os.environ.get('CAIRN_EXTENSION_TRACE')
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind': kind, 'name': name, 'tool': tool, 'status': status}) + '\n')


def collect(process, worker):
    env = getattr(process, 'env', {})
    path = env.get('CAIRN_EXTENSION_TRACE')
    scope = worker.env.get('CAIRN_TRACE_SCOPE', '')
    previous = current.get()
    current.set(None)
    events = list(previous['events']) if previous and previous.get('scope') == scope and scope else []
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            try:
                event = json.loads(line)
                if event not in events:
                    events.append(event)
            except ValueError:
                continue
    current.set({'scope': scope, 'project_id': worker.env.get('CAIRN_PROJECT_ID'), 'run_id': scope,
                 'agent_id': worker.env.get('CAIRN_AGENT_ID', worker.name), 'events': events[:1000]})


def attach(client, path, data):
    trace = current.get()
    if not trace or not trace['run_id'] or not trace['project_id'] or not isinstance(data, dict):
        return
    if not path.startswith(f"/projects/{trace['project_id']}/"):
        return
    fact_id = data['fact'].get('id') if isinstance(data.get('fact'), dict) else None
    if path.endswith('/complete') and data.get('to') == 'goal':
        fact_id = 'goal'
    if not fact_id:
        return
    payload = {key: trace[key] for key in ['run_id', 'agent_id', 'events']}
    response = client._session().put(client._url(f"/projects/{trace['project_id']}/facts/{fact_id}/extensions"), json=payload, timeout=client._timeout)
    response.raise_for_status()
