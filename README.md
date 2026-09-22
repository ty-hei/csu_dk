# csu-dk

中南大学个人本地打卡工具：一个简洁的 localhost 页面，以及可独立调用的单次业务核心。
手动查询或提交一次后结束本次会话，不托管账号，也不执行定时任务。

## 启动

支持目标为 **Apple Silicon Mac** 和 **Windows x64**，使用 Python 3.14。
Mac 已做本地启动验证；Windows 已配置 CI，尚未在本次工作中完成真机验收。
Intel Mac 和 Windows ARM64 不在支持范围内。

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在项目目录执行：

```bash
uv run --locked simple.py
```

浏览器打开 **http://127.0.0.1:8765/**，输入学号和密码：

- **只查询状态**：登录并查询，不进行位置校验或提交打卡。
- **打卡一次**：查询当前事项，跳过已完成或未开放的事项，校验位置后最多提交一次，再查询确认。
- **遇到单处登录时，登出其他 PC 并继续**：默认不勾选。勾选后遇到登录冲突，会提交学校官方继续登录表单，使其他电脑上的学校登录失效。

每次操作结束后至少等待 15 秒。提交超时不会自动重发；结果未确认时请在学校端核实。
位置使用仓库内的楼栋种子坐标和学校测距接口解析，**不是浏览器实时定位**。

按 **Ctrl+C** 停止服务；关闭浏览器不会停止服务。端口被占用时：

```bash
uv run --locked simple.py --port 8766
```

服务只监听 `127.0.0.1`，校验 Host、Origin 和一次性表单令牌，拒绝代理转发请求。
本地页面不提供远程访问认证，不应通过反向代理或 Tunnel 对外公开。

## 依赖隔离与清理

入口使用内联依赖和 `simple.py.lock`，只安装三个直接依赖：`requests`、`pycryptodome`、
`beautifulsoup4`，以及它们的传递依赖。不需要 `.env`、数据库、邮件服务、Web 框架或调度器。
请保留整个仓库目录，`simple.py` 会导入 `app/` 中的核心代码和楼栋种子文件。

首次运行需联网下载 Python 和依赖。准备好后可离线启动 uv：

```bash
uv run --offline --locked simple.py
```

`--offline` 只限制 uv；程序仍需联网访问学校。uv 不向系统 Python 安装包，默认将脚本环境放在
用户级缓存，将下载的 Python 放在 uv 管理目录。停止服务后可用 `uv cache clean` 清理共享缓存；
这会同时清理其他 uv 项目的缓存。Python 解释器由 uv 单独管理，不会随缓存清理而删除。

如果希望 Python 和依赖缓存都放在项目内、便于整目录清理，可在运行前设置：

macOS：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.uv-python"
uv run --managed-python --locked simple.py
```

Windows PowerShell：

```powershell
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
$env:UV_PYTHON_INSTALL_DIR = "$PWD/.uv-python"
uv run --managed-python --locked simple.py
```

停止服务后，删除 `.uv-cache` 和 `.uv-python` 即可清理这套运行环境；两目录已被 Git 忽略。
这不会卸载 uv 本身。删除后再次运行需要重新下载。

## 验证码

默认不安装 OCR。如果学校要求图形验证码，可停止服务后改用：

```bash
uv run --locked --with ddddocr simple.py
```

OCR 是可选附加依赖，不在脚本锁文件内，首次需要联网下载，识别不保证成功。
Windows 使用 OCR 可能需要 [Visual C++ 运行库](https://onnxruntime.ai/docs/install/#requirements)。
识别不可用时不提交空验证码；连续两次识别被拒后停止尝试。

## 数据与凭据

- 账号、密码、Cookie 和业务令牌仅用于当前请求，不写入文件；请求结束后关闭学校会话连接。
- 不保存验证码图片、登录 HTML、个人地址缓存或业务日志，不读取旧 `.env`、数据库或密钥。
- 密码不回填到响应页面，原始学校错误响应不展示；浏览器自身的密码保存功能由浏览器控制。
- 测试使用合成账号和假会话，默认阻止真实网络请求；不要把真实凭据写进源码、测试或命令行参数。
- `.gitignore` 排除了本地配置、旧数据目录、数据库、私钥、Cookie 文件和运行缓存。忽略规则无法替代提交前检查。

从旧版迁移时，先停止旧服务并卸载已安装的启动项，再将 `data/`、`.env` 等私人文件移到仓库外妥善保管。
新版不读取或迁移旧版账号数据。旧的邮箱登录、账号管理、SQLite、自动调度、管理 API、静态前端和启动项脚本已移除。

## 单次核心

```python
from getpass import getpass
from app.csu.workflow import run_once

result = run_once(
    input("学号："),
    getpass("密码："),
    submit=False,       # 显式设置 True 才提交打卡
    force_logout=False, # 显式设置 True 才在冲突时登出其他 PC
)
print(result.message)
```

此示例需在包含项目依赖的 Python 环境中执行。核心返回 `Result(status, message)`，
状态包括 `ready`、`done`、`no_task`、`waiting`、`success`、`failed`、`unconfirmed`。
登录或查询异常会抛出异常；`StatusQueryError` 表示已进入登录后的打卡状态查询阶段，但查询未成功。

```text
simple.py                 本地 HTTP 控制页与 uv 内联依赖
app/csu/workflow.py        单次查询、提交与结果确认
app/csu/cas.py             CAS 密码加密、验证码和单处登录处理
app/csu/zhxg.py            智慧学工登录及业务请求
app/csu/des.py             业务请求体加密
app/csu/ocr.py             可选验证码识别
app/buildings.py          位置解析与校验
app/data/buildings.json   楼栋种子坐标，不含账号记录
```

## 开发与验证

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
```

开发环境使用 `.venv` 和 `uv.lock`；脚本运行环境使用 `simple.py.lock`。修改运行依赖时同步更新
`pyproject.toml` 和 `simple.py` 的内联声明，再运行 `uv lock` 与 `uv lock --script simple.py`。

已验证本地页面、模拟业务流程及强制登出后的真实登录。最近一次学校状态查询返回业务码 `500`，
其原因尚未确认，**尚未完成真实打卡验收**。测试通过不代表学校当前会接受提交。
当前代码没有 Cloudflare Workers 部署入口。

MIT License，见 [LICENSE](LICENSE)。
