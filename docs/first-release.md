# 首次发行指南

本文用仓库内的 Flask 示例完成交付。发行方需要 Python 3.11、Docker，以及支持命名构建上下文和 `--no-cache-filter` 的 Docker Buildx。源码加密和原生运行时编译都在 Dockerfile 内完成，宿主机无需安装 C 编译器。以下以 `linux/amd64` 为例；其他系统、架构需分别构建和验证原生运行时。

只想先跑通示例，可按 [Flask 示例的完整命令](../examples/flask/README.md)从环境准备一直执行到接口验证。

查询某条命令的参数、输出文件和使用限制，见[完整命令参考](cli.md)。

## 1. 准备密钥

```sh
git clone https://github.com/baiyang/AppGuard.git
cd AppGuard
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install appguard-runtime==0.0.2
mkdir -p .data/delivery
python -m appguard keygen --out .data/issuer.key
python -m appguard code-keygen --out .data/products/example-web/code.key
```

已有仓库可跳过克隆。签名密钥只生成一次，每个产品的代码密钥也只生成一次，后续普通发布继续使用；上述生成命令不会覆盖已有密钥。三种密钥的区别见[密钥说明](keys.md)。

本指南从仓库取得 Flask 示例、Dockerfile 和镜像审计工具；Dockerfile 从 PyPI 安装固定版本 `appguard-runtime==0.0.2`，构建不依赖 AppGuard 仓库源码。仅使用自己的项目时可直接复制 Dockerfile，按下方说明调整应用配置。开发仓库内尚未发布的修改时，宿主机改为 `python -m pip install -e '.[test]'`；Dockerfile 仍使用固定的已发布版本。

示例按项目目录组织：`src/example_web/` 包内放置 `__init__.py`、`web_app.py` 和业务模块 `service.py`，`scripts/` 放置业务入口 `cli.py`、镜像构建脚本 `build.sh` 和交付验证脚本 `verify_delivery.py`；Dockerfile、依赖清单及 `guard.toml` 位于示例项目根目录。配置 `include = ["src/", "scripts/cli.py"]` 只选择业务包和业务命令，共加密四个 Python 模块，不交付构建、验证脚本。授权入口只限制 Web 业务请求，CLI 不检查许可证。

| 文件或目录 | 保存与交付 |
| --- | --- |
| `.data/issuer.key` | 发行方私密备份，用于签署清单和许可证，不交付 |
| `.data/issuer.pub` | 编译运行时的公钥，可以公开，无需单独交付 |
| `.data/products/example-web/code.key` | 发行方按产品私密备份，独立文件不交付，其值编入运行时 |

## 2. 直接构建并检查镜像

在仓库根目录执行以下命令，构建上下文只需示例应用目录。`application` 指向包含源码、`guard.toml` 和 `requirements.txt` 的应用目录；Dockerfile 内的 `protected-build` 阶段会调用已安装的 `appguard build` 加密源码、签署清单，再调用 `appguard build-runtime` 编译私有运行时 wheel。

```sh
docker buildx build --load --platform linux/amd64 \
  -f examples/flask/Dockerfile \
  --build-context application=examples/flask \
  --no-cache-filter protected-build \
  --secret id=issuer_key,src=.data/issuer.key \
  --secret id=publisher_public,src=.data/issuer.pub \
  --secret id=code_key,src=.data/products/example-web/code.key \
  -t example-web:001 examples/flask
python tools/audit_image.py --image example-web:001 --out .data/image-audit.json
```

不需要提前运行 `appguard build`、准备 release 目录或计算密钥摘要。后续发布复用密钥，修改镜像标签后重新执行上述构建和审计命令即可。

镜像构建也可使用等价的示例脚本，前三个参数依次是签名私钥、公钥和产品代码密钥，最后是镜像标签：

```sh
sh examples/flask/scripts/build.sh \
  .data/issuer.key .data/issuer.pub .data/products/example-web/code.key \
  example-web:001
```

省略镜像标签时默认使用 `example-web:001`。平台默认是 `linux/amd64`，可通过 `APPGUARD_PLATFORM` 环境变量调整。应用上下文仍为 `examples/flask/`，`guard.toml` 中的 `src/` 等路径均从该目录开始解析。

Dockerfile 通过只读构建挂载读取业务源码，通过 BuildKit secrets 临时读取三份密钥文件，只将安装后的运行时、依赖和加密应用复制到最终镜像。独立密钥文件、业务源码、发行工具和生成的 C 源码不进入交付镜像；客户无需编译工具。构建机器会接触源码和密钥，构建缓存中也有私有运行时产物，因此构建环境和缓存应由发行方私密管理，不作为交付物发布。

