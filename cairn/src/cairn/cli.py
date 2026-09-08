from pathlib import Path

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    config = DispatchConfig.load(config_path)
    if config.runtime.server_managed_agents and not startup_healthcheck_only:
        from cairn.dispatcher.orchestrator import ParentOrchestrator

        loop = ParentOrchestrator(config_path)
    else:
        loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("child-dispatch")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--project-id", required=True)
@click.option("--log-level", default="INFO", show_default=True)
def child_dispatch(config_path: Path, project_id: str, log_level: str):
    """Run one isolated Child scheduler process."""
    configure_logging(log_level)
    loop = DispatcherLoop(
        config_path,
        scope_kind="child",
        scope_project_id=project_id,
    )
    try:
        loop.run()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
