from __future__ import annotations

from concurrent.futures import Future

from cairn.dispatcher.models import ReasonCheckpoint, RunningTask
from cairn.dispatcher.baseline import BASELINE_TASKS, missing_baseline_tasks
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.server.models import Fact, ProjectSummary, RuntimeAgentProfile

from conftest import make_config, make_intent, make_project


def _loop() -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.cleanup_futures = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop.agent_performance = {}
    loop.intent_failure_counts = {}
    loop.intent_retry_until = {}
    loop.blackboard_refresh_interval = 2
    loop._last_blackboard_refresh = {}
    loop._log_state = {}
    loop.project_cursor = 0
    return loop


def _summary(project_id: str, status: str) -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        bootstrap_enabled=True,
        created_at="2026-01-01T00:00:00Z",
        fact_count=2,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )


def test_reason_trigger_detects_new_facts_and_open_intent_completion() -> None:
    loop = _loop()
    project = make_project(intents=[make_intent()])
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )
    project.facts.append(Fact(id="f002", description="new"))
    project.intents = []

    assert loop._reason_trigger(project) == "facts:3->4,open_intents:1->0"


def test_reason_trigger_returns_none_when_graph_is_unchanged() -> None:
    loop = _loop()
    project = make_project(intents=[make_intent()])
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )

    assert loop._reason_trigger(project) is None


def test_refresh_runtime_projects_discards_active_and_changed_cleanup_markers() -> None:
    loop = _loop()
    loop.runtime_project_ids = {"active", "stopped", "deleted"}
    loop._inactive_cleanup_done = {
        "active": "stopped",
        "stopped": "stopped",
        "changed": "completed",
        "deleted": "completed",
    }

    loop._refresh_runtime_projects(
        [
            _summary("active", "active"),
            _summary("stopped", "stopped"),
            _summary("changed", "stopped"),
        ]
    )

    assert loop.runtime_project_ids == {"active"}
    assert loop._inactive_cleanup_done == {"stopped": "stopped"}


def test_reap_cleanup_future_records_only_successful_inactive_cleanup() -> None:
    loop = _loop()
    succeeded: Future[bool] = Future()
    failed: Future[bool] = Future()
    succeeded.set_result(True)
    failed.set_result(False)
    loop.cleanup_futures = {
        succeeded: ("container-success", "proj-success", "completed"),
        failed: ("container-failed", "proj-failed", "stopped"),
    }
    loop._cleanup_pending = {"container-success", "container-failed"}
    loop._inactive_cleanup_done = {"proj-failed": "stopped"}

    loop._reap_cleanup_futures()

    assert loop.cleanup_futures == {}
    assert loop._cleanup_pending == set()
    assert loop._inactive_cleanup_done == {"proj-success": "completed"}


def test_active_blackboard_refresh_uses_configured_interval(monkeypatch) -> None:
    loop = _loop()
    loop.runtime_project_ids = {"active"}
    loop.blackboard_refresh_interval = 2
    refreshed: list[str] = []
    loop._refresh_project_shared_state = lambda project_id: refreshed.append(project_id)
    now = iter([10.0, 11.0, 12.1])
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.monotonic", lambda: next(now))
    summaries = [_summary("active", "active")]

    loop._refresh_active_project_shared_states(summaries)
    loop._last_blackboard_refresh["active"] = 10.0
    loop._refresh_active_project_shared_states(summaries)
    loop._refresh_active_project_shared_states(summaries)

    assert refreshed == ["active", "active"]


def test_choose_worker_prefers_priority_then_lower_running_count() -> None:
    workers = make_config().workers
    first = workers[0].model_copy(update={"name": "first", "priority": 0})
    busy = workers[0].model_copy(update={"name": "busy", "priority": 0})
    lower_priority = workers[0].model_copy(update={"name": "lower", "priority": 1})

    ordered = choose_worker(
        [lower_priority, busy, first],
        {"busy": 2, "first": 0, "lower": 0},
    )

    assert [worker.name for worker in ordered] == ["first", "busy", "lower"]


