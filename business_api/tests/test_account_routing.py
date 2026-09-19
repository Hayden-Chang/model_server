import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[2]


def test_compose_keeps_private_services_unpublished_and_uses_internal_credentials():
    base=yaml.safe_load((ROOT/"docker-compose.yml").read_text())["services"]
    overlay=yaml.safe_load((ROOT/"docker-compose.accounts.yml").read_text())["services"]
    assert "ports" not in overlay["time-fragment-api"]
    assert "ports" not in base["business-api"]
    assert overlay["business-api"]["environment"]["PLANNING_INTERNAL_ONLY"]=="true"
    assert overlay["business-api"]["environment"]["PLANNING_INTERNAL_SECRET"]==overlay["time-fragment-api"]["environment"]["PLANNING_INTERNAL_SECRET"]
    assert overlay["caddy"]["volumes"]==["./Caddyfile.accounts:/etc/caddy/Caddyfile:ro"]
    # Both services that call the App Store Server API must be able to read the
    # signing key, and only read it: the mount is pinned read-only. A missing
    # mount or a writable one is a configuration regression, not a detail.
    apple_mount="./.secrets:/opt/model_server/.secrets:ro"
    apple_dir=apple_mount.split(":")[1]
    assert overlay["time-fragment-api"]["volumes"]==["model-server-usage:/var/lib/model-server", apple_mount]
    assert overlay["billing-worker"]["volumes"]==[apple_mount]
    for service in ("time-fragment-api","billing-worker"):
        environment=overlay[service]["environment"]
        # resolve_apple_key_p8 prefers the path form and the host ships the key
        # as a file, so the operator must be able to supply the path...
        assert "APPLE_PRIVATE_KEY_PATH" in environment
        # ...and its default must name a file inside the mounted directory, or
        # the container would read a path it cannot see.
        default_path=environment["APPLE_PRIVATE_KEY_PATH"].split(":-",1)[1].rstrip("}")
        assert default_path.startswith(apple_dir+"/"), (service, default_path, apple_dir)
        # The bundle id is what App Store receipts are checked against, so its
        # default must be the real one; a wrong default silently rejects every
        # genuine transaction.
        assert environment["APPLE_BUNDLE_ID"].split(":-",1)[1].rstrip("}")=="com.hayden.timefragment"
    # time-fragment-api owns POST /billing/apple/verify, so it needs the same
    # Apple credentials as the worker; it previously had none and every purchase
    # failed closed with BILLING_NOT_CONFIGURED after StoreKit had charged.
    assert overlay["time-fragment-api"]["environment"]["APPLE_KEY_ID"]==overlay["billing-worker"]["environment"]["APPLE_KEY_ID"]
    # AccountBackend.quota() is the only reader of the member daily limit and
    # only account_main.py wires it up, so the API must forward it while the
    # worker -- which never calls quota() -- must not, exactly as with the guest
    # limit. Omitting it silently pinned every signed-in member to the code
    # default 30, because no Compose file declares env_file.
    api_environment=overlay["time-fragment-api"]["environment"]
    assert "TIME_FRAGMENT_MEMBER_QUOTA_LIMIT" in api_environment
    assert api_environment["TIME_FRAGMENT_MEMBER_QUOTA_LIMIT"].split(":-",1)[1].rstrip("}")=="30"
    assert "TIME_FRAGMENT_MEMBER_QUOTA_LIMIT" not in overlay["billing-worker"]["environment"]
    rollback=overlay["quota-rollback"]
    assert "ports" not in rollback
    assert rollback["profiles"]==["rollback"]
    assert rollback["volumes"]==["model-server-usage:/var/lib/model-server"]
    assert set(rollback["environment"])=={"SUPABASE_URL","SUPABASE_SERVICE_ROLE_KEY"}
    assert "SUPABASE_SERVICE_ROLE_KEY" not in base["business-api"]["environment"]


@pytest.mark.skipif(not os.environ.get("CADDY_BINARY"),reason="Set CADDY_BINARY to the verified Caddy 2.11.4 executable")
def test_real_caddy_routes_public_api_and_blocks_private_planning(tmp_path):
    binary=os.environ["CADDY_BINARY"]
    env={**os.environ,"PUBLIC_DOMAIN":"test.example.com","XDG_DATA_HOME":str(tmp_path),"XDG_CONFIG_HOME":str(tmp_path)}
    subprocess.run([binary,"adapt","--config",str(ROOT/"Caddyfile.accounts"),"--adapter","caddyfile"],env=env,
                   stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)

    def server(label):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(label.encode())
            def log_message(self,*args):
                pass
        instance=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        threading.Thread(target=instance.serve_forever,daemon=True).start()
        return instance

    account,planner=server("account"),server("planner")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));port=listener.getsockname()[1]
    config=(ROOT/"Caddyfile.accounts").read_text()
    config=config.replace("default_sni {$PUBLIC_DOMAIN}","admin off\n\tpersist_config off")
    config=config.replace("https://{$PUBLIC_DOMAIN}",f"http://127.0.0.1:{port}")
    config="\n".join(line for line in config.splitlines() if not line.strip().startswith("tls "))
    config=config.replace("time-fragment-api:8000",f"127.0.0.1:{account.server_port}")
    config=config.replace("business-api:8000",f"127.0.0.1:{planner.server_port}")
    path=tmp_path/"Caddyfile";path.write_text(config)
    with (tmp_path/"caddy.log").open("w") as log:
        process=subprocess.Popen([binary,"run","--config",str(path),"--adapter","caddyfile"],env=env,stdout=log,stderr=log)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}",timeout=2,trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get("/health/live").status_code==200:break
                    except httpx.TransportError:
                        if process.poll() is not None:pytest.fail("Caddy exited during startup")
                        time.sleep(0.05)
                for route in ["/api/auth/guest","/api/plan/parse","/api/account/quota","/api/development/membership",
                              "/admin/time-fragment/quotas/reset-all"]:
                    assert client.get(route).text=="account"
                for route in ["/health/live","/v1/pipelines/general-text-v1:run","/admin/observability"]:
                    assert client.get(route).text=="planner"
                for route in ["/internal","/internal/time-fragment/plan","/internal//time-fragment/plan","/%69nternal/time-fragment/plan"]:
                    assert client.get(route).status_code==404
        finally:
            process.terminate();process.wait(timeout=5)
            account.shutdown();planner.shutdown()
            account.server_close();planner.server_close()
