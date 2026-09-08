import asyncio
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from cairn.extensions.config import MCPServer, merge_mcp, public_mcp, validate_skill_paths
from cairn.extensions.mcp_client import MCPConnections
from cairn.server.db import get_conn

router = APIRouter(tags=['extensions'])


class ExtensionInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['skill', 'mcp']
    config: dict
    agent_ids: list[str] = Field(default_factory=list, max_length=500)


def public(row):
    data = dict(row)
    data['config'] = json.loads(data['config'])
    data['agent_ids'] = json.loads(data['agent_ids'])
    if data['kind'] == 'mcp':
        data['config'] = public_mcp({'server': data['config']})['server']
    return data


@router.get('/extensions')
def list_extensions():
    with get_conn() as conn:
        return [public(row) for row in conn.execute('SELECT * FROM extension_library ORDER BY name,id')]


def save(body, extension_id=None):
    with get_conn() as conn:
        old = conn.execute('SELECT * FROM extension_library WHERE id=?', (extension_id,)).fetchone()
        if extension_id and old is None:
            raise HTTPException(404, 'Extension not found')
        known = {row['id'] for row in conn.execute('SELECT id FROM agents')}
        if not set(body.agent_ids) <= known:
            raise HTTPException(422, 'Unknown model Agent assignment')
        if old and old['kind'] != body.kind:
            raise HTTPException(422, 'Create a new entry to change extension kind')
        try:
            if body.kind == 'skill':
                config = {'path': validate_skill_paths([body.config.get('path', '')])[0]}
            else:
                config = MCPServer.model_validate(body.config).model_dump()
                previous = json.loads(old['config']) if old else {}
                config = merge_mcp({'server': config}, {'server': previous})['server']
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(422, 'Invalid extension configuration: check Skill path or MCP fields') from exc
        key = extension_id or 'ext_' + uuid4().hex[:20]
        conn.execute('INSERT OR REPLACE INTO extension_library VALUES (?,?,?,?,?)', (key, body.name.strip(), body.kind, json.dumps(config), json.dumps(list(dict.fromkeys(body.agent_ids)))))
        return public(conn.execute('SELECT * FROM extension_library WHERE id=?', (key,)).fetchone())


@router.post('/extensions', status_code=201)
def create(body: ExtensionInput):
    return save(body)


@router.put('/extensions/{extension_id}')
def update(extension_id: str, body: ExtensionInput):
    return save(body, extension_id)


@router.delete('/extensions/{extension_id}', status_code=204)
def delete(extension_id: str):
    with get_conn() as conn:
        if not conn.execute('DELETE FROM extension_library WHERE id=?', (extension_id,)).rowcount:
            raise HTTPException(404, 'Extension not found')


@router.post('/extensions/{extension_id}/test')
async def test(extension_id: str):
    with get_conn() as conn:
        row = conn.execute('SELECT * FROM extension_library WHERE id=?', (extension_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Extension not found')
    config = json.loads(row['config'])
    try:
        if row['kind'] == 'skill':
            return {'ok': True, 'skills': validate_skill_paths([config['path']]), 'tools': []}
        async with asyncio.timeout(45):
            async with MCPConnections({extension_id: config}) as client:
                tools = await client.list_tools()
        return {'ok': True, 'skills': [], 'tools': [tool.name for tool in tools]}
    except Exception as exc:
        raise HTTPException(502, f'Extension check failed ({type(exc).__name__})') from exc


class TraceEvent(BaseModel):
    kind: Literal['skill', 'mcp']
    name: str = Field(max_length=200)
    tool: str = Field(default='', max_length=200)
    status: Literal['read', 'success', 'error']


class FactTrace(BaseModel):
    run_id: str = Field(max_length=64)
    agent_id: str = Field(default='', max_length=100)
    events: list[TraceEvent] = Field(default_factory=list, max_length=1000)


@router.put('/projects/{project_id}/facts/{fact_id}/extensions')
def attach_trace(project_id: str, fact_id: str, body: FactTrace):
    with get_conn() as conn:
        if not conn.execute('SELECT 1 FROM facts WHERE project_id=? AND id=?', (project_id, fact_id)).fetchone():
            raise HTTPException(404, 'Fact not found')
        conn.execute('INSERT OR IGNORE INTO fact_extensions VALUES (?,?,?)', (project_id, fact_id, body.model_dump_json()))
    return {'ok': True}
