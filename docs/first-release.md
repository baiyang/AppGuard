# 首次发行指南

本文用仓库内的 Flask 示例完成交付。发行方需要 Python 3.11、Docker 和支持命名构建上下文的 Docker Buildx。以下以 `linux/amd64` 为例；其他系统、架构需分别构建和验证原生运行时。

## 1. 准备密钥

```sh
git clone https://github.com/baiyang/AppGuard.git
cd AppGuard
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install appguard-runtime==0.0.1
mkdir -p .data/releases .data/delivery
python -m appguard keygen --out .data/issuer.key
python -m appguard code-keygen --out .data/products/example-web/code.key
```

已有仓库可跳过克隆。签名密钥只生成一次，每个产品的代码密钥也只生成一次，后续普通发布继续使用；上述生成命令不会覆盖已有密钥。三种密钥的区别见[密钥说明](keys.md)。

仅使用自己的项目时无需克隆仓库，安装 PyPI 工具包即可运行 `appguard`；本指南克隆仓库是为了取得 Flask 示例、Dockerfile 和镜像审计工具。开发仓库内尚未发布的修改时，改为 `python -m pip install -e '.[test]'`。

示例包含 `web_app.py`、业务模块 `service.py`、独立命令行 `cli.py`、依赖清单及 `guard.toml`。三个 Python 模块整体加密；授权入口只限制 Web 业务请求，CLI 不检查许可证。

## 2. 构建加密应用

```sh
python -m appguard build \
  --source examples/flask --config examples/flask/guard.toml \
  --issuer-key .data/issuer.key \
  --code-key .data/products/example-web/code.key \
  --out .data/releases/example-001
```

每次使用新输出目录，如 `example-002`。构建输出只包含 `bundle/tree/` 加载入口、`bundle/modules/` 密文和 `bundle/manifest.json` 签名清单，不生成密钥文件或私密发布记录。

| 文件或目录 | 保存与交付 |
| --- | --- |
| `.data/issuer.key` | 发行方私密备份，用于签署清单和许可证，不交付 |
| `.data/issuer.pub` | 编译运行时的公钥，可以公开，无需单独交付 |
| `.data/products/example-web/code.key` | 发行方按产品私密备份，独立文件不交付，其值编入运行时 |
| `.data/releases/example-001/bundle/` | 制作镜像的输入，不含独立密钥文件 |

## 3. 制作并检查镜像

示例 Dockerfile 会从 `appguard/_runtime/` 编译私有的 `appguard-product-runtime` wheel，再安装到客户镜像中。若自行管理部署，也可直接从已安装的 PyPI 工具包编译：

```sh
appguard build-runtime --public-key .data/issuer.pub \
  --code-key .data/products/example-web/code.key \
  --out .data/runtime/example-web
```

此命令需要 C 编译器和 Python 3.11 开发头文件，构建隔离环境会下载固定版本的 Cython、setuptools 和 wheel。输出目录必须尚不存在；wheel 只适用于构建时的系统、架构和 CPython 版本，并包含产品代码密钥，因此只应随对应产品私下交付，不能上传公共包仓库。客户只需安装 wheel，无需编译器。

```sh
APPGUARD_PUBLIC_KEY_SHA256="$(python -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path(".data/issuer.pub").read_bytes()).hexdigest())')"
APPGUARD_CODE_KEY_SHA256="$(python -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path(".data/products/example-web/code.key").read_bytes()).hexdigest())')"
docker buildx build --load --platform linux/amd64 \
  -f examples/flask/Dockerfile \
  --build-context release=.data/releases/example-001/bundle \
  --build-context application=examples/flask \
  --secret id=publisher_public,src=.data/issuer.pub \
  --secret id=code_key,src=.data/products/example-web/code.key \
  --build-arg APPGUARD_PUBLIC_KEY_SHA256="$APPGUARD_PUBLIC_KEY_SHA256" \
  --build-arg APPGUARD_CODE_KEY_SHA256="$APPGUARD_CODE_KEY_SHA256" \
  -t example-web:001 .
python tools/audit_image.py --image example-web:001 --out .data/image-audit.json
```

Dockerfile 在独立构建阶段编译运行时，只将安装后的运行时、依赖和加密应用复制到最终镜像。独立密钥文件、业务源码和生成的 C 源码不进入交付镜像；客户无需编译工具。

