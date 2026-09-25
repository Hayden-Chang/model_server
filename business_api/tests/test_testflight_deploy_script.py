import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40


def deployment(tmp_path, schema="25"):
    release = tmp_path / "release"
    server = tmp_path / "server"
    tools = tmp_path / "bin"
    (release / "scripts").mkdir(parents=True)
    (server / ".secrets").mkdir(parents=True)
    tools.mkdir()
    shutil.copy2(ROOT / "scripts/deploy-testflight-api.sh", release / "scripts/deploy-testflight-api.sh")
    for name in ["docker-compose.yml", "docker-compose.accounts.yml", "Caddyfile.accounts"]:
        (release / name).write_text("fixture")
    (release / ".release-sha").write_text(SHA)
    (server / ".env").write_text("APPLE_ENVIRONMENT=sandbox\n")
    (server / "Caddyfile.accounts").write_text("previous config")
    log = tmp_path / "commands.jsonl"
    docker = tools / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args=sys.argv[1:]
with open(os.environ['COMMAND_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='exec' and 'billing_environment_schema' in args[-1]: print(os.environ['SCHEMA_VERSION'])
if args[0]=='inspect':
    print('sha256:old' if '--format' in args else '{}')
''')
    curl = tools / "curl"
    curl.write_text('''#!/usr/bin/env python3
import pathlib, sys
args=sys.argv[1:]
pathlib.Path(args[args.index('--dump-header')+1]).write_text('X-Apple-Billing-Environment: sandbox\\n')
print('{}')
''')
    docker.chmod(0o755)
    curl.chmod(0o755)
    env = {**os.environ, "MODEL_SERVER_ROOT": str(server), "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "COMMAND_LOG": str(log), "SCHEMA_VERSION": schema}
    result = subprocess.run(["bash", str(release / "scripts/deploy-testflight-api.sh"), SHA],
                            env=env, text=True, capture_output=True, timeout=30)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls, server


def test_missing_migration_blocks_before_build_or_container_changes(tmp_path):
    result, calls, server = deployment(tmp_path, schema="24")
    assert result.returncode != 0
    assert "schema 25 is required" in result.stderr
    assert all(call[0] == "exec" for call in calls)
    assert not (server / "rollback-backups").exists()


def test_archive_release_deploys_only_testflight_and_preserves_recovery_material(tmp_path):
    result, calls, server = deployment(tmp_path)
    assert result.returncode == 0, result.stderr
    compose = [call for call in calls if call[0] == "compose"]
    assert len(compose) == 3
    assert compose[-2][-2:] == ["build", "testflight-api"]
    assert compose[-1][-5:] == ["up", "-d", "--no-deps", "--wait", "testflight-api"]
    assert all("billing-worker" not in call and "business-api" not in call for call in compose)
    assert (server / ".env").read_text() == "APPLE_ENVIRONMENT=sandbox\n"
    backups = list((server / "rollback-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "Caddyfile.accounts").read_text() == "previous config"
    assert (backups[0] / "previous-image-id").read_text().strip() == "sha256:old"
    assert (backups[0] / "previous-container.json").stat().st_mode & 0o077 == 0
