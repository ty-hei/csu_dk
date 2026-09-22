"""单次核心与本地控制页：全部使用假客户端，不访问学校。"""
from __future__ import annotations

import io
import subprocess
import sys
import threading
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
import requests

import simple
from app.csu import workflow


def status(**data):
    return {"code": "200", "data": {"sfydk": False, "kdk": True, "dkbc": "test", **data}}


@pytest.fixture
def client(monkeypatch):
    fake = Mock()
    fake.dk_status.return_value = status()
    fake.submit_dk.return_value = {"code": "200"}
    monkeypatch.setattr(workflow.buildings, "for_student", Mock(return_value=(
        (112.0, 28.0), "测试楼栋", {"canDk": True}, "seed",
    )))
    return fake


@pytest.mark.parametrize(("response", "expected"), [
    ({"code": "331"}, "no_task"),
    (status(sfydk=True), "done"),
    (status(kdk=False), "waiting"),
])
def test_no_unnecessary_submit(client, response, expected):
    client.dk_status.return_value = response
    assert workflow.execute(client, submit=True).status == expected
    client.submit_dk.assert_not_called()
    workflow.buildings.for_student.assert_not_called()


def test_query_is_readonly(client):
    assert workflow.execute(client).status == "ready"
    client.submit_dk.assert_not_called()
    workflow.buildings.for_student.assert_not_called()


@pytest.mark.parametrize("response", [None, [], {"code": "500"}, {"code": "200", "data": None}, status(dkbc="")])
def test_invalid_status_never_submits(client, response):
    client.dk_status.return_value = response
    with pytest.raises(RuntimeError):
        workflow.execute(client, submit=True)
    client.submit_dk.assert_not_called()


def test_location_rejected(client):
    workflow.buildings.for_student.return_value = ((112.0, 28.0), "测试", {"canDk": False}, "seed")
    assert workflow.execute(client, submit=True).status == "failed"
    client.submit_dk.assert_not_called()


@pytest.mark.parametrize("submission", [{"code": "200"}, requests.Timeout(), None])
def test_confirm_and_never_resend(client, submission):
    client.dk_status.side_effect = [status(), status(sfydk=True, dksj="20:00")]
    if isinstance(submission, Exception):
        client.submit_dk.side_effect = submission
    else:
        client.submit_dk.return_value = submission
    result = workflow.execute(client, submit=True)
    assert result.status == "success"
    assert "20:00" in result.message
    client.submit_dk.assert_called_once()
    workflow.buildings.for_student.assert_called_once_with(client)


@pytest.mark.parametrize("confirmation", [requests.Timeout(), TimeoutError(), [], status()])
def test_ambiguous_result_not_retried(client, confirmation):
    client.dk_status.side_effect = [status(), confirmation]
    assert workflow.execute(client, submit=True).status == "unconfirmed"
    client.submit_dk.assert_called_once()


def test_session_closed_even_on_failure(monkeypatch):
    session = Mock()
    factory = Mock(return_value=session)
    client = Mock()
    client.login.side_effect = RuntimeError("登录失败")
    constructor = Mock(return_value=client)
    monkeypatch.setattr(workflow, "new_session", factory)
    monkeypatch.setattr(workflow, "ZhxgClient", constructor)
    with pytest.raises(RuntimeError):
        workflow.run_once("test", "test")
    factory.assert_called_once_with()
    constructor.assert_called_once_with(session=session, force_logout=False)
    session.close.assert_called_once()


def server_state():
    return SimpleNamespace(server_port=8765, token="test-token", action_lock=threading.Lock(),
                           next_attempt=0, runner=Mock(return_value=workflow.Result("ready", "仅查询")))


def request(server, *, action="status", token="test-token", method="POST", headers=None, force_logout=None):
    handler = simple.Handler.__new__(simple.Handler)
    handler.server = server
    handler.client_address = ("127.0.0.1", 1234)
    handler.path = "/run" if method == "POST" else "/"
    handler.headers = Message()
    fields = {"csrf": token, "username": "test-user", "password": "test-secret", "action": action}
    if force_logout is not None:
        fields["force_logout"] = force_logout
    body = urlencode(fields).encode()
    for key, value in {"Host": "127.0.0.1:8765", "Content-Type": "application/x-www-form-urlencoded",
                       "Content-Length": str(len(body)), **(headers or {})}.items():
        handler.headers[key] = value
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    getattr(handler, f"do_{method}")()
    return handler


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example:8765"}, {"Origin": "https://attacker.example"},
    {"Origin": "null"}, {"X-Forwarded-For": ""}, {"Forwarded": "for=127.0.0.1"},
])
def test_host_origin_and_proxy_guard(headers):
    state = server_state()
    handler = request(state, headers=headers)
    handler.send_response.assert_called_once_with(403)
    state.runner.assert_not_called()


def test_csrf_and_replay():
    state = server_state()
    request(state, token="bad").send_response.assert_called_once_with(403)
    state.runner.assert_not_called()
    handler = request(state)
    handler.send_response.assert_called_once_with(200)
    state.runner.assert_called_once_with("test-user", "test-secret", submit=False, force_logout=False)
    assert b"test-secret" not in handler.wfile.getvalue()
    assert b"test-user" not in handler.wfile.getvalue()
    request(state).send_response.assert_called_once_with(403)
    state.runner.assert_called_once()


def test_submit_explicit_and_output_escaped():
    state = server_state()
    state.runner.return_value = workflow.Result("success", "<script>alert(1)</script>")
    handler = request(state, action="checkin")
    state.runner.assert_called_once_with("test-user", "test-secret", submit=True, force_logout=False)
    assert b"<script>" not in handler.wfile.getvalue()
    assert b"&lt;script&gt;" in handler.wfile.getvalue()


def test_busy_cooldown_and_invalid_action():
    state = server_state()
    with state.action_lock:
        request(state).send_response.assert_called_once_with(409)
    state.next_attempt = float("inf")
    request(state).send_response.assert_called_once_with(429)
    request(state, action="unknown").send_response.assert_called_once_with(400)
    state.runner.assert_not_called()


def test_errors_do_not_echo_secrets():
    state = server_state()
    state.runner.side_effect = RuntimeError("raw-response test-secret token=secret-token")
    handler = request(state)
    assert b"test-secret" not in handler.wfile.getvalue()
    assert b"secret-token" not in handler.wfile.getvalue()
    assert "单处登录" in simple.error_message(RuntimeError("其他 PC 端登录"))


def test_explicit_force_logout_option():
    state = server_state()
    request(state, force_logout="invalid").send_response.assert_called_once_with(400)
    state.runner.assert_not_called()
    request(state, force_logout="1").send_response.assert_called_once_with(200)
    state.runner.assert_called_once_with("test-user", "test-secret", submit=False, force_logout=True)


def test_query_failure_distinguished_from_login(client):
    client.dk_status.return_value = {"code": "500", "message": "internal upstream details"}
    with pytest.raises(workflow.StatusQueryError) as caught:
        workflow.execute(client)
    message = simple.error_message(caught.value)
    assert "登录已成功" in message and "500" in message
    assert "internal upstream details" not in message
    client.submit_dk.assert_not_called()


def test_minimal_import_has_no_server_side_effects(tmp_path):
    code = """
import sys
import simple
from app import buildings
buildings.resolve('不存在的楼栋')
blocked = {'app.config', 'app.db', 'app.checkin', 'app.exits', 'fastapi', 'apscheduler', 'ddddocr'}
assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)
"""
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True)