交付目录保留 `src/example_web/` 和 `scripts/cli.py` 层级，里面的 `.py` 均为加密模块加载入口。Dockerfile 设置 `PYTHONPATH=/app/src`，以 `example_web.web_app:app` 为 Gunicorn 入口；业务命令通过 `docker exec example-web python /app/scripts/cli.py` 执行，包导入使用 `example_web.service`。

**每次构建必须保留 `--no-cache-filter protected-build`。** BuildKit secret 内容变化不会自动使缓存失效；该参数让加密和运行时编译每次使用当前挂载的密钥，避免轮换密钥后复用旧产物。发行工具和系统依赖安装阶段仍可使用缓存。不要把密钥值通过 `--build-arg` 传入。

审计遍历交付镜像的所有层，包括后来被删除的文件，拒绝业务明文源码、明文字节码、生成的 C/Cython 源文件、已知密钥文件、预装许可证及带凭据的依赖来源 URL。默认业务目录为 `/app`；有自定义布局时通过 `--app-root`、`--bundle-root` 和可重复的 `--protected-path` 指定检查范围。审计不能证明二进制密钥不可提取，也不是任意文件内容中的秘密扫描器。

## 3. 签发并验证

```sh
python -m appguard issue \
  --product example-web --issuer-key .data/issuer.key \
  --customer customer-001 --expires 2027-12-31T23:59:59Z \
  --out .data/customer-001.license
```

这会生成原始签名 JSON。发行方留存 `.data/customer-001.license`，供检查、命令行导入和下面的验证工具使用；交付文件另外生成：

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

