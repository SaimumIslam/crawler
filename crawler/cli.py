"""Command-line interface for the crawler.

Commands:
  crawl    <template.yaml> [--ignore-robots] [--export json|csv|jsonl]
  schedule <template.yaml ...>  [--ignore-robots]     # run the daily scheduler
  runs     [--site NAME] [--limit N]                   # list past runs
  export   <run_id> [--format json|csv|jsonl] [--out PATH]
  validate <template.yaml>                             # check a template parses
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import SiteTemplate, ensure_dirs
from .logging_conf import configure, get_logger
from .store.db import Store
from .store.export import export_run

app = typer.Typer(add_completion=False, help="Anti-detection web crawler.")
console = Console()
log = get_logger("cli")


@app.callback()
def _init(log_level: str = typer.Option("INFO", "--log-level")):
    configure(log_level)
    ensure_dirs()


@app.command()
def crawl(
    template: Path = typer.Argument(..., help="Path to a site template YAML"),
    ignore_robots: bool = typer.Option(False, "--ignore-robots", help="Opt-in; logged"),
    export: str = typer.Option(None, "--export", help="json | csv | jsonl"),
    captcha: str = typer.Option(
        None, "--captcha", help="Override CAPTCHA policy: skip | manual | stop"
    ),
):
    """Run a single crawl now."""
    from .schedule.runner import run_template

    tmpl = SiteTemplate.from_yaml(template)
    if captcha:
        if captcha not in ("skip", "manual", "stop"):
            raise typer.BadParameter("--captcha must be skip, manual, or stop")
        tmpl.captcha.on_detect = captcha  # type: ignore[assignment]
    result = run_template(tmpl, ignore_robots=ignore_robots)
    console.print(
        f"[green]Run {result['run_id']} complete[/]: "
        f"{result['pages']} pages, {result['records_new']} new records"
    )
    if export:
        with Store() as store:
            path = export_run(store, result["run_id"], fmt=export)
        console.print(f"[cyan]Exported[/] -> {path}")


@app.command()
def schedule(
    templates: list[Path] = typer.Argument(..., help="One or more template YAMLs"),
    ignore_robots: bool = typer.Option(False, "--ignore-robots"),
):
    """Start the blocking scheduler for the given templates."""
    from .schedule.daily import run_scheduler

    tmpls = [SiteTemplate.from_yaml(p) for p in templates]
    run_scheduler(tmpls, ignore_robots=ignore_robots)


@app.command()
def runs(
    site: str = typer.Option(None, "--site"),
    limit: int = typer.Option(20, "--limit"),
):
    """List past crawl runs."""
    with Store() as store:
        rows = store.list_runs(site=site, limit=limit)
    table = Table("id", "site", "status", "pages", "new", "started")
    import datetime as dt

    for r in rows:
        started = dt.datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
        table.add_row(
            str(r["id"]), r["site"], r["status"],
            str(r["pages_fetched"]), str(r["records_new"]), started,
        )
    console.print(table)


@app.command()
def export(
    run_id: int = typer.Argument(...),
    format: str = typer.Option("json", "--format"),
    out: Path = typer.Option(None, "--out"),
):
    """Export a run's records."""
    with Store() as store:
        path = export_run(store, run_id, fmt=format, out_path=str(out) if out else None)
    console.print(f"[cyan]Exported[/] -> {path}")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """Launch the local web dashboard (templates, crawls, runs, exports)."""
    from .web.app import run_dashboard

    console.print(f"[green]Dashboard[/] -> http://{host}:{port}")
    run_dashboard(host=host, port=port)


