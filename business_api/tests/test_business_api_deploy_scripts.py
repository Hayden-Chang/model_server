import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts" / "deploy-business-api.sh"
ROLLBACK = ROOT / "scripts" / "rollback-business-api.sh"
SMOKE = ROOT / "scripts" / "verify-bare-title-production.py"


def load_smoke() -> Any:
    spec = importlib.util.spec_from_file_location("bare_title_production_smoke", SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def bare_title_response(title: str) -> dict[str, Any]:
    return {
        "validation": {"valid": True, "attempts": 2, "issues": []},
        "proposal": {
            "operations": [
                {
                    "type": "add",
                    "temporaryId": "temporary-1",
                    "title": title,
                    "durationSlots": 2,
                    "priority": None,
                    "inputOrder": 0,
                }
            ],
            "candidatePlan": {
                "items": [
                    {
                        "itemId": "temporary-1",
                        "title": title,
                        "durationSlots": 2,
                        "segments": [{"startSlot": 36, "endSlot": 38}],
                    }
                ]
            },
        },
    }


def test_business_api_deploy_and_rollback_scripts_have_valid_shell_syntax() -> None:
    for script in (DEPLOY, ROLLBACK):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


def test_deploy_has_immutable_automatic_rollback_and_only_recreates_business_api() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    rollback = ROLLBACK.read_text(encoding="utf-8")

    assert 'docker tag "$old_image_id" "$rollback_image"' in deploy
    assert 'trap automatic_rollback ERR INT TERM' in deploy
    assert '"$backup_dir/rollback-business-api.sh" "$backup_dir"' in deploy
    assert 'up -d --no-deps --force-recreate business-api' in deploy
    assert 'up -d --no-deps --no-build --force-recreate business-api' in rollback
    for forbidden in ("down", "stop caddy", "restart caddy", "restart time-fragment-api"):
        assert forbidden not in deploy
        assert forbidden not in rollback


def test_bare_title_smoke_accepts_only_exact_title_and_earliest_slot() -> None:
    smoke = load_smoke()
    smoke.assert_exact_bare_title(bare_title_response("爸爸"), "爸爸")

    rewritten = bare_title_response("给爸爸打电话")
    with pytest.raises(AssertionError, match="rewritten"):
        smoke.assert_exact_bare_title(rewritten, "爸爸")

    misplaced = bare_title_response("爸爸")
    misplaced["proposal"]["candidatePlan"]["items"][0]["segments"] = [
        {"startSlot": 40, "endSlot": 42}
    ]
    with pytest.raises(AssertionError):
        smoke.assert_exact_bare_title(misplaced, "爸爸")


def test_command_smoke_request_contains_an_existing_target() -> None:
    smoke = load_smoke()
    item = {
        "itemId": "occurrence-1",
        "objectType": "internalTask",
        "title": "性能",
        "durationSlots": 2,
        "segments": [{"startSlot": 36, "endSlot": 38}],
        "isPinned": False,
        "isCompleted": False,
    }

    request = smoke.planning_request("把性能移到下午", "2026-09-14", [item])

    assert request["currentPlan"]["items"] == [item]
    assert request["baseFingerprint"] == smoke.fingerprint("2026-09-14", [item])