def test_new_fact_dispatches_reason_before_unclaimed_explore_intent() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project(intents=[make_intent()])
    project.intents[0].worker = None
    project.facts.append(Fact(id="f002", description="new"))
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True
    loop._dispatch_explore = lambda *_args: dispatched.append(("explore", "")) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "facts:3->4")]


def test_initial_enabled_project_without_bootstrap_worker_dispatches_reason() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    loop.futures = {}
    project = make_project()
    project.facts = project.facts[:2]
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_initial_project = lambda _project: dispatched.append(("bootstrap", "")) or True
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "initial")]


def test_initial_disabled_project_skips_configured_bootstrap_worker() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project()
    project.project.bootstrap_enabled = False
    project.facts = project.facts[:2]
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_initial_project = lambda _project: dispatched.append(("bootstrap", "")) or True
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "initial")]


def test_initial_enabled_project_without_bootstrap_worker_skips_bootstrap() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    project = make_project()
    project.project.bootstrap_enabled = True
    project.facts = project.facts[:2]

    assert not loop._project_requires_bootstrap(project)


def test_initial_enabled_project_keeps_existing_bootstrap_intent_when_workers_change() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    project = make_project(intents=[make_intent()])
    project.project.bootstrap_enabled = True
    project.facts = project.facts[:2]
    project.intents[0].description = "bootstrap"
    project.intents[0].creator = "dispatcher.bootstrap"
    project.intents[0].from_ = ["origin"]

    assert loop._project_requires_bootstrap(project)


def test_cancel_inactive_tasks_marks_stopped_and_deleted_projects() -> None:
    loop = _loop()
    stopped = TaskCancellation()
    deleted = TaskCancellation()
    loop.futures = {
        Future(): RunningTask("stopped", "explore", "worker", stopped),
        Future(): RunningTask("deleted", "reason", "worker", deleted),
    }

    loop._cancel_inactive_tasks([_summary("stopped", "stopped")])

    assert stopped.reason == "stopped"
    assert deleted.reason == "deleted"


def test_initialize_reason_checkpoint_only_for_active_projects_with_open_intents() -> None:
    loop = _loop()
    active = _summary("active", "active")
    active.unclaimed_intent_count = 1

    loop._initialize_reason_checkpoints(
        [
            active,
            _summary("idle", "active"),
            _summary("stopped", "stopped"),
        ]
    )

    assert loop.reason_checkpoints == {
        "active": ReasonCheckpoint(fact_count=2, hint_count=0, open_intent_count=1)
    }


def test_select_worker_reports_busy_unhealthy_rejected_and_unsupported_workers(monkeypatch) -> None:
    loop = _loop()
    base = make_config()
    busy = base.workers[0].model_copy(update={"name": "busy", "task_types": ["reason"]})
    unhealthy = base.workers[0].model_copy(update={"name": "unhealthy", "task_types": ["reason"]})
    rejected = base.workers[0].model_copy(update={"name": "rejected", "task_types": ["reason"]})
    unsupported = base.workers[0].model_copy(update={"name": "unsupported", "task_types": ["explore"]})
    loop.config = base.model_copy(update={"workers": [busy, unhealthy, rejected, unsupported]})
    loop.futures = {Future(): RunningTask("proj", "reason", "busy", TaskCancellation())}
    loop.worker_unhealthy_until = {"unhealthy": 110.0}
    loop.worker_rejected_until = {("proj", "reason", "rejected"): 120.0}
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.time", lambda: 100.0)

    project = make_project()
    project.project.id = "proj"
    selection = loop._select_worker(project, "reason")

    assert selection.worker is None
    assert selection.blocked_busy == ["busy(1/1)"]
    assert selection.blocked_unhealthy == ["unhealthy(10.0s)"]
    assert selection.blocked_rejected == ["rejected(20.0s)"]
    assert selection.blocked_task_type == ["unsupported"]


