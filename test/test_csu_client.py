"""业务登录与加密请求：仅使用合成数据。"""
from unittest.mock import Mock

import pytest

from app.csu import zhxg


def test_login_passes_force_logout(monkeypatch):
    session = Mock()
    session.post.return_value.json.return_value = {"data": {"token": "fake-token"}}
    login = Mock(return_value="<script>var uid='fake-user'; var lzc='fake-lzc';</script>")
    monkeypatch.setattr(zhxg, "cas_login", login)
    client = zhxg.ZhxgClient(session=session, force_logout=True)
    assert client.login("test-user", "test-password") == "fake-token"
    login.assert_called_once_with(session, "test-user", "test-password", zhxg.CAS_CALLBACK, force_logout=True)
    assert session.post.call_args.kwargs["json"]["caasual"] == client.casual


@pytest.mark.parametrize("payload", [None, [], {}, {"data": []}, {"data": {"debug": "sensitive-value"}}])
def test_invalid_exchange_does_not_echo_raw_response(payload):
    session = Mock()
    session.post.return_value.json.return_value = payload
    with pytest.raises(zhxg.ZhxgError) as error:
        zhxg.ZhxgClient(session=session)._exchange_callback("var uid='fake-user';")
    assert "sensitive-value" not in str(error.value)


def test_status_uses_encrypted_request():
    session = Mock()
    session.post.return_value.json.return_value = {"code": "331"}
    client = zhxg.ZhxgClient(session=session)
    client.token = "fake-token"
    assert client.dk_status() == {"code": "331"}
    request = session.post.call_args.kwargs
    assert request["data"] == zhxg.des_encrypt({"paramsData": {"dklb": "PA"}}, client.casual)
    assert request["headers"]["Authorization"] == "fake-token"


def test_invalid_response_does_not_echo_raw_body():
    session = Mock()
    session.post.return_value.json.side_effect = ValueError("raw secret response")
    with pytest.raises(zhxg.ZhxgError, match="返回格式异常"):
        zhxg.ZhxgClient(session=session).dk_status()
