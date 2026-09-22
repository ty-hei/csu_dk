"""智慧学工（zhxg.csu.edu.cn）：CAS 登录换业务 JWT，业务请求体走 DES-ECB。"""
from __future__ import annotations

import re
from urllib.parse import quote

import requests

from .cas import UA, cas_login
from .des import des_encrypt, generate_casual

ORIGIN = "https://zhxg.csu.edu.cn"
CAS_CALLBACK = f"{ORIGIN}/fdcwonsun/caslogin_h5.jsp"
API_SYS = f"{ORIGIN}/znzhxgpt/basesys"
API_QXJ = f"{ORIGIN}/znzhxgpt/qxj"
APP_CODE = "znzhxgpt"


class ZhxgError(Exception):
    pass


def new_session() -> requests.Session:
    return requests.Session()


class ZhxgClient:
    def __init__(self, session: requests.Session | None = None, *, force_logout: bool = False):
        self.session = session if session is not None else new_session()
        self.force_logout = force_logout
        self.casual = generate_casual(16)
        self.token = None

    def login(self, username: str, password: str) -> str:
        html = cas_login(self.session, username, password, CAS_CALLBACK, force_logout=self.force_logout)
        return self._exchange_callback(html)

    def _exchange_callback(self, html: str) -> str:
        uid = re.search(r"var\s+uid\s*=\s*'([^']*)'", html) or re.search(r'var\s+uid\s*=\s*"([^"]*)"', html)
        lzc = re.search(r"var\s+lzc\s*=\s*'([^']*)'", html) or re.search(r'var\s+lzc\s*=\s*"([^"]*)"', html)
        if not uid:
            raise ZhxgError("CAS 回调页未返回 uid（可能是 service 不匹配）")

        response = self.session.post(
            f"{API_SYS}/rbac-yh/login-other",
            json={
                "tyrzpt": "1",
                "channeld": "1",
                "yhzh": quote(quote(uid.group(1)), safe=""),
                "lzc": quote(quote(lzc.group(1) if lzc else ""), safe=""),
                "caasual": self.casual,
            },
            headers={
                "user-agent": UA,
                "content-type": "application/json; charset=utf-8",
                "deviceType": "4",
            },
            timeout=20,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        payload = data.get("data") if isinstance(data, dict) else None
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise ZhxgError("换取业务 token 失败，请在学校页面确认账号状态")
        self.token = token
        return token

    def _headers(self) -> dict:
        return {
            "user-agent": UA,
            "content-type": "application/json; charset=utf-8",
            "deviceType": "4",
            "Authorization": self.token or "",
            "token": self.token or "",
            "AppCode": APP_CODE,
        }

    def post(self, path: str, payload: dict) -> dict:
        response = self.session.post(
            f"{API_QXJ}{path}",
            data=des_encrypt(payload, self.casual),
            headers=self._headers(),
            timeout=20,
        )
        try:
            result = response.json()
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise ZhxgError("学校接口返回格式异常")
        return result

    def dk_status(self, dklb: str = "PA") -> dict:
        return self.post("/qxj-padkglxx/queryKqDkbc", {"paramsData": {"dklb": dklb}})

    def check_location(self, jd: float, wd: float, dklb: str = "PA") -> dict:
        return self.post("/qxj-padkglxx/jcqqwzsjsfndk", {"paramsData": {"jd": jd, "wd": wd, "dklb": dklb}})

    def submit_dk(self, jd: float, wd: float, dkbc: str, dkdz: str = "", smsy: str = "", smfj: str = "",
                  sfwcdk: int = 0) -> dict:
        return self.post("/qxj-padkglxx/xspadk", {
            "jd": jd, "wd": wd, "dkbc": dkbc, "dkdz": dkdz, "smsy": smsy, "smfj": smfj, "sfwcdk": sfwcdk,
        })
