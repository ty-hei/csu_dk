"""单处登录：仅显式授权时提交学校的继续表单，绝不重复踢出。"""
from unittest.mock import Mock

import pytest
import requests

from app.csu import cas

LOGIN_URL = f"{cas.CAS_BASE}/login?service=example"
LANDING_URL = "https://zhxg.csu.edu.cn/fdcwonsun/caslogin_h5.jsp"
KICKOUT = """<h3>单处登录提醒</h3>
<form id="continue" method="post">
<input name="execution" value="fresh-flow-state"><input name="_eventId" value="continue"></form>
<form id="cancel" method="post">
<input name="execution" value="fresh-flow-state"><input name="_eventId" value="cancel"></form>"""
LOGIN_PAGE = '<input id="execution" value="login-state"><input id="pwdEncryptSalt" value="abcdefghijklmnop">'


def response(html, url=LOGIN_URL):
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result._content = html.encode()
    result.encoding = "utf-8"
    return result


def test_default_does_not_kick_out():
    session = Mock()
    with pytest.raises(RuntimeError, match="其他 PC"):
        cas._continue_single_login(session, response(KICKOUT), 20, force_logout=False)
    session.post.assert_not_called()


def test_official_continue_form_only():
    session = Mock()
    landing = response("landing", LANDING_URL)
    session.post.return_value = landing
    assert cas._continue_single_login(session, response(KICKOUT), 20, force_logout=True) is landing
    session.post.assert_called_once_with(
        LOGIN_URL, data={"execution": "fresh-flow-state", "_eventId": "continue"},
        headers={"user-agent": cas.UA, "referer": LOGIN_URL}, timeout=20,
    )


def test_english_page_recognized_by_forms():
    assert cas._single_login_page(KICKOUT.replace("单处登录提醒", "Single sign-on reminder"))


@pytest.mark.parametrize("html", [
    KICKOUT.replace('id="continue"', 'id="missing"'),
    KICKOUT.replace('method="post"', 'method="get"'),
    KICKOUT.replace('value="fresh-flow-state"', 'value=""'),
    KICKOUT.replace('value="continue"', 'value="cancel"'),
    KICKOUT.replace('id="continue"', 'id="continue" action="https://evil.example/login"'),
    KICKOUT.replace('id="continue"', 'id="continue" action="https://ca.csu.edu.cn.evil.example/authserver/login"'),
    KICKOUT.replace('id="continue"', 'id="continue" action="https://ca.csu.edu.cn@evil.example/authserver/login"'),
    KICKOUT.replace('id="continue"', 'id="continue" action="/other-app"'),
])
def test_malformed_or_foreign_form_not_posted(html):
    session = Mock()
    with pytest.raises(RuntimeError, match="未执行强制登出"):
        cas._continue_single_login(session, response(html), 20, force_logout=True)
    session.post.assert_not_called()


def test_never_repeats_kickout():
    session = Mock()
    session.post.return_value = response(KICKOUT)
    with pytest.raises(RuntimeError, match="尚未解除"):
        cas._continue_single_login(session, response(KICKOUT), 20, force_logout=True)
    session.post.assert_called_once()


@pytest.mark.parametrize("captcha", [False, True])
def test_login_passes_opt_in_through_captcha_path(monkeypatch, captcha):
    session = Mock()
    session.get.return_value = response(LOGIN_PAGE)
    session.post.side_effect = [response(KICKOUT), response("landing", LANDING_URL)]
    monkeypatch.setattr(cas, "_needs_captcha", lambda *_: captcha)
    monkeypatch.setattr(cas, "_captcha_image", lambda *_: b"image")
    monkeypatch.setattr(cas.ocr, "solve", lambda *_: "AB12")
    assert cas.cas_login(session, "test", "pw", LANDING_URL, force_logout=True) == "landing"
    assert session.post.call_count == 2
    assert session.post.call_args.kwargs["data"] == {"execution": "fresh-flow-state", "_eventId": "continue"}
