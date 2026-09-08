"""Shared extension definitions and model assignments; legacy configuration stays readable."""
import hashlib
import json


def initialize(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS extension_library (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL, agent_ids TEXT NOT NULL DEFAULT '[]')")
    conn.execute("CREATE TABLE IF NOT EXISTS fact_extensions (project_id TEXT NOT NULL, fact_id TEXT NOT NULL, trace TEXT NOT NULL, PRIMARY KEY(project_id,fact_id), FOREIGN KEY(project_id,fact_id) REFERENCES facts(project_id,id) ON DELETE CASCADE)")
    conn.execute("CREATE TABLE IF NOT EXISTS fact_extension_sources (project_id TEXT NOT NULL, fact_id TEXT NOT NULL, source_project_id TEXT NOT NULL, source_fact_id TEXT NOT NULL, PRIMARY KEY(project_id,fact_id,source_project_id,source_fact_id), FOREIGN KEY(project_id,fact_id) REFERENCES facts(project_id,id) ON DELETE CASCADE, FOREIGN KEY(source_project_id,source_fact_id) REFERENCES facts(project_id,id) ON DELETE CASCADE)")
    # Move existing per-Agent definitions once; matching configurations are shared.
    for agent in conn.execute("SELECT id,skill_paths,mcp_servers FROM agents").fetchall():
        entries = [(str(path).replace('\\', '/').split('/')[-2] if str(path).endswith('SKILL.md') else str(path).replace('\\', '/').split('/')[-1], 'skill', {'path': path}) for path in json.loads(agent['skill_paths'])]
        entries += [(name, 'mcp', config) for name, config in json.loads(agent['mcp_servers']).items()]
        for name, kind, config in entries:
            encoded = json.dumps(config, sort_keys=True)
            key = 'ext_' + hashlib.sha256((kind + name + encoded).encode()).hexdigest()[:20]
            row = conn.execute('SELECT agent_ids FROM extension_library WHERE id=?', (key,)).fetchone()
            ids = json.loads(row['agent_ids']) if row else []
            if agent['id'] not in ids:
                ids.append(agent['id'])
            conn.execute('INSERT OR REPLACE INTO extension_library VALUES (?,?,?,?,?)', (key, name, kind, encoded, json.dumps(ids)))
        if entries:
            conn.execute("UPDATE agents SET skill_paths='[]',mcp_servers='{}' WHERE id=?", (agent['id'],))


def resolve(conn, agent):
    skills = json.loads(agent['skill_paths'])
    servers = json.loads(agent['mcp_servers'])
    for row in conn.execute('SELECT * FROM extension_library ORDER BY id'):
        if agent['id'] not in json.loads(row['agent_ids']):
            continue
        config = json.loads(row['config'])
        if row['kind'] == 'skill':
            if config['path'] not in skills:
                skills.append(config['path'])
        else:
            config['display_name'] = row['name']
            servers[row['id']] = config
    return {'skill_paths': skills, 'mcp_servers': servers}


def link_trace(conn, project_id, fact_id, source_project_id, source_fact_id):
    conn.execute('INSERT OR IGNORE INTO fact_extension_sources VALUES (?,?,?,?)', (project_id, fact_id, source_project_id, source_fact_id))


def fact_trace(conn, project_id, fact_id, visited=None):
    visited = set() if visited is None else set(visited)
    if (project_id, fact_id) in visited or len(visited) >= 32:
        return None
    visited.add((project_id, fact_id))
    row = conn.execute('SELECT trace FROM fact_extensions WHERE project_id=? AND fact_id=?', (project_id, fact_id)).fetchone()
    if row:
        return json.loads(row['trace'])
    events = []
    found = False
    for source in conn.execute('SELECT * FROM fact_extension_sources WHERE project_id=? AND fact_id=?', (project_id, fact_id)):
        trace = fact_trace(conn, source['source_project_id'], source['source_fact_id'], visited)
        if trace is not None:
            found = True
            for event in trace['events']:
                event = {**event, 'source': f"{source['source_project_id']}/{source['source_fact_id']}"}
                if event not in events:
                    events.append(event)
    return {'run_id': '', 'agent_id': '', 'inherited': True, 'events': events[:1000]} if found else None
