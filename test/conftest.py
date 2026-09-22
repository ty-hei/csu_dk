"""所有自动测试禁止真实网络请求，学校接口使用假会话。"""
import pytest
import requests


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("自动测试不允许真实网络请求")

    monkeypatch.setattr(requests.Session, "request", blocked)