def test_disabled_worker_healthcheck_skips_automatic_startup_but_force_runs_diagnostic() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"worker_healthcheck": "disabled"})}
    )
    calls: list[bool] = []
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(show_commands)
    loop._startup_healthchecks_checked = False

    loop.run_startup_healthchecks()

    assert calls == []
    assert loop._startup_healthchecks_checked

    loop._startup_healthchecks_checked = False
    loop.run_startup_healthchecks(show_commands=True, force=True)

    assert calls == [True]


def test_startup_only_worker_healthcheck_runs_automatic_startup_check() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"worker_healthcheck": "startup_only"})}
    )
    calls: list[bool] = []
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(show_commands)
    loop._startup_healthchecks_checked = False

    loop.run_startup_healthchecks()

    assert calls == [False]


def test_dynamic_agents_refresh_into_dedicated_api_workers() -> None:
    loop = _loop()
    base = make_config()
    loop.config = base.model_copy(
        update={
            "runtime": base.runtime.model_copy(update={"dynamic_agents": True}),
            "workers": [],
        }
    )
    loop.client = type(
        "Client",
        (),
        {
            "list_runtime_agents": lambda _self: [
                RuntimeAgentProfile(
                    id="agent_001",
                    name="Recon",
                    api_base_url="http://127.0.0.1:9001/v1",
                    api_key="key-one",
                    model="model-a",
                    use_penetration_prompt=True,
                    enabled=True,
                    max_running=2,
                    priority=3,
                    has_api_key=True,
                )
            ]
        },
    )()
    loop.dynamic_workers = []

    loop._refresh_dynamic_workers()

    worker = loop.dynamic_workers[0]
    assert worker.name == "agent_001"
    assert worker.type == "codex"
    assert worker.max_running == 2
    assert worker.env["CODEX_BASE_URL"] == "http://127.0.0.1:9001/v1"
    assert worker.env["OPENAI_API_KEY"] == "key-one"
    assert worker.env["CODEX_MODEL"] == "model-a"
    assert worker.env["CAIRN_USE_PENETRATION_PROMPT"] == "true"


def test_multi_agent_selection_filters_project_scope_and_spreads_load() -> None:
    loop = _loop()
    base = make_config()
    first = base.workers[0].model_copy(
        update={"name": "agent_001", "max_running": 3, "priority": 0}
    )
    second = base.workers[0].model_copy(
        update={"name": "agent_002", "max_running": 3, "priority": 10}
    )
    excluded = base.workers[0].model_copy(
        update={"name": "agent_003", "max_running": 3, "priority": 0}
    )
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    loop.dynamic_workers = [first, second, excluded]
    loop.futures = {
        Future(): RunningTask("proj_001", "explore", "agent_001", TaskCancellation())
    }
    project = make_project()
    project.project.agent_ids = ["agent_001", "agent_002"]
    project.project.target_url = "http://127.0.0.1:3000/path"

    selection = loop._select_worker(project, "explore")

    assert selection.worker is not None
    assert selection.worker.name == "agent_002"
    assert selection.worker.env["CAIRN_CODEX_NETWORK_ALLOW"] == "127.0.0.1"
    assert selection.worker.env["CAIRN_PROJECT_TARGET_URL"] == project.project.target_url


def test_dynamic_agent_weight_rewards_progress_and_penalizes_failure() -> None:
    loop = _loop()
    base = make_config()
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    task = RunningTask("proj_001", "reason", "agent_001", TaskCancellation())

    loop._record_agent_outcome(task, "success")
    loop._record_agent_outcome(task, "success")
    assert loop._agent_weight("proj_001", "agent_001") == 3
    assert loop.agent_performance[("proj_001", "agent_001")].progress_streak == 2

    loop._record_agent_outcome(task, "failed")
    assert loop._agent_weight("proj_001", "agent_001") == 2
    assert loop.agent_performance[("proj_001", "agent_001")].progress_streak == 0
    assert loop.agent_performance[("proj_001", "agent_001")].failure_streak == 1


