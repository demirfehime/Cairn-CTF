from __future__ import annotations

from cairn.dispatcher.attachments import prepare_attachments

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_parent_reason_payload,
    validate_reason_payload,
)
from cairn.dispatcher.prompting import (
    format_fact_ids,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    preview,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    from uuid import uuid4
    from cairn.extensions.trace import current
    current.set(None)
    worker = worker.model_copy(update={"env": {**worker.env, "CAIRN_TRACE_SCOPE": uuid4().hex, "CAIRN_PROJECT_ID": project.project.id}})
    driver = get_driver(worker.type, config.runtime.execution)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s worker=%s timeout=%ss",
                project.project.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancellation.reason,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s worker=%s status=%s detail=%s",
                    project.project.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                return "unhealthy"
        open_intents = [
            {
                "id": intent.id,
                "from": intent.from_,
                "description": intent.description,
                "worker": intent.worker,
            }
            for intent in project.intents
            if intent.to is None
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        prompt = render_prompt(
            load_prompt(
                config.runtime.prompt_group,
                "parent_reason.md" if project.project.kind == "parent" else "reason.md",
            ),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="reason_execute",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "example_fact_id": allowed_fact_ids[0] if allowed_fact_ids else "origin",
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(config.tasks.reason.max_intents),
            },
        )

        prompt += prepare_attachments(client, container_manager, container_name, project)

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="reason_execute",
            timeout_seconds=config.tasks.reason.timeout,
            stdin_text=command.stdin_text,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            if project.project.kind == "parent":
                parent_plan = validate_parent_reason_payload(
                    payload,
                    open_intents_empty=not open_intents,
                    max_intents=config.tasks.reason.max_intents,
                )
                if parent_plan["kind"] != "rejected":
                    referenced_fact_ids = {
                        fact_id
                        for action in [
                            *parent_plan["intents"],
                            *([parent_plan["complete"]] if parent_plan["complete"] else []),
                        ]
                        for fact_id in action["from"]
                    }
                    invalid_fact_ids = sorted(referenced_fact_ids - set(allowed_fact_ids))
                    if invalid_fact_ids:
                        raise ValueError(
                            f"parent action references invalid facts: {invalid_fact_ids}; "
                            f"valid facts: {allowed_fact_ids}"
                        )
                kind, data = parent_plan["kind"], parent_plan
            else:
                kind, data = validate_reason_payload(
                    payload,
                    open_intents_empty=not open_intents,
                    max_intents=config.tasks.reason.max_intents,
                )
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if kind == "rejected":
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if kind == "complete":
            complete_data = data["complete"] if project.project.kind == "parent" else data
            response = client.complete(
                project.project.id,
                complete_data["from"],
                complete_data["description"],
                worker.name,
                complete_data.get("evidence"),
            )
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return "failed"
            if response.status_code == 202:
                LOG.info(
                    "Parent Flag candidate staged project=%s worker=%s candidate=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    response.data.get("id") if isinstance(response.data, dict) else None,
                    execute_ms,
                    total_ms,
                )
                return "success"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                complete_data["from"],
                execute_ms,
                total_ms,
            )
            return "success"
        if kind == "plan":
            created = 0
            moved = 0
            hinted = 0
            for intent_data in data["intents"]:
                response = client.create_intent(
                    project.project.id,
                    intent_data["from"],
                    intent_data["goal"],
                    worker.name,
                    child_title=intent_data["title"],
                    child_origin=intent_data["origin"],
                    child_goal=intent_data["goal"],
                    priority=int(intent_data.get("priority", 100)),
                )
                if response.ok:
                    created += 1
                else:
                    LOG.warning(
                        "parent Child intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
            for move in data["moves"]:
                response = client.move_agent(
                    project.project.id,
                    move["agent_id"],
                    move["target_child_id"],
                    move["reason"],
                    breakthrough=bool(move.get("breakthrough", False)),
                )
                if response.ok:
                    moved += 1
                else:
                    LOG.warning(
                        "parent Agent move rejected project=%s agent=%s child=%s status=%s body=%s",
                        project.project.id,
                        move["agent_id"],
                        move["target_child_id"],
                        response.status_code,
                        response.text,
                    )
            for hint in data["hints"]:
                response = client.create_hint(
                    hint["child_id"], hint["content"], f"parent.{worker.name}"
                )
                if response.ok:
                    hinted += 1
                else:
                    LOG.warning(
                        "parent Hint write failed project=%s child=%s status=%s body=%s",
                        project.project.id,
                        hint["child_id"],
                        response.status_code,
                        response.text,
                    )
            LOG.info(
                "parent plan finished project=%s worker=%s created_children=%s moved_agents=%s forwarded_hints=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                moved,
                hinted,
                execute_ms,
                total_ms,
            )
            return "success"
        if kind == "intents":
            created = 0
            for intent_data in data:
                response = client.create_intent(project.project.id, intent_data["from"], intent_data["description"], worker.name)
                if response.status_code == 403:
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    continue
                if not response.ok:
                    LOG.warning(
                        "reason intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    continue
                created += 1
                LOG.info(
                    "reason created intent project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    intent_data["from"],
                    intent_data["description"],
                )
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            if created == 0:
                LOG.warning(
                    "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    len(data),
                    execute_ms,
                    total_ms,
                )
                return "failed"
            return "success"
        LOG.info(
            "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name)
