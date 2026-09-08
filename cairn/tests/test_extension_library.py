import asyncio
import json
from types import SimpleNamespace

from test_extensions import client, skill, mcp_server
from cairn.extensions.config import KEEP_SECRET
from cairn.extensions.library import initialize, link_trace, fact_trace
from cairn.extensions.mcp_client import MCPConnections
from cairn.extensions.trace import collect, current, attach
from cairn.server import db


def agent(client, name):
    result = client.post('/agents', json={'name': name, 'base_url': 'http://model.test', 'api_key': 'key', 'model': 'same-model'})
    assert result.status_code == 201
    return result.json()['id']


def test_library_shared_assignments_revocation_and_masked_update(client, mcp_server):
    a, b = agent(client, 'a'), agent(client, 'b')
    body = {'name': 'Shared echo', 'kind': 'mcp', 'config': mcp_server['echo'], 'agent_ids': [a,b]}
    result = client.post('/extensions', json=body)
    assert result.status_code == 201, result.text
    saved = result.json()
    assert saved['config']['env']['TEST_TOKEN'] == KEEP_SECRET
    assert 'private-test-token' not in client.get('/extensions').text
    runtime = client.get('/agents/runtime').json()
    assert all(saved['id'] in row['mcp_servers'] for row in runtime)
    assert len(client.get('/extensions').json()) == 1
    saved['agent_ids'] = [a]
    assert client.put('/extensions/' + saved['id'], json=saved).status_code == 200
    runtime = {row['id']: row for row in client.get('/agents/runtime').json()}
    assert runtime[a]['mcp_servers'][saved['id']]['env']['TEST_TOKEN'] == 'private-test-token'
    assert not runtime[b]['mcp_servers']
    assert client.post('/extensions/' + saved['id'] + '/test').status_code == 200
    assert client.delete('/extensions/' + saved['id']).status_code == 204
    assert not any(row['mcp_servers'] for row in client.get('/agents/runtime').json())


def test_skill_assignment_validation_and_legacy_migration(client, skill):
    a, b = agent(client, 'a'), agent(client, 'b')
    body = {'name': 'sample', 'kind': 'skill', 'config': {'path': str(skill)}, 'agent_ids': ['missing']}
    assert client.post('/extensions', json=body).status_code == 422
    with db.get_conn() as conn:
        conn.execute('UPDATE agents SET skill_paths=?', (json.dumps([str(skill/'SKILL.md')]),))
        initialize(conn)
        initialize(conn)
    saved = client.get('/extensions').json()
    assert len(saved) == 1
    assert set(saved[0]['agent_ids']) == {a,b}
    assert all(row['skill_paths'] == [str(skill/'SKILL.md')] for row in client.get('/agents/runtime').json())
    assert client.post('/extensions/' + saved[0]['id'] + '/test').status_code == 200


def test_observed_skill_read_and_mcp_call_are_recorded(tmp_path, skill, mcp_server, monkeypatch):
    path = tmp_path/'trace.jsonl'
    monkeypatch.setenv('CAIRN_EXTENSION_TRACE', str(path))
    monkeypatch.setenv('CAIRN_SKILL_MANIFEST', json.dumps([{'name': 'sample', 'path': str(skill/'SKILL.md')}]))
    async def run():
        async with MCPConnections(mcp_server) as clients:
            tools = await clients.list_tools()
            assert not path.exists(), 'Discovery is not usage'
            result = await clients.call_tool('cairn_read_skill', {'name': 'sample'})
            assert 'Use the MCP echo tool' in result.content[0].text
            echo = next(tool for tool in tools if tool.name != 'cairn_read_skill')
            await clients.call_tool(echo.name, {'text': 'sensitive-result-not-logged'})
    asyncio.run(run())
    text = path.read_text()
    assert 'sensitive-result' not in text and 'private-test-token' not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert [e['kind'] for e in events] == ['skill', 'mcp']


def test_trace_persistence_and_project_isolation(client):
    p = client.post('/projects', json={'title':'trace', 'origin':'start', 'goal':'finish'}).json()['project']['id']
    body = {'run_id':'run-one', 'agent_id':'model-one', 'events':[{'kind':'mcp', 'name':'echo','tool':'echo','status':'success'}]}
    assert client.put(f'/projects/{p}/facts/origin/extensions', json=body).status_code == 200
    facts = client.get(f'/projects/{p}').json()['facts']
    assert next(f for f in facts if f['id']=='origin')['extension_trace'] == body
    assert next(f for f in facts if f['id']=='goal').get('extension_trace') is None
    assert client.put('/projects/missing/facts/origin/extensions', json=body).status_code == 404
    changed = {**body, 'events':[]}
    client.put(f'/projects/{p}/facts/origin/extensions', json=changed)
    assert next(f for f in client.get(f'/projects/{p}').json()['facts'] if f['id']=='origin')['extension_trace'] == body


