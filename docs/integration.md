# 后端接入

本文说明 Flask、FastAPI 及其他 WSGI/ASGI 应用的接入方式。产品构建与镜像交付见[首次发行指南](first-release.md)。

## 安装产品运行时

应用环境需要安装 `appguard build-runtime` 生成的产品专用运行时 wheel；示例镜像已完成安装。不同产品分别使用独立容器或虚拟环境。将下方路径替换为实际产物，系统、CPU 架构及 Python 版本需与 wheel 匹配：

```sh
python3.11 -m pip install /path/to/appguard_product_runtime-0.0.2-cp311-cp311-linux_x86_64.whl
```

使用 FastAPI 或下文的 ASGI 适配器时，为 wheel 添加 `[fastapi]`，安装适配器依赖：

```sh
python3.11 -m pip install '/path/to/appguard_product_runtime-0.0.2-cp311-cp311-linux_x86_64.whl[fastapi]'
```

应用仍需安装自身的框架和服务器依赖。复用 Flask 示例 Dockerfile 构建 FastAPI 项目时，在 `requirements.txt` 中加入 `fastapi` 和 ASGI 服务器（如 `uvicorn`），并调整启动命令为应用的 ASGI 入口。

## 授权规则

AppGuard 在业务入口统一检查 HTTP 请求的许可证，授权无效时返回 HTTP 403。授权入口无需有效许可证；显式配置的 `exempt_paths` 仅豁免完全匹配路径的 GET/HEAD 请求。默认没有业务路径豁免。

HTTP 的 `/_license` 及 `/_license/` 下路径由 AppGuard 接管，用于授权页面、状态查询和许可证导入。

WebSocket 仅在握手时检查授权，上述 HTTP 豁免不适用；已建立的连接不因许可证失效而主动断开。

## Flask

在应用路由和其他中间件配置完成后、应用启动前，最后注册 AppGuard，使授权检查位于业务入口外层：

```python
from flask import Flask
from appguard_flask import AppGuard

app = Flask(__name__)
# 配置业务路由和其他中间件。
AppGuard().init_app(app)
```

## FastAPI

同样在其他中间件配置完成后、应用开始处理请求前，最后注册 AppGuard：

```python
from fastapi import FastAPI
from appguard_fastapi import AppGuard

app = FastAPI()
# 配置业务路由和其他中间件。
AppGuard(app)
```

应用工厂可使用 `init_app`：

```python
from fastapi import FastAPI
from appguard_fastapi import AppGuard

def create_app():
    app = FastAPI()
    # 配置业务路由和其他中间件。
    AppGuard().init_app(app)
    return app
```

也可将注册语句替换为 FastAPI 原生写法，两种方式选择其一：

```python
from appguard_fastapi import LicenseMiddleware

app.add_middleware(LicenseMiddleware)
```

## 其他 WSGI / ASGI 应用

完成应用和其他中间件组装后，包装提供给服务器的入口。WSGI 应用使用：

```python
from appguard_host import LicenseMiddleware

application = LicenseMiddleware(application)
```

ASGI 应用使用：

```python
from appguard_fastapi import LicenseMiddleware

application = LicenseMiddleware(application)
```

## 可选存活探针

需要未激活时可用的存活探针时，由业务应用提供路由，并在注册时显式配置 `exempt_paths`。例如 FastAPI：

```python
from fastapi import FastAPI
from appguard_fastapi import AppGuard

app = FastAPI()

@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health():
    return {"ok": True}

# 配置其余业务路由和中间件后注册。
AppGuard(app, exempt_paths=("/healthz",))
```

Flask 的 `AppGuard`、FastAPI 原生 `add_middleware` 和 WSGI/ASGI 包装方式也接受相同的 `exempt_paths` 参数。豁免不按目录前缀匹配；探针只应报告进程存活，不暴露业务数据。
