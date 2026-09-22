"""单次打卡核心：只依赖学校客户端和位置校验，不读取配置、不落库、不调度。"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from .. import buildings
from .zhxg import ZhxgClient, new_session


@dataclass(frozen=True)
class Result:
    status: str
    message: str


class StatusQueryError(RuntimeError):
    """登录后的业务查询失败，与 CAS 登录失败区分。"""


def _status(client) -> tuple[str, dict]:
    response = client.dk_status()
    if not isinstance(response, dict):
        raise StatusQueryError("学校打卡状态查询返回异常，请稍后再试")
    code = str(response.get("code", ""))
    if code == "331":
        return code, {}
    if code != "200" or not isinstance(response.get("data"), dict):
        safe_code = code if code.isascii() and code.isdigit() and len(code) <= 3 else "未知"
        raise StatusQueryError(f"学校打卡状态查询失败（返回码 {safe_code}），请稍后再试")
    return code, response["data"]


def execute(client, *, submit: bool = False) -> Result:
    """使用已登录客户端；最多提交一次，提交结果不确定时只查询、不重发。"""
    code, data = _status(client)
    if code == "331":
        return Result("no_task", "当前无打卡事项")
    if data.get("sfydk"):
        return Result("done", f"已打卡，时间：{data.get('dksj') or '学校未返回'}")
    if not data.get("kdk"):
        return Result("waiting", f"当前不可打卡：{data.get('bkyy') or '学校暂未开放'}")
    if not submit:
        return Result("ready", "当前可以打卡；本次仅查询，未提交")
    if not data.get("dkbc"):
        raise RuntimeError("学校未返回打卡班次，未提交")

    coordinate, name, verdict, _source = buildings.for_student(client)
    if not verdict.get("canDk"):
        return Result("failed", "学校位置校验未通过，未提交")
    try:
        response = client.submit_dk(jd=coordinate[0], wd=coordinate[1], dkbc=data["dkbc"], dkdz=name)
        accepted = str(response.get("code", "")) == "200" if isinstance(response, dict) else None
    except (requests.RequestException, TimeoutError, ConnectionError):
        accepted = None

    # 无论提交返回成功、异常还是超时，都以只读复核为准，避免重复提交。
    try:
        code, confirmed = _status(client)
    except (requests.RequestException, RuntimeError, TimeoutError, ConnectionError):
        return Result("unconfirmed", "已尝试提交，但无法查询确认结果；请到学校页面核实，勿连续重试")
    if code == "200" and confirmed.get("sfydk"):
        return Result("success", f"打卡成功，时间：{confirmed.get('dksj') or '学校未返回'}")
    if accepted is False:
        return Result("failed", "学校未接受此次提交，请到学校页面查看原因")
    return Result("unconfirmed", "已尝试提交，但学校尚未确认已打卡；请到学校页面核实，勿连续重试")


def run_once(username: str, password: str, *, submit: bool = False, force_logout: bool = False) -> Result:
    """会话仅存在内存中，不读取服务端 .env，不写密码、Cookie 或验证码图片。"""
    session = new_session()
    try:
        client = ZhxgClient(session=session, force_logout=force_logout)
        client.login(username, password)
        return execute(client, submit=submit)
    finally:
        session.close()
