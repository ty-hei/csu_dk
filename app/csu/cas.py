"""CAS 登录与密码加密。"""
from __future__ import annotations

import base64
import re
import secrets
import time
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from . import ocr

CAS_BASE = "https://ca.csu.edu.cn/authserver"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"
_AES_LENGTHS = {16: AES, 24: AES, 32: AES}

MAX_CAPTCHA_ATTEMPTS = 2

_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"RIFF", b"II*\x00", b"MM\x00*")

CAPTCHA_URL = f"{CAS_BASE}/getCaptcha.htl"


class CasIpFrozenError(Exception):
    frozen = True


class _CaptchaRejected(Exception):
    """验证码被拒。"""


def _random_from(chars: str, length: int) -> str:
    return "".join(secrets.choice(chars) for _ in range(length))


def encrypt_password(password: str, salt: str) -> str:
    key = salt.encode()
    if len(key) not in (16, 24, 32):
        raise ValueError(f"pwdEncryptSalt 长度异常：{len(key)}")
    plain = (_random_from(_AES_CHARS, 64) + password).encode()
    cipher = AES.new(key, AES.MODE_CBC, _random_from(_AES_CHARS, 16).encode())
    return base64.b64encode(cipher.encrypt(pad(plain, AES.block_size))).decode()


def input_value(html: str, field_id: str) -> str | None:
    """读取表单值。"""
    tag = BeautifulSoup(html, "html.parser").find("input", id=field_id)
    value = tag.get("value") if tag else None
    return str(value) if value is not None else None


def error_tip(html: str) -> str | None:
    """读取 CAS 错误提示。"""
    node = BeautifulSoup(html, "html.parser").select_one("#showErrorTip")
    if not node:
        return None
    return " ".join(node.get_text(" ", strip=True).split()) or None


def detect_ip_frozen(html: str) -> CasIpFrozenError | None:
    text = str(html or "")
    if "IP冻结" in text or "已被冻结" in text:
        detail = re.search(r"您的IP[（(]([^）)]+)[）)]", text)
        suffix = f"（{detail.group(1)}）" if detail else ""
        return CasIpFrozenError(f"学校统一身份认证已冻结本机 IP{suffix}：多次无效登录会触发风控，请稍后再试")
    return None


_CAS_ERRORS = {
    "badCredentials": ("密码错误", "密码有误", "用户名或密码错"),
    "inactive": ("未激活",),
    "locked": ("锁定",),
    "captcha": ("验证码",),
    "sessionExpired": ("会话已失效", "会话失效"),
}


def classify_error(text: str = "") -> str | None:
    for kind, patterns in _CAS_ERRORS.items():
        if any(pattern in text for pattern in patterns):
            return kind
    return None


def _is_cas_host(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == "ca.csu.edu.cn"


def _single_login_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return all(
        soup.select_one(f'form#{event} input[name="_eventId"][value="{event}"]') is not None
        for event in ("continue", "cancel")
    ) or "单处登录" in html or "当前账户已在其他PC端登录" in html


def _continue_single_login(session: requests.Session, response, timeout: int, *, force_logout: bool):
    """按学校官方 continue 表单登出其他 PC；每次登录至多提交一次。"""
    if not _is_cas_host(response.url) or not _single_login_page(response.text):
        return response
    if not force_logout:
        raise RuntimeError("当前账户已在其他 PC 端登录，可勾选「登出其他 PC 并继续」后重试")
    form = BeautifulSoup(response.text, "html.parser").select_one("form#continue")
    if form is None or str(form.get("method", "")).lower() != "post":
        raise RuntimeError("学校单处登录表单结构异常，未执行强制登出")
    target = urljoin(response.url, str(form.get("action") or response.url))
    if not _is_cas_host(target) or not urlsplit(target).path.startswith("/authserver/"):
        raise RuntimeError("学校单处登录表单地址异常，未执行强制登出")
    fields = {}
    for name in ("execution", "_eventId"):
        inputs = form.select(f'input[name="{name}"]')
        if len(inputs) != 1 or not inputs[0].get("value"):
            raise RuntimeError("学校单处登录表单字段异常，未执行强制登出")
        fields[name] = str(inputs[0]["value"])
    if fields["_eventId"] != "continue":
        raise RuntimeError("学校单处登录表单事件异常，未执行强制登出")
    result = session.post(target, data=fields, headers={"user-agent": UA, "referer": response.url}, timeout=timeout)
    result.raise_for_status()
    if _is_cas_host(result.url) and _single_login_page(result.text):
        raise RuntimeError("学校单处登录限制尚未解除，请在学校页面核实后重试")
    return result


def cas_login(session: requests.Session, username: str, password: str,
              service: str, timeout: int = 20, *, force_logout: bool = False) -> str:
    """登录 CAS 并返回落地页。"""
    login_url = f"{CAS_BASE}/login?service={requests.utils.quote(service, safe='')}"

    first = session.get(login_url, headers={"user-agent": UA}, timeout=timeout)
    frozen = detect_ip_frozen(first.text)
    if frozen:
        raise frozen

    first = _continue_single_login(session, first, timeout, force_logout=force_logout)

    if not _is_cas_host(first.url):
        return first.text

    if not input_value(first.text, "execution") or not input_value(first.text, "pwdEncryptSalt"):
        raise RuntimeError("未找到 CAS 登录表单（页面结构可能变了）")

    if not password:
        raise RuntimeError("请填写密码")

    if _needs_captcha(session, username, timeout):
        return _login_with_captcha(session, login_url, username, password, timeout,
                                   force_logout=force_logout)

    return _submit_login(session, login_url, first.text, username, password, "", timeout, force_logout=force_logout)


def _needs_captcha(session: requests.Session, username: str, timeout: int) -> bool:
    """检查是否需要验证码。"""
    try:
        need = session.get(
            f"{CAS_BASE}/checkNeedCaptcha.htl",
            params={"username": username, "_": int(time.time() * 1000)},
            headers={"user-agent": UA},
            timeout=timeout,
        )
        return bool(need.ok and need.json().get("isNeed"))
    except (requests.RequestException, ValueError):
        return False


def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(magic) for magic in _IMAGE_MAGIC)


