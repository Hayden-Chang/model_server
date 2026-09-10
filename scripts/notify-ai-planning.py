#!/usr/bin/env python3
"""Email only synthetic probe failures and recovery, using a server-local credential."""

import argparse
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def mail_config():
    explicit = os.environ.get("PROBE_MAIL_CONFIG")
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(explicit) if explicit else Path(credentials) / "mail-config" if credentials else None
    if path is None or (not explicit and not path.exists()):
        return None
    config = json.loads(path.read_text())
    for key in ("host", "from", "to", "username", "password"):
        if not isinstance(config.get(key), str) or not config[key].strip() or "\n" in config[key] or "\r" in config[key]:
            raise ValueError("invalid mail configuration")
    for key in ("from", "to"):
        if "@" not in config[key] or any(char in config[key] for char in ",;<> "):
            raise ValueError("one plain email address is required")
    if config.get("security") not in ("ssl", "starttls"):
        raise ValueError("encrypted SMTP is required")
    port = config.get("port", 465 if config["security"] == "ssl" else 587)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid SMTP port")
    config["port"] = port
    return config


def send_mail(config, kind, result, incident=None):
    titles = {"failure": "AI 规划服务检测异常", "recovery": "AI 规划服务已恢复", "test": "AI 规划告警邮箱验证"}
    message = EmailMessage()
    message["From"], message["To"] = config["from"], config["to"]
    message["Subject"] = "[DayMosaic] " + titles[kind]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    lines = [titles[kind], "", "服务：DayMosaic AI 规划"]
    if kind == "test":
        lines += ["这是一封配置验证邮件，没有触发模型调用。", "以后只在探测发现故障和恢复时通知，正常运行不发信。"]
    else:
        lines += ["检测时间：" + result["checkedAt"]]
        if kind == "failure":
            lines += ["阶段：" + str(result.get("stage", "unknown")),
                      "错误码：" + str(result.get("code", "UNKNOWN")),
                      "HTTP 状态：" + str(result.get("httpStatus", "无"))]
            for label, key in (("检测运行 ID", "probeRunID"), ("请求 ID", "requestID"),
                               ("响应请求 ID", "responseRequestID")):
                if result.get(key):
                    lines.append(label + "：" + str(result[key]))
        else:
            lines += ["首次异常：" + incident["checkedAt"], "真实排程已通过：08:00–08:30。",
                      "本次耗时：" + str(result.get("durationSeconds", "未知")) + " 秒"]
            for label, key in (("异常检测运行 ID", "probeRunID"), ("异常请求 ID", "requestID")):
                if incident.get(key):
                    lines.append(label + "：" + str(incident[key]))
        lines += ["", "每小时检测一次；本邮件只包含合成测试的状态信息。"]
    message.set_content("\n".join(lines))
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        client = smtplib.SMTP_SSL(config["host"], config["port"], timeout=10, context=context)
    else:
        client = smtplib.SMTP(config["host"], config["port"], timeout=10)
    try:
        if config["security"] == "starttls":
            client.starttls(context=context)
        client.login(config["username"], config["password"])
        client.send_message(message, from_addr=config["from"], to_addrs=[config["to"]])
    finally:
        # DATA acceptance is the success boundary; a later QUIT error must not cause a duplicate.
        client.close()


def notify(directory, result):
    try:
        config = mail_config()
        if config is None:
            return {"status": "disabled"}
        path = directory / "mail-state.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        if result["status"] == "unhealthy":
            if not state:
                state = {"incident": {key: result.get(key) for key in
                         ("checkedAt", "stage", "code", "httpStatus", "probeRunID", "requestID", "responseRequestID")},
                         "failureSent": False}
            kind = None if state["failureSent"] else "failure"
        else:
            kind = "recovery" if state else None
        # Preserve an incident even if sending fails, so the next hourly run can retry.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state) + "\n")
        temporary.replace(path)
        if kind is None:
            return {"status": "quiet"}
        send_mail(config, kind, result, state.get("incident"))
        state = {} if kind == "recovery" else {**state, "failureSent": True}
        temporary.write_text(json.dumps(state) + "\n")
        temporary.replace(path)
        return {"status": "accepted", "event": kind}
    except smtplib.SMTPAuthenticationError:
        return {"status": "failed", "code": "SMTP_AUTH_FAILED"}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "failed", "code": "EMAIL_NOTIFICATION_FAILED"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", required=True, action="store_true")
    parser.parse_args()
    try:
        config = mail_config()
        if config is None:
            raise ValueError("mail is not configured")
        send_mail(config, "test", {})
    except (OSError, ValueError, TypeError, KeyError):
        print('{"mailTest":"failed"}')
        raise SystemExit(1)
    print('{"mailTest":"accepted_by_smtp"}')