def test_dynamic_agent_selection_rotates_away_from_failed_agent() -> None:
    loop = _loop()
    base = make_config()
    preferred = base.workers[0].model_copy(update={"name": "agent_001", "priority": 0})
    fallback = base.workers[0].model_copy(update={"name": "agent_002", "priority": 100})
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    loop.dynamic_workers = [preferred, fallback]
    loop.futures = {}
    project = make_project()
    project.project.agent_ids = ["agent_001", "agent_002"]

    assert loop._select_worker(project, "reason").worker.name == "agent_001"
    loop._record_agent_outcome(
        RunningTask(project.project.id, "reason", "agent_001", TaskCancellation()),
        "failed",
    )
    assert loop._select_worker(project, "reason").worker.name == "agent_002"


def test_no_progress_resets_streak_without_changing_dynamic_weight() -> None:
    loop = _loop()
    base = make_config()
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    task = RunningTask("proj_001", "reason", "agent_001", TaskCancellation())
    loop._record_agent_outcome(task, "success")
    loop._record_agent_outcome(task, "no_progress")

    assert loop._agent_weight("proj_001", "agent_001") == 1
    performance = loop.agent_performance[("proj_001", "agent_001")]
    assert performance.progress_streak == 0
    assert performance.failure_streak == 0


def test_reap_no_progress_updates_reason_checkpoint_without_rewarding_agent() -> None:
    loop = _loop()
    base = make_config()
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    future: Future[str] = Future()
    future.set_result("no_progress")
    loop.futures = {
        future: RunningTask(
            "proj_001",
            "reason",
            "agent_001",
            TaskCancellation(),
            fact_count=3,
            hint_count=2,
            open_intent_count=0,
        )
    }

    loop._reap_futures()

    assert loop.reason_checkpoints["proj_001"] == ReasonCheckpoint(3, 2, 0)
    assert loop._agent_weight("proj_001", "agent_001") == 0


def test_baseline_coverage_keeps_weak_credentials_when_auth_intent_does_not_test_them() -> None:
    auth = make_intent("i001")
    auth.worker = None
    auth.description = "Test SQL injection and authentication logic bypass on the login form."
    ssrf = make_intent("i002")
    ssrf.worker = None
    ssrf.description = "Investigate SSRF through URL preview and callback parameters."
    project = make_project(intents=[auth, ssrf])

    missing = {task.key for task in missing_baseline_tasks(project)}

    assert "weak_credentials" in missing
    assert "injection" not in missing
    assert "ssrf" not in missing


def test_dynamic_project_creates_only_missing_baseline_intents() -> None:
    loop = _loop()
    base = make_config()
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    project = make_project(intents=[])
    project.project.target_url = "http://127.0.0.1:3000"
    project.project.agent_ids = ["agent_001", "agent_002"]
    existing = make_intent("i001")
    existing.worker = None
    existing.description = "Safely verify the suspected SSRF path."
    project.intents = [existing]
    calls: list[tuple[str, str]] = []

    def create_intent(_project_id, _from_ids, description, creator):
        calls.append((creator, description))
        intent_id = f"i{len(calls) + 1:03d}"
        return ApiResult(
            201,
            {
                "id": intent_id,
                "from": ["origin"],
                "to": None,
                "description": description,
                "creator": creator,
                "worker": None,
                "last_heartbeat_at": None,
                "created_at": f"2026-01-01T00:00:{len(calls) + 2:02d}Z",
                "concluded_at": None,
            },
        )

    loop.client = type("Client", (), {"create_intent": staticmethod(create_intent)})()

    created = loop._ensure_baseline_intents(project)

    expected = len(BASELINE_TASKS) - 1
    assert created == expected
    assert len(calls) == expected
    assert all(creator != "dispatcher.baseline.ssrf" for creator, _description in calls)
    assert any(creator == "dispatcher.baseline.weak_credentials" for creator, _description in calls)