def test_trace_scope_keeps_execute_and_conclude_but_not_next_task(tmp_path):
    token = current.set(None)
    try:
        path = tmp_path/'trace'
        event = {'kind':'mcp','name':'echo','tool':'echo','status':'success'}
        path.write_text(json.dumps(event)+'\n')
        process = SimpleNamespace(env={'CAIRN_EXTENSION_TRACE':str(path)})
        worker = SimpleNamespace(env={'CAIRN_TRACE_SCOPE':'one','CAIRN_PROJECT_ID':'p'}, name='a')
        collect(process, worker)
        collect(SimpleNamespace(env={}), worker)
        assert current.get()['events'] == [event]
        captured=[]
        response=SimpleNamespace(raise_for_status=lambda:None)
        session=SimpleNamespace(put=lambda url,**kw: (captured.append((url,kw)) or response))
        client=SimpleNamespace(_session=lambda:session,_url=lambda x:x,_timeout=1)
        attach(client,'/projects/other/intents/i/conclude',{'fact':{'id':'f'}})
        assert not captured
        attach(client,'/projects/p/intents/i/conclude',{'fact':{'id':'f'}})
        assert captured[0][0]=='/projects/p/facts/f/extensions'
        worker.env['CAIRN_TRACE_SCOPE']='two'
        collect(SimpleNamespace(env={}),worker)
        assert current.get()['events']==[]
    finally:
        current.reset(token)


def test_skill_only_native_workers_enable_the_reader_bridge(tmp_path, skill):
    from cairn.extensions.runtime import prepare_extensions
    for binary in ['codex', 'claude']:
        workspace=tmp_path/binary
        workspace.mkdir()
        env={'CAIRN_EXTENSIONS_JSON': json.dumps({'skill_paths':[str(skill)], 'mcp_servers':{}})}
        command,_=prepare_extensions(str(workspace),env,[binary,'exec','--','prompt'],None)
        assert 'cairn_read_skill' in command[-1]
        assert 'cairn.extensions.mcp_proxy' in ' '.join(command) if binary=='codex' else '--mcp-config' in command


def test_source_trace_link_resolves_after_source_fact_trace_arrives(client):
    p = client.post('/projects', json={'title':'source', 'origin':'start', 'goal':'finish'}).json()['project']['id']
    with db.get_conn() as conn:
        link_trace(conn, p, 'goal', p, 'origin')
        assert fact_trace(conn,p,'goal') is None
    body = {'run_id':'run', 'events':[{'kind':'skill','name':'sample','tool':'read','status':'read'}]}
    assert client.put(f'/projects/{p}/facts/origin/extensions',json=body).status_code==200
    with db.get_conn() as conn:
        trace=fact_trace(conn,p,'goal')
        assert trace['inherited'] is True
        assert trace['events'][0]['source']==f'{p}/origin'
        # Cycles from malformed imports never recurse indefinitely.
        link_trace(conn,p,'origin',p,'goal')
        assert fact_trace(conn,p,'goal')['events'][0]['name']=='sample'


def test_protocol_conclude_attaches_collected_trace(client, monkeypatch):
    from cairn.dispatcher.protocol.client import CairnClient
    p=client.post('/projects',json={'title':'workflow','origin':'start','goal':'finish'}).json()['project']['id']
    intent=client.post(f'/projects/{p}/intents',json={'from':['origin'],'description':'check','creator':'worker'}).json()['id']
    assert client.post(f'/projects/{p}/intents/{intent}/heartbeat',json={'worker':'worker'}).status_code==200
    runtime=CairnClient('http://testserver')
    monkeypatch.setattr(runtime,'_session',lambda:client)
    token=current.set({'project_id':p,'run_id':'scope','agent_id':'model','events':[{'kind':'mcp','name':'echo','tool':'echo','status':'success'}]})
    try:
        response=runtime.conclude(p,intent,'worker','checked')
        assert response.ok
        fid=response.data['fact']['id']
        fact=next(f for f in client.get(f'/projects/{p}').json()['facts'] if f['id']==fid)
        assert fact['extension_trace']['events'][0]['name']=='echo'
    finally:
        current.reset(token)
