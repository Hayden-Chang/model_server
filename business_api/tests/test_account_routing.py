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
    assert overlay["time-fragment-api"]["volumes"]==["model-server-usage:/var/lib/model-server"]
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
                for route in ["/health/live","/v1/pipelines/general-text-v1:run","/admin/observability",
                              "/admin/runtime/pipelines/time-fragment-plan-v2"]:
                    assert client.get(route).text=="planner"
                for route in ["/internal","/internal/time-fragment/plan","/internal//time-fragment/plan","/%69nternal/time-fragment/plan"]:
                    assert client.get(route).status_code==404
        finally:
            process.terminate();process.wait(timeout=5)
            account.shutdown();planner.shutdown()
            account.server_close();planner.server_close()