def test_unattempted_intent_is_selected_before_failed_intent() -> None:
    loop = _loop()
    failed = make_intent("i001")
    failed.worker = None
    failed.created_at = "2026-01-01T00:00:01Z"
    fresh = make_intent("i002")
    fresh.worker = None
    fresh.created_at = "2026-01-01T00:00:02Z"
    loop.intent_failure_counts[("proj_001", "i001")] = 2

    selected = loop._select_unclaimed_intent("proj_001", [failed, fresh])

    assert selected is not None
    assert selected.id == "i002"


def test_failed_intent_uses_exponential_cooldown(monkeypatch) -> None:
    loop = _loop()
    loop.config = make_config()
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.time", lambda: 100.0)
    task = RunningTask(
        "proj_001",
        "explore",
        "agent_001",
        TaskCancellation(),
        intent_id="i001",
    )

    loop._record_intent_outcome(task, "failed")
    assert loop.intent_failure_counts[("proj_001", "i001")] == 1
    assert loop.intent_retry_until[("proj_001", "i001")] == 105.0

    loop._record_intent_outcome(task, "failed")
    assert loop.intent_failure_counts[("proj_001", "i001")] == 2
    assert loop.intent_retry_until[("proj_001", "i001")] == 110.0

    intent = make_intent("i001")
    intent.worker = None
    assert loop._select_unclaimed_intent("proj_001", [intent]) is None


def test_multi_agent_initial_project_skips_single_bootstrap_and_expands_reason_paths() -> None:
    loop = _loop()
    base = make_config()
    worker = base.workers[0].model_copy(update={"name": "agent_001"})
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True}), "workers": []}
    )
    loop.dynamic_workers = [worker]
    loop.futures = {}
    loop.container_manager = object()
    loop.executor = type(
        "Executor",
        (),
        {
            "submit": lambda _self, fn, *args: _completed_future(
                loop, fn, args
            )
        },
    )()
    loop.client = type(
        "Client",
        (),
        {"claim_reason": lambda _self, *_args: ApiResult(200, {})},
    )()
    project = make_project()
    project.facts = project.facts[:2]
    project.project.agent_ids = ["agent_001", "agent_002", "agent_003", "agent_004"]

    assert not loop._project_requires_bootstrap(project)
    assert loop._dispatch_reason(project, "graph", "initial")
    submitted_config = loop._submitted_args[0]
    assert submitted_config.tasks.reason.max_intents == 4
    assert base.tasks.reason.max_intents == 3


def test_multi_agent_project_ignores_legacy_bootstrap_intent() -> None:
    loop = _loop()
    base = make_config()
    loop.config = base.model_copy(
        update={"runtime": base.runtime.model_copy(update={"dynamic_agents": True})}
    )
    project = make_project(intents=[make_intent()])
    project.project.agent_ids = ["agent_001", "agent_002"]
    project.intents[0].description = "bootstrap"
    project.intents[0].creator = "dispatcher.bootstrap"
    project.intents[0].from_ = ["origin"]

    assert not loop._project_requires_bootstrap(project)


def test_successful_task_refreshes_stable_shared_project_state(tmp_path) -> None:
    loop = _loop()
    project = make_project()
    project.project.target_url = "http://127.0.0.1:3000"
    project.project.agent_ids = ["agent_001"]
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "project:\n  title: test\n",
        },
    )()
    loop.container_manager = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))

    loop._refresh_project_shared_state("proj_001")

    shared = tmp_path / "proj_001" / ".cairn" / "shared" / "project-state.yaml"
    assert shared.read_text(encoding="utf-8") == "project:\n  title: test\n"


def _completed_future(loop: DispatcherLoop, _fn, args) -> Future[str]:
    loop._submitted_args = args
    future: Future[str] = Future()
    future.set_result("success")
    return future
