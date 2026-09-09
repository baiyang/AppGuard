# AppGuard

为 Python Web 项目提供整模块代码加密交付和离线产品授权。发行方交付镜像与许可证，客户导入许可证后即可使用后端接口。代码保护与授权判断独立，不需要额外部署授权服务器。

当前支持 **CPython 3.11**，提供 **Flask 插件和 WSGI 中间件**。ASGI 需要另行适配；运行环境的系统、CPU 架构和 Python 版本必须与原生运行时匹配。

## 安装

```sh
python3.11 -m pip install appguard-runtime==0.0.1
appguard --help
```

PyPI 上的 `appguard-runtime` 是发行方工具包，包含密钥生成、模块加密、许可证签发和原生运行时编译模板。它不包含任何产品密钥，也不直接安装客户侧的 `guard_runtime`、`appguard_host` 或 `appguard_flask`。`appguard` 的所有子命令也可通过 `python -m appguard` 调用。

为产品生成一次密钥：

```sh
appguard keygen --out .data/issuer.key
appguard code-keygen --out .data/products/example-web/code.key
```

然后按[首次发行指南](https://github.com/baiyang/AppGuard/blob/main/docs/first-release.md)直接构建镜像。示例 Dockerfile 从 PyPI 安装固定版本 `appguard-runtime==0.0.1`，加密源码、编译产品运行时并组装交付镜像，可直接复制到自己的项目使用。构建无需 AppGuard 仓库源码，宿主机无需预先生成加密包，也无需安装 C 编译器。密钥通过 BuildKit secrets 传入，最终镜像只包含运行依赖、产品运行时和加密应用。

后续发布复用密钥，重新执行镜像构建即可。每次构建都需保留指南中的 `--no-cache-filter protected-build`，确保加密和编译使用当前密钥。自行管理非 Docker 部署时，指南也提供独立构建加密包和私有运行时 wheel 的命令；不同产品应使用各自独立的容器或虚拟环境。

## 从这里开始

- **从示例源码构建并试用**：按 [Flask 示例](https://github.com/baiyang/AppGuard/blob/main/examples/flask/README.md)依次准备环境、生成密钥、执行 Docker 构建并启动验证。
- **第一次制作交付包**：按[首次发行指南](https://github.com/baiyang/AppGuard/blob/main/docs/first-release.md)完成打包、测试和交付。
- **已收到交付包**：按[示例部署说明](https://github.com/baiyang/AppGuard/blob/main/examples/flask/DEPLOY.md)启动应用并导入许可证。
- **接入自己的后端**：参考下方接入配置；密钥保存与轮换见[密钥说明](https://github.com/baiyang/AppGuard/blob/main/docs/keys.md)。
- **了解许可证内容和交付方式**：见[许可证格式](https://github.com/baiyang/AppGuard/blob/main/docs/keys.md#license-format)和 [Base64 交付步骤](https://github.com/baiyang/AppGuard/blob/main/docs/keys.md#base64-delivery)。
- **维护 AppGuard 版本**：见[版本与发布流程](https://github.com/baiyang/AppGuard/blob/main/docs/releases.md)。

## 命令总览

发行方安装公共包 `appguard-runtime` 后使用 `appguard`；部署端的 `appguard_host` 由产品专用运行时提供，示例镜像已安装。每条命令的完整参数、输入输出和使用示例见[命令参考](https://github.com/baiyang/AppGuard/blob/main/docs/cli.md)。

| 命令 | 在哪里执行 | 用途 |
| --- | --- | --- |
| `appguard keygen` | 发行方 | 生成签名私钥和对应公钥，首次准备时执行 |
| `appguard code-keygen` | 发行方 | 为产品生成代码加密密钥，后续构建复用 |
| `appguard build` | 发行方或 Docker 构建阶段 | 按 `guard.toml` 加密 Python 模块并签署清单 |
| `appguard build-runtime` | 发行方或 Docker 构建阶段 | 使用产品密钥编译部署端的原生运行时 wheel |
| `appguard issue` | 发行方 | 签发产品许可证，续期时指定新到期时间重新签发 |
| `appguard inspect` | 发行方 | 查看许可证或清单的元数据，不验证签名 |
| `python -m appguard_host status` | 应用容器或部署环境 | 查看当前产品的授权状态 |
| `python -m appguard_host install` | 应用容器或部署环境 | 导入许可证，激活或续期，无需重启 |

先生成一次密钥，再构建产品，最后签发并导入许可证。使用示例 Dockerfile 时，`build` 和 `build-runtime` 已在镜像构建期间自动执行。

在终端查看帮助：

```sh
appguard --help
appguard issue --help
python -m appguard --help
```

将 `issue` 换成其他子命令名即可查看相应参数。`appguard ...` 也可写成 `python -m appguard ...`，请使用已安装工具包的 Python 环境。

## 架构与边界

```mermaid
flowchart TB
    subgraph publisher["发行方"]
        source["Python 源码 + guard.toml"]
        issuer["签名私钥 issuer.key"]
        code_key["产品代码密钥 code.key"]
        build["编译并加密整模块<br/>签署 manifest.json"]
        issue["签发产品许可证"]
        source --> build
        issuer --> build
        code_key --> build
        issuer --> issue
    end
    subgraph customer["客户应用容器"]
        web["Flask / WSGI 后端<br/>最外层授权中间件"]
        portal["/_license/<br/>状态与许可证导入"]
        runtime["Cython 运行时<br/>模块验签解密、许可证验签"]
        bundle["加载入口 + 加密模块 + 签名清单"]
        license[("授权卷<br/>license.json + 时钟记录")]
        web --> runtime
        web --> portal
        bundle --> runtime
        license --> runtime
    end
    build --> bundle
    issue -.->|"离线交付许可证"| license
```

系统只维护三种长期密钥值：发行方签名私钥 `issuer.key`、对应验签公钥 `issuer.pub`、每产品固定的代码密钥 `code.key`。公钥和代码密钥编入运行时；签名私钥不交付。普通应用更新复用产品代码密钥，无需重新签发未到期的产品许可证。

- **代码保护**：构建端用 `compile` / `marshal` 生成整模块字节码，再用 AES-GCM 加密为 `.agc`。镜像内的 `.py` 仅为加载入口；运行时验签、解密后在内存执行，不将明文字节码写回磁盘。
- **后端授权**：中间件在每个业务请求进入应用之前验签并检查有效期。未授权、过期或许可证无效时，所有业务路径统一返回 **HTTP 403 JSON**，不依据 `Accept`、路径前缀或浏览器类型重定向。
- **授权页面**：`/_license/`、状态与导入接口独立开放。应用未授权也能启动，客户可随时导入续期许可证，无需重启。
- **前后端分离**：前端 HTML、JavaScript、CSS 不是加密目标。前端统一处理后端授权错误并跳转授权页；本项目不接管前端路由。不要把所有业务 403 都当作授权到期，应检查响应的授权错误码。

加密让客户拿不到可直接阅读的业务 Python 源码，不保证抵抗主机管理员提取二进制密钥、内存代码或修改运行程序。代码密钥随运行时交付，许可证不承载解密密钥；移除授权中间件后可以调用业务逻辑。这是精简方案的明确边界。

授权只控制新进入的 HTTP 业务请求，不控制 CLI、后台任务、直接函数调用，也不中断已开始的请求或流式响应。离线时钟回退检测只用于辅助发现异常，无法阻止管理员恢复整机或授权卷快照。产品许可证不绑定机器或构建版本，同一许可证可复制到同产品的其他部署。

## 接入自己的后端

Flask 项目在应用与其他中间件配置完成后，最后注册 AppGuard，使授权检查位于业务入口最外层：

```python
from appguard_flask import AppGuard

AppGuard().init_app(app)
```

其他 WSGI 后端在完成应用组装后包装入口：

```python
from appguard_host import LicenseMiddleware

application = LicenseMiddleware(application)
```

默认没有业务路径豁免。需要存活探针时，可显式配置 `AppGuard(exempt_paths=("/healthz",))` 或 `LicenseMiddleware(application, exempt_paths=("/healthz",))`；仅完全匹配路径的 GET/HEAD 请求免授权，不按目录前缀放行。探针应只报告进程存活，不暴露业务数据。

在 `guard.toml` 中选择交付文件，不需要配置函数、检查点或改写业务函数：

```toml
product_id = "my-web-app"
include = ["src/", "scripts/cli.py", "templates/", "static/"]
exclude = ["**/__pycache__/", "**/.env*", "**/*.pyc"]
```

路径相对于应用项目根目录，即 Docker 构建的 `application` 上下文或独立命令的 `build --source`，支持文件、目录和通配符；按自己的项目实际文件调整选择范围。选中的 Python 模块整体加密；非 Python 文件原样复制，需排除私密配置、开发文件和生成的 C 源文件。

[Flask 示例](https://github.com/baiyang/AppGuard/blob/main/examples/flask/README.md)采用 `src/example_web/` 业务包和 `scripts/` 脚本目录。其配置只交付 `src/` 和业务命令 `scripts/cli.py`，构建、验证脚本不进入镜像。加密后保留目录结构，Dockerfile 通过 `PYTHONPATH=/app/src` 加载业务包，以 `example_web.web_app:app` 启动 Gunicorn，并从项目根目录的 `requirements.txt` 安装依赖。系统依赖、数据库初始化及后台任务仍由业务项目自己的部署流程负责。

## 签发、激活与续期

发行方知道产品标识、客户标识和到期时间后即可签发，不再收集部署申请：

```sh
python -m appguard issue \
  --product example-web --issuer-key .data/issuer.key \
  --customer customer-001 --expires 2027-12-31T23:59:59Z \
  --out .data/customer-001.license
```

`issue` 输出原始签名 JSON，发行方留存该文件供检查和命令行导入。交付前，将整个文件编码为单行 Base64：

```sh
python - <<'PY'
import base64
from pathlib import Path

source = Path(".data/customer-001.license")
target = Path(".data/delivery/customer-001.b64.license")
encoded = base64.b64encode(source.read_bytes()).decode("ascii")
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x", encoding="ascii") as output:
    output.write(encoded + "\n")
print(target)
PY
```

把 `.data/delivery/customer-001.b64.license` 交给客户。客户在电脑浏览器中打开 `http://服务器地址:8000/_license/`，在“许可证文件”处选择收到的文件，点击“激活授权”；也可将文件的完整单行内容粘贴到“授权码”输入框。Base64 是可逆编码，不提供保密性；许可证由数字签名防篡改，详见[许可证格式与校验](https://github.com/baiyang/AppGuard/blob/main/docs/keys.md#license-format)。

授权有效后，示例的 `/api/answer?value=8` 返回 `{"answer":50}`。缺失或过期授权时，该接口及其他业务路径均返回 403 JSON。

续期时指定新的到期时间和输出文件，重新签发后导入即可。相同发行方和产品的新构建继续使用原有效许可证；不需要保存每个构建的秘密 `release.json`。授权卷只保存许可证和辅助时钟记录，重建容器时继续挂载。

部署端也可使用命令行查看或导入授权。`install` 仅接受 `issue` 生成的原始签名 JSON；只有 Base64 交付文件时，先按[解码步骤](https://github.com/baiyang/AppGuard/blob/main/docs/keys.md#base64-delivery)恢复原始文件：

```sh
python -m appguard_host status
python -m appguard_host install /path/to/customer-001.license
```

## 配置与迁移

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `APPGUARD_BUNDLE` | `/opt/appguard/bundle` | 加密模块及签名清单目录 |
| `APPGUARD_LICENSE_DIR` | `/var/lib/appguard` | 可写、持久化的授权目录 |
| `APPGUARD_SECURE_COOKIE` | `0` | HTTPS 部署时设为 `1` |

无法激活时查看 `/_license/` 的状态提示，确认许可证产品、发行方、有效期与系统时间。运行时需与产品代码密钥匹配，但不要求每次业务构建重新编译。

旧版 `function-bodies-v1` 包和绑定 `build_id` 的许可证不能直接用于新版。首次迁移需要移除旧函数配置、生成产品代码密钥、重新构建镜像，并重新签发产品许可证。此后普通更新可复用许可证；迁移细节见[首次发行指南](https://github.com/baiyang/AppGuard/blob/main/docs/first-release.md#从旧版迁移)。