BuildKit secret 内容变化不会自动使缓存失效。两个非秘密构建参数是密钥文件原始内容的 SHA256，Dockerfile 会核对参数与挂载文件后再编译。普通发布更换 bundle 不影响运行时构建层；更换密钥时必须重新计算参数，使运行时正确重编。不要把密钥本身通过 `--build-arg` 传入。

审计遍历交付镜像的所有层，包括后来被删除的文件，拒绝业务明文源码、明文字节码、生成的 C/Cython 源文件、已知密钥文件、预装许可证及带凭据的依赖来源 URL。默认业务目录为 `/app`；有自定义布局时通过 `--app-root`、`--bundle-root` 和可重复的 `--protected-path` 指定检查范围。审计不能证明二进制密钥不可提取，也不是任意文件内容中的秘密扫描器。

## 4. 签发并验证

```sh
python -m appguard issue \
  --product example-web --issuer-key .data/issuer.key \
  --customer customer-001 --expires 2027-12-31T23:59:59Z \
  --out .data/customer-001.license
```

不需要部署公钥、授权申请或发布记录。按[部署说明](../examples/flask/DEPLOY.md)启动测试容器，在 `/_license/` 导入许可证后，确认 `/api/answer?value=8` 返回 `{"answer":50}`。使用测试许可证验证缺失、过期、篡改、错误产品和错误签名均使业务请求返回 403 JSON，并确认续期无需重启。

测试客户的授权卷不要预装到交付镜像。后续更新同产品镜像时，也应验证当前有效许可证可以继续使用。

### 自动验证

在仓库根目录、已激活的 Python 3.11 虚拟环境中，补齐测试与原生编译依赖。版本约束与项目和示例保持一致：

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

测试会在临时目录生成测试密钥、加密示例并编译原生运行时，不需要 Docker，也不读取已有客户授权。开发机需有可用的 C 编译器和 Python 3.11 开发头文件。

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

## 5. 导出交付

```sh
docker image save -o .data/delivery/example-web-001.tar example-web:001
cp examples/flask/DEPLOY.md .data/delivery/DEPLOY.md
```

交付镜像、部署说明和对应客户的许可证。实际项目另附配置模板、数据库初始化步骤和发行方联系方式。许可证可以通过离线渠道单独交付，安装和运行都不要求连接发行方服务器。

## 换成自己的项目

1. 按 [README](../README.md#接入自己的后端)在最外层接入插件或中间件，准备仅包含产品标识和文件选择的 `guard.toml`。
2. 为该产品生成一次 `code.key`，调整 `build --source`、`--config`、`--code-key` 和输出路径。
3. 准备包含服务进程依赖的 `requirements.txt`，将 `--build-context application` 指向其目录。
4. 调整 Dockerfile 的启动命令、工作目录、端口及系统依赖，例如 `gunicorn myapp.web:app`。
5. 构建、审计和验证后导出镜像，同步部署说明中的镜像名、授权卷和配置。

前端源码和静态资源不是加密目标。前端可按授权错误码统一跳转授权页；后端对所有业务请求保持 403 JSON，不能依赖浏览器重定向来阻断接口调用。中间件不能覆盖由 Nginx 等其他进程直接提供的资源。

## 从旧版迁移

旧版采用函数体拆分、每次发布独立密钥和构建版本绑定许可证；新版采用 `format: 2`、`kind: manifest`、`layout: modules-v1` 的整模块包及产品许可证，旧包和旧许可证均不兼容。

1. 删除 `guard.toml` 中的 `protected_functions` 与 `checkpoints`，去掉仅为检查点保留的空函数。
2. 为产品生成固定 `code.key`，按上述命令重新加密全部业务模块、构建运行时和镜像。
3. 使用原发行方签名私钥和产品标识重新签发一次新版许可证，在新镜像中导入。保留业务数据，旧 `deployment.key` 不再使用。
4. 更新前端授权错误处理：业务请求现在统一返回 403 JSON，不再自动 302 跳转。

迁移完成后，相同产品和发行方的普通应用更新复用许可证，不再重新申请授权。更换代码密钥需要重建全部模块和运行时，但仍可使用原有效产品许可证；更换签名私钥则需要重新交付运行时并重新签发许可证。同一产品许可证可以复制到不同安装实例，当前不提供机器绑定。
