import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rollback-account-ai-cutover.sh"


def run_script(*arguments, env=None):
    environment = {**os.environ, "PUBLIC_DOMAIN": "test.example.com", **(env or {})}
    return subprocess.run(
        ["sh", str(SCRIPT), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_shell_syntax_and_help():
    syntax = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert syntax.returncode == 0, syntax.stderr
    help_result = run_script("--help")
    assert help_result.returncode == 0
    assert "--strict" in help_result.stdout and "--skip-export" in help_result.stdout
    unknown = run_script("--bogus")
    assert unknown.returncode == 2
    assert "unknown option" in unknown.stderr


def test_dry_run_prints_the_full_plan_without_creating_backups():
    result = run_script("--dry-run")
    assert result.returncode == 0, result.stderr
    assert "docker compose -f docker-compose.yml -f docker-compose.accounts.yml stop caddy time-fragment-api" in result.stdout
    assert "--profile rollback run --rm --no-deps quota-rollback python -m app.reverse_ai_quota_export --step export" in result.stdout
    assert "--profile rollback run --rm --no-deps quota-rollback python -m app.reverse_ai_quota_export --step close --reset-import" in result.stdout
    assert "docker compose -f docker-compose.yml up -d" in result.stdout
    assert "dry run complete" in result.stdout
    assert not (ROOT / "rollback-backups").exists()


def test_skip_export_disables_reset_and_reports_stale_counters():
    result = run_script("--dry-run", "--skip-export", "--no-reset-import")
    assert result.returncode == 0, result.stderr
    assert "skipped by --skip-export" in result.stdout
    assert "--profile rollback run --rm --no-deps quota-rollback python -m app.reverse_ai_quota_export --step close --reset-import" not in result.stdout
    assert "--profile rollback run --rm --no-deps quota-rollback python -m app.reverse_ai_quota_export --step close" in result.stdout