def _captcha_image(session: requests.Session, page_html: str, login_url: str,
                   timeout: int) -> bytes | None:
    """读取验证码图片。"""
    src = None
    for tag in BeautifulSoup(page_html, "html.parser").find_all("img"):
        candidate = str(tag.get("src") or "")
        if "captcha" in candidate.lower():
            src = urljoin(login_url, candidate)
            break
    url = src or f"{CAPTCHA_URL}?{int(time.time() * 1000)}"

    try:
        response = session.get(url, headers={"user-agent": UA, "referer": login_url}, timeout=timeout)
    except requests.RequestException:
        return None

    if not response.ok or not _looks_like_image(response.content):
        return None
    return response.content


def _login_with_captcha(session: requests.Session, login_url: str, username: str,
                        password: str, timeout: int, *, force_logout: bool = False) -> str:
    for _attempt in range(MAX_CAPTCHA_ATTEMPTS):
        page = session.get(login_url, headers={"user-agent": UA}, timeout=timeout)
        frozen = detect_ip_frozen(page.text)
        if frozen:
            raise frozen

        image = _captcha_image(session, page.text, login_url, timeout)
        code = ocr.solve(image) if image else None
        if not code:
            # 不提交无效识别结果
            raise RuntimeError(
                "CAS 要求输入验证码，但自动识别不可用（未安装 ddddocr 或识别失败），"
                "请启用 OCR 支持或稍后重试"
            )

        try:
            return _submit_login(session, login_url, page.text, username, password, code, timeout,
                                 force_logout=force_logout)
        except _CaptchaRejected:
            continue

    raise RuntimeError(
        f"CAS 验证码连续 {MAX_CAPTCHA_ATTEMPTS} 次未通过，请启用 OCR 支持或稍后重试"
    )


def _submit_login(session: requests.Session, login_url: str, page_html: str, username: str,
                  password: str, captcha: str, timeout: int, *, force_logout: bool = False) -> str:
    execution = input_value(page_html, "execution")
    salt = input_value(page_html, "pwdEncryptSalt")
    if not execution or not salt:
        raise RuntimeError("未找到 CAS 登录表单（页面结构可能变了）")

    posted = session.post(
        login_url,
        data={
            "username": username,
            "password": encrypt_password(password, salt),
            "captcha": captcha,
            "execution": execution,
            "_eventId": "submit",
            "cllt": "userNameLogin",
            "dllt": "generalLogin",
            "lt": "",
            # 延长 CASTGC 有效期
            "rememberMe": "true",
        },
        headers={"user-agent": UA, "content-type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )

    posted = _continue_single_login(session, posted, timeout, force_logout=force_logout)
    frozen_after = detect_ip_frozen(posted.text)
    if frozen_after:
        raise frozen_after

    tip = error_tip(posted.text)
    if tip:
        kind = classify_error(tip)
        if kind == "captcha":
            raise _CaptchaRejected("验证码未通过")
        if kind == "badCredentials":
            raise RuntimeError(
                "学号或密码有误。请先前往学校信息门户确认能够正常登录，"
                "再回到本地页面重试"
            )
        if kind == "inactive":
            raise RuntimeError("账号未激活，请先在统一身份认证平台激活")
        if kind == "locked":
            raise RuntimeError("账号已被锁定，请联系学校")
        raise RuntimeError("CAS 登录失败，请在学校页面查看原因")

    if _is_cas_host(posted.url):
        raise RuntimeError("CAS 登录未完成，仍停留在学校认证页面")

    return posted.text