@app.command("bcs-scrape")
def bcs_scrape(
    ignore_robots: bool = typer.Option(
        False, "--ignore-robots",
        help="This domain's robots.txt is 'Disallow: /' - only pass this for a site you're authorized to crawl. Opt-in; logged.",
    ),
    export: str = typer.Option(None, "--export", help="json | csv | jsonl"),
    only: str = typer.Option(None, "--only", help="preli | written (default: both)"),
    limit: int = typer.Option(None, "--limit", help="Max exams to scrape (useful for testing)"),
    rate_limit: float = typer.Option(1.0, "--rate-limit", help="Requests/sec to bcsconfidence.online"),
):
    """Scrape every question from bcsconfidence.online's BCS question bank."""
    from .sites.bcs_question_bank import run as run_bcs_scrape

    if only and only not in ("preli", "written"):
        raise typer.BadParameter("--only must be preli or written")
    result = run_bcs_scrape(
        ignore_robots=ignore_robots, rate_limit_per_sec=rate_limit, limit=limit, only=only,
    )
    console.print(
        f"[green]Run {result['run_id']} complete[/]: "
        f"{result['pages']} pages, {result['records_new']} new records"
    )
    if export:
        with Store() as store:
            path = export_run(store, result["run_id"], fmt=export)
        console.print(f"[cyan]Exported[/] -> {path}")


@app.command("avijatra-bank-jobs-scrape")
def avijatra_bank_jobs_scrape(
    ignore_robots: bool = typer.Option(
        False, "--ignore-robots",
        help="avijatra.com's robots.txt allows /job-question-bank/ already; this is here for consistency/logging only.",
    ),
    export: str = typer.Option(None, "--export", help="json | csv | jsonl"),
    limit: int = typer.Option(None, "--limit", help="Max exams to scrape (useful for testing)"),
    rate_limit: float = typer.Option(1.0, "--rate-limit", help="Requests/sec to avijatra.com"),
):
    """Scrape every past-paper exam from avijatra.com's bank-jobs question bank."""
    from .sites.avijatra_bank_jobs import run as run_avijatra_scrape

    result = run_avijatra_scrape(
        category="bank-jobs", ignore_robots=ignore_robots, rate_limit_per_sec=rate_limit, limit=limit,
    )
    console.print(
        f"[green]Run {result['run_id']} complete[/]: "
        f"{result['pages']} pages, {result['records_new']} new records"
    )
    if export:
        with Store() as store:
            path = export_run(store, result["run_id"], fmt=export)
        console.print(f"[cyan]Exported[/] -> {path}")


@app.command("avijatra-scrape")
def avijatra_scrape(
    category: str = typer.Argument(
        ..., help="URL segment after /job-question-bank/, e.g. bank-jobs, primary-assistant-teacher",
    ),
    ignore_robots: bool = typer.Option(
        False, "--ignore-robots",
        help="avijatra.com's robots.txt allows /job-question-bank/ already; this is here for consistency/logging only.",
    ),
    export: str = typer.Option(None, "--export", help="json | csv | jsonl"),
    limit: int = typer.Option(None, "--limit", help="Max exams to scrape (useful for testing)"),
    rate_limit: float = typer.Option(1.0, "--rate-limit", help="Requests/sec to avijatra.com"),
):
    """Scrape every past-paper exam from any avijatra.com job-question-bank category."""
    from .sites.avijatra_bank_jobs import run as run_avijatra_scrape

    result = run_avijatra_scrape(
        category=category, ignore_robots=ignore_robots, rate_limit_per_sec=rate_limit, limit=limit,
    )
    console.print(
        f"[green]Run {result['run_id']} complete[/] (site={result['site']}): "
        f"{result['pages']} pages, {result['records_new']} new records"
    )
    if export:
        with Store() as store:
            path = export_run(store, result["run_id"], fmt=export)
        console.print(f"[cyan]Exported[/] -> {path}")


@app.command()
def validate(template: Path = typer.Argument(...)):
    """Validate that a template parses and is well-formed."""
    tmpl = SiteTemplate.from_yaml(template)
    console.print(f"[green]OK[/] template '{tmpl.name}': "
                  f"{len(tmpl.start_urls)} start URL(s), mode={tmpl.extract.mode}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