交付文件是对完整签名 JSON 编码得到的单行 Base64，文件已存在时不会覆盖。Base64 是可逆编码，不是加密；许可证的来源和完整性由签名校验。字段及校验规则见[许可证格式](keys.md#license-format)，需要命令行导入时见 [Base64 解码步骤](keys.md#base64-delivery)。

不需要部署公钥、授权申请或发布记录。按[部署说明](../examples/flask/DEPLOY.md)启动测试容器，在电脑浏览器中打开 `http://localhost:8000/_license/`（远程部署替换 `localhost`），选择 `.data/delivery/customer-001.b64.license` 并点击“激活授权”；也可把文件的完整单行内容粘贴到“授权码”。确认授权有效后，访问 `/api/answer?value=8` 应返回 `{"answer":50}`。使用测试许可证验证缺失、过期、篡改、错误产品和错误签名均使业务请求返回 403 JSON，并确认续期无需重启。

测试客户的授权卷不要预装到交付镜像。后续更新同产品镜像时，也应验证当前有效许可证可以继续使用。

### 自动验证

在仓库根目录、已激活的 Python 3.11 虚拟环境中，安装验证依赖并检查完整 Docker 交付流程：

```sh
python -m pip install -e '.[test]'
python examples/flask/scripts/verify_delivery.py \
  --image appguard-ci:local --out .data/delivery-verification.json
```

验证器在临时目录生成测试密钥，通过示例 `scripts/build.sh` 调用 Dockerfile 完成加密和运行时编译，审计镜像，再启动隔离测试容器检查授权、到期、续期和业务 CLI。镜像中的发行工具来自 Dockerfile 固定的 PyPI 版本，因此此验证检查已发布工具包的交付流程。它不读取已有客户授权，也不需要宿主机 C 编译器。

开发 AppGuard 本身时，运行 `python -m pytest -q` 检查当前仓库源码，包括尚未发布的修改。这组测试直接在宿主机临时目录中加密代码并编译原生运行时，需要可用的 C 编译器和 Python 3.11 开发头文件。

对已启动的示例测试容器，再运行真实 HTTP 验证：

```sh
python -m tools.verify_http \
  --url http://localhost:8000 \
  --issuer-key .data/issuer.key --product example-web \
  --restore-license .data/customer-001.license \
  --restart-container example-web \
  --out .data/http-verification.json
```

只对专用测试部署执行此命令，并按实际端口、容器名和许可证路径调整参数。验证器会临时安装短期许可证，等待到期后检查业务请求被阻断，再检查重启后的授权状态；它会在 `finally` 中尝试重新导入 `--restore-license` 指定的有效许可证，即使验证失败也会执行恢复。测试会短暂中断业务访问，恢复失败时应按输出错误处理并手动导入该许可证。

## 4. 导出交付

```sh
docker image save -o .data/delivery/example-web-001.tar example-web:001
cp examples/flask/DEPLOY.md .data/delivery/DEPLOY.md
```

将 `.data/delivery/` 中的以下文件交给对应客户：

- `example-web-001.tar`：应用镜像。
- `DEPLOY.md`：启动和激活步骤。
- `customer-001.b64.license`：第 3 步生成的 Base64 许可证，可直接在授权页面上传或粘贴。

原始 JSON `.data/customer-001.license` 由发行方留存管理，不放入交付目录。实际项目另附配置模板、数据库初始化步骤和发行方联系方式。Base64 许可证也可以通过离线渠道单独交付，安装和运行都不要求连接发行方服务器。

## 换成自己的项目

1. 按 [README](../README.md#接入自己的后端)在最外层接入插件或中间件，准备仅包含产品标识和文件选择的 `guard.toml`。
2. 为该产品生成一次 `code.key`，将构建命令中的 `code_key` secret 指向该文件；继续使用对应的发行方密钥对。
3. 在项目根目录放置 `guard.toml` 和包含服务进程依赖的 `requirements.txt`，将 `--build-context application` 指向项目根目录。采用 `src/` 布局时，配置中的交付范围仍写作 `src/`；额外交付的业务脚本逐个列出。
4. 调整 Dockerfile 的启动命令、工作目录、`PYTHONPATH`、端口及系统依赖。例如代码放在 `src/myapp/` 时，使用 `PYTHONPATH=/app/src` 和 `gunicorn myapp.web:app`。
5. 构建、审计和验证后导出镜像，同步部署说明中的镜像名、授权卷和配置。

可将示例 Dockerfile 复制到自己的项目。Flask 示例通过 `python -m pip install --no-cache-dir appguard-runtime==0.0.2` 安装发行工具，可编译包含 Flask 和 ASGI 支持的运行时，无需复制 AppGuard 仓库源码。FastAPI 项目还需在 `requirements.txt` 中加入 `fastapi` 和所选 ASGI 服务器（如 `uvicorn`），并调整启动命令。需要复用构建脚本时，同时复制 `scripts/build.sh` 并保留它与项目根目录的相对位置。

在自己的项目根目录使用 `-f Dockerfile --build-context application=.`，并将构建命令末尾的默认上下文设为 `.`，保留三个 `--secret` 和 `--no-cache-filter protected-build` 参数。将密钥保存在应用源码目录之外，并调整 secret 的文件路径；默认构建上下文用 `.dockerignore` 排除版本库和开发产物。镜像审计工具可继续从 AppGuard 仓库执行。

前端源码和静态资源不是加密目标。前端可按授权错误码统一跳转授权页；后端对所有业务请求保持 403 JSON，不能依赖浏览器重定向来阻断接口调用。中间件不能覆盖由 Nginx 等其他进程直接提供的资源。

## 可选：不使用 Docker

自行管理部署时，可单独构建加密包和产品运行时。以下命令在与目标部署相同的系统、CPU 架构和 CPython 3.11 环境中执行；编译运行时需要 C 编译器和 Python 开发头文件。

```sh
appguard build \
  --source examples/flask --config examples/flask/guard.toml \
  --issuer-key .data/issuer.key \
  --code-key .data/products/example-web/code.key \
  --out .data/releases/example-001
appguard build-runtime --public-key .data/issuer.pub \
  --code-key .data/products/example-web/code.key \
  --out .data/runtime/example-web
```

两个输出目录都必须尚不存在；以后按需选择新目录。加密包只包含 `bundle/tree/` 加载入口、`bundle/modules/` 密文和 `bundle/manifest.json` 签名清单，不生成密钥文件或私密发布记录。构建运行时的隔离环境会下载固定版本的 Cython、setuptools 和 wheel。

使用本指南安装的 `0.0.2` 发行工具生成的 `appguard_product_runtime-0.0.2-*.whl` 只适用于构建时的系统、架构和 CPython 版本，包含客户侧插件和编入产品密钥的原生模块，只应随对应产品私下交付，不能上传公共包仓库。客户使用 `python -m pip install /path/to/appguard_product_runtime-0.0.2-*.whl` 安装，无需编译器；FastAPI 项目按[接入说明](integration.md#安装产品运行时)添加 `[fastapi]` 依赖扩展。部署时安装业务依赖，将 `bundle/tree/` 作为应用目录，并通过 `APPGUARD_BUNDLE` 指向包含清单和密文的 `bundle/` 目录。

示例的业务包位于该应用目录下的 `src/example_web/`，因此还需将 `PYTHONPATH` 设置为 `bundle/tree/src/` 的绝对路径，再以 `gunicorn example_web.web_app:app` 启动。独立业务命令位于 `bundle/tree/scripts/cli.py`，使用同一 `PYTHONPATH` 执行。

## 更新与授权范围

相同产品和发行方的普通应用更新可复用有效许可证。更换代码密钥需要重建全部模块和运行时，但仍可使用原有效产品许可证；更换签名私钥则需要重新交付运行时并重新签发许可证。同一产品许可证可以复制到不同安装实例，当前不提供机器绑定。
