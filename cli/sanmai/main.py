"""``sanmai`` — the operator CLI for provisioning and running a SanMai AI stack.

Every command from ``open-core-architecture.md`` §9 is present. All commands
*print their plan* and exit ``0`` — they are deliberately side-effect-free
scaffolding — **except** ``doctor``, which really probes the host toolchain
(``gcloud`` / ``terraform`` / ``docker``) and exits non-zero if anything is
missing. ``doctor`` is the CLI acceptance probe: green means the box can drive
a real provisioning run.

Run ``sanmai --help`` to list every command.
"""

from __future__ import annotations

import shutil

import typer

app = typer.Typer(
    name="sanmai",
    help="SanMai AI — provision, migrate, and operate a self-hostable restaurant OS.",
    no_args_is_help=True,
    add_completion=False,
)


def _plan(command: str, steps: list[str]) -> None:
    """Print the plan a command *would* execute. No side effects."""
    typer.echo(f"[sanmai {command}] plan (dry scaffold — nothing was executed):")
    for i, step in enumerate(steps, start=1):
        typer.echo(f"  {i}. {step}")


@app.command()
def init(
    name: str = typer.Option("my-venue", help="Human name for the new deployment."),
) -> None:
    """Scaffold a new SanMai config repo (control-plane + venue skeleton)."""
    _plan(
        "init",
        [
            f"create ./{name}/ with sanmai.toml + adapters.toml",
            "write .env.example with SANMAI_-prefixed keys (no secrets)",
            "seed migrations/ and infra/ (Terraform module refs)",
            "print next steps: `sanmai secrets`, then `sanmai provision`",
        ],
    )


@app.command()
def provision(
    env: str = typer.Option("staging", help="Target environment to provision."),
) -> None:
    """Stand up cloud infrastructure for a venue (2 projects + control plane)."""
    _plan(
        "provision",
        [
            f"terraform init/plan/apply for env={env}",
            "create GCP projects: <venue>-data and <venue>-run",
            "wire Cloud SQL, Firestore, GCS bucket, service accounts",
            "register the venue in the control plane",
        ],
    )


@app.command()
def secrets(
    action: str = typer.Argument("sync", help="One of: sync | list | rotate."),
) -> None:
    """Manage provider credentials in the secret manager (never printed)."""
    _plan(
        "secrets",
        [
            f"action={action}",
            "read required SANMAI_* keys from adapters.toml",
            "upsert into Secret Manager (values redacted in all output)",
            "verify each secret is readable by the Cloud Run SA",
        ],
    )


@app.command()
def migrate(
    database_url: str = typer.Option("", help="DB URL; defaults to the env config."),
    dir: str = typer.Option("migrations", help="Directory of *.sql migrations."),
) -> None:
    """Apply ledgered SQL migrations (delegates to ``be.migrate``)."""
    _plan(
        "migrate",
        [
            f"resolve database_url ({'given' if database_url else 'from config'})",
            f"scan {dir}/*.sql in lexical order",
            "run additive-only DDL check (reject bare DROP unless allowed)",
            "python -m be.migrate --database-url <url> --dir " + dir,
        ],
    )


@app.command()
def deploy(
    service: str = typer.Option("be", help="Which artifact to deploy: be | fe."),
    env: str = typer.Option("staging", help="Target environment."),
) -> None:
    """Build and roll out an artifact (BE image / FE bundle)."""
    _plan(
        "deploy",
        [
            f"build {service} artifact",
            f"push to registry for env={env}",
            "roll out to Cloud Run / Pages (env vars preserved)",
            "run post-deploy healthz + readyz probes",
        ],
    )


@app.command()
def dev() -> None:
    """Run the whole stack locally against fakes (no external credentials)."""
    _plan(
        "dev",
        [
            "select demo/echo/generic/noop/local/static adapters",
            "start FastAPI (be.app.main:app) with uvicorn --reload",
            "apply migrations to a local sqlite/pg",
            "print the local URLs (/, /healthz, /readyz)",
        ],
    )


@app.command()
def config(
    action: str = typer.Argument("show", help="One of: show | validate | diff."),
) -> None:
    """Inspect and validate the merged configuration."""
    _plan(
        "config",
        [
            f"action={action}",
            "load be.config.Settings (env-only, fail-loud)",
            "report config_schema_version + selected adapters",
            "redact any secret-bearing fields",
        ],
    )


@app.command()
def adapter(
    action: str = typer.Argument("list", help="One of: list | show | check."),
) -> None:
    """List available seam adapters and the one bound per seam."""
    _plan(
        "adapter",
        [
            f"action={action}",
            "enumerate seams: payments, llm, fiscal, notify, storage, identity",
            "show the concrete provider bound to each seam",
            "check=probe each bound provider's config (fail-loud)",
        ],
    )


@app.command()
def doctor() -> None:
    """Really check the local toolchain — the only non-stub command.

    Exits non-zero if any required binary (``gcloud``, ``terraform``,
    ``docker``) is missing from ``PATH``.
    """
    required = ("gcloud", "terraform", "docker")
    missing: list[str] = []
    typer.echo("[sanmai doctor] checking required tools on PATH:")
    for tool in required:
        path = shutil.which(tool)
        if path:
            typer.echo(f"  ok    {tool:<10} -> {path}")
        else:
            typer.echo(f"  MISS  {tool:<10} -> not found")
            missing.append(tool)
    if missing:
        typer.echo(f"doctor: missing required tools: {', '.join(missing)}", err=True)
        raise typer.Exit(code=1)
    typer.echo("doctor: all required tools present.")


@app.command()
def logs(
    service: str = typer.Option("be", help="Which service's logs to tail."),
) -> None:
    """Tail logs for a deployed service."""
    _plan(
        "logs",
        [
            f"resolve service={service} in the active env",
            "stream Cloud Run / Cloud Logging entries",
            "apply severity + time-window filters",
        ],
    )


@app.command()
def status() -> None:
    """Show deployment + adapter health for the active environment."""
    _plan(
        "status",
        [
            "resolve active environment from config",
            "query Cloud Run revisions + traffic split",
            "probe /healthz and /readyz for each service",
            "summarize bound adapters and last migration version",
        ],
    )


if __name__ == "__main__":  # pragma: no cover
    app()
