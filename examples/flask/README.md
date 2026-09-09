# Flask 示例：从源码构建并运行

本示例使用 Dockerfile 从公共 PyPI 安装 `appguard-runtime==0.0.1`，在镜像构建期间加密 Python 源码并编译产品运行时。流程是：准备环境、生成一次密钥、构建镜像、启动容器、导入许可证。

以下命令适用于 macOS / Linux 的 shell，均在 **AppGuard 仓库根目录**执行。克隆仓库是为了获取示例文件和验证工具，不是安装 AppGuard 的必要步骤；接入自己的项目无需克隆。已有仓库时直接进入该目录；否则先获取示例：

```sh
git clone https://github.com/baiyang/AppGuard.git
cd AppGuard
```

## 示例目录

```text
examples/flask/
+-- src/
|   +-- example_web/
|       +-- __init__.py
|       +-- web_app.py       Flask 应用与授权中间件
|       +-- service.py       业务逻辑
+-- scripts/
|   +-- cli.py               业务命令行入口
|   +-- build.sh             Docker 镜像构建脚本
|   +-- verify_delivery.py   示例交付验证脚本
+-- Dockerfile
+-- guard.toml
+-- requirements.txt
+-- README.md
+-- DEPLOY.md
```

业务代码放在 `src/example_web/` 包内，命令入口及构建、验证脚本放在 `scripts/`。`guard.toml` 使用 `include = ["src/", "scripts/cli.py"]` 选择交付文件，包含包入口在内共加密四个 Python 模块；`build.sh` 和 `verify_delivery.py` 不在交付范围内。镜像审计和 HTTP 验证的通用工具仍位于仓库根目录的 `tools/`。

## 1. 准备环境

安装 Python 3.11、Docker 和 Docker Buildx，并启动 Docker。构建需要访问 Docker Hub、Debian 软件源和 PyPI；这里以 `linux/amd64` 镜像为例。Buildx 需要支持命名构建上下文和 `--no-cache-filter`。

```sh
docker info
docker buildx version
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install appguard-runtime==0.0.1
```

宿主机的 AppGuard 用于生成密钥和签发许可证。源码加密、C 编译器安装、运行时编译和 `requirements.txt` 依赖安装均由 Dockerfile 完成。

## 2. 生成密钥（只执行一次）

```sh
appguard keygen --out .data/issuer.key
appguard code-keygen --out .data/products/example-web/code.key
```

第一条命令同时生成签名私钥 `issuer.key` 和公钥 `issuer.pub`；第二条生成产品代码密钥。已有对应密钥时跳过这两条命令，后续构建继续复用。密钥保存在应用目录之外，不作为普通文件复制进镜像。

示例目录已经包含全部源码、脚本、依赖清单和 `guard.toml`，无需额外生成构建输入。`guard.toml` 指定产品 `example-web`；其中的文件路径相对于 `examples/flask/` 项目根目录。

## 3. 根据 Dockerfile 构建镜像

```sh
docker buildx build --load --platform linux/amd64 \
  -f examples/flask/Dockerfile \
  --build-context application=examples/flask \
  --no-cache-filter protected-build \
  --secret id=issuer_key,src=.data/issuer.key \
  --secret id=publisher_public,src=.data/issuer.pub \
  --secret id=code_key,src=.data/products/example-web/code.key \
  -t example-web:001 examples/flask
```

这是本示例的 Docker 构建命令。使用 `docker buildx build` 是为了传入应用上下文、密钥和指定阶段的缓存选项；单独执行 `docker build .` 缺少这些输入。

`-f` 指定 Dockerfile；`application` 提供源码、配置和依赖清单；三个 `--secret` 提供第 2 步生成的密钥文件。`--load` 将镜像载入本机供 `docker run` 使用。每次构建都保留 `--no-cache-filter protected-build`，使加密和编译使用当前密钥。

也可执行示例的构建脚本，效果与上述命令相同：

```sh
sh examples/flask/scripts/build.sh \
  .data/issuer.key .data/issuer.pub .data/products/example-web/code.key \
  example-web:001
```

前三个参数依次是签名私钥、公钥和代码密钥，第四个参数是镜像标签，省略时使用 `example-web:001`。脚本会定位示例项目目录并传入 Docker 构建所需选项，默认平台为 `linux/amd64`；需要其他平台时可在命令前设置 `APPGUARD_PLATFORM`，并同步调整后续 `docker run --platform`。

构建成功后得到 `example-web:001`。不需要提前执行 `appguard build` 或准备加密包目录。可检查镜像是否残留业务明文源码、独立密钥文件等：

```sh
python tools/audit_image.py --image example-web:001 --out .data/image-audit.json
```

加密产物保留项目目录结构：容器中的 `/app/src/example_web/` 和 `/app/scripts/cli.py` 是加载入口，密文与签名清单位于 `/opt/appguard/bundle/`。Dockerfile 设置 `PYTHONPATH=/app/src`，让 Python 能找到 `example_web` 包，并通过 `gunicorn --bind 0.0.0.0:8000 example_web.web_app:app` 启动服务。

## 4. 启动容器，在浏览器中查看授权页面

以下步骤按 Docker 和浏览器都在你当前使用的电脑上说明。在终端执行：

```sh
docker volume create example-web-license
docker run -d --name example-web --platform linux/amd64 \
  -p 127.0.0.1:8000:8000 \
  --mount type=volume,source=example-web-license,target=/var/lib/appguard \
  example-web:001
```

等几秒钟，让应用完成启动，然后：

1. 在这台电脑上打开浏览器，例如 Chrome、Edge 或 Safari。
2. 在浏览器顶部的地址栏输入 `http://localhost:8000/_license/`，按回车。也可点击这个完整地址：[http://localhost:8000/_license/](http://localhost:8000/_license/)。
3. 确认页面标题为“产品授权”，产品标识为 `example-web`。首次使用时，页面应显示“授权状态：尚未激活”，下方有“授权码”输入框、“许可证文件”选择框，以及“激活授权”按钮。

这就是示例应用内置的许可证管理页面，用于查看授权状态和导入许可证。打开页面不需要先有许可证；接下来第 5 步会生成并导入一份测试许可证。

`localhost` 表示当前这台电脑，`8000` 是启动命令映射的端口，`/_license/` 是授权页面的路径。本示例只开放本机访问；Docker 运行在远程服务器时，请按[服务器部署说明](DEPLOY.md)配置访问地址。

还可在终端检查业务接口：

```sh
curl --retry 20 --retry-connrefused --retry-delay 1 \
  -i 'http://localhost:8000/api/answer?value=8'
```

尚未导入许可证时，这条命令应返回 HTTP 403，JSON 中的 `code` 为 `LICENSE_MISSING`。这是业务接口在等待激活时的正常结果，完成第 5 步后再验证即可。

如果浏览器提示无法连接，请在终端运行 `docker logs --tail 100 example-web` 检查启动日志。若 8000 端口被占用，可将启动命令改为 `-p 127.0.0.1:8080:8000`，浏览器地址相应改为 `http://localhost:8080/_license/`，后续 `curl` 也使用 8080。已有同名容器时需更换容器名，并同步调整后续命令；首次试用应使用尚未安装许可证的授权卷。

## 5. 签发、导入并验证许可证

回到终端，生成供本次试用的许可证文件：

```sh
appguard issue \
  --product example-web --issuer-key .data/issuer.key \
  --customer demo-customer --expires 2027-12-31T23:59:59Z \
  --out .data/demo-customer.license
```

到期时间必须晚于签发时的当前时间；输出文件已存在时请指定新文件名。命令成功后，原始签名 JSON 保存在仓库根目录下的 `.data/demo-customer.license`，由发行方留存，供检查和命令行导入使用。

接着，将完整许可证文件编码为单行 Base64，生成实际交付给客户的文件：

```sh
python - <<'PY'
import base64
from pathlib import Path

source = Path(".data/demo-customer.license")
target = Path(".data/delivery/demo-customer.b64.license")
encoded = base64.b64encode(source.read_bytes()).decode("ascii")
target.parent.mkdir(parents=True, exist_ok=True)
with target.open("x", encoding="ascii") as output:
    output.write(encoded + "\n")
print(target)
PY
```

成功后会打印 `.data/delivery/demo-customer.b64.license`；已有同名文件时不会覆盖，请更换输出路径。Base64 是可逆编码，不是加密，许可证的来源和完整性由数字签名校验。许可证中的字段、校验规则及编码层次见[许可证格式说明](../../docs/keys.md#license-format)。

回到第 4 步打开的“产品授权”网页：

1. 找到“许可证文件”，点击文件选择按钮，选中刚生成的 `.data/delivery/demo-customer.b64.license`。“授权码”输入框可以留空。
2. 点击“激活授权”。
3. 确认页面显示“授权状态：授权有效”和到期时间，无需重启容器。

`.data` 是隐藏目录。如果文件选择窗口中找不到它，可以在终端执行以下命令，将输出的完整单行内容粘贴到网页的“授权码”输入框，再点击“激活授权”。不要截取内容、插入换行或添加引号：

```sh
cat .data/delivery/demo-customer.b64.license
```

也可以通过命令行导入，与网页操作任选一种即可。命令行仅接受 `issue` 生成的原始签名 JSON，所以下面仍使用发行方留存的 `.data/demo-customer.license`，不能换成 `.b64.license`；只有交付文件时，先按[解码步骤](../../docs/keys.md#base64-delivery)恢复 JSON：

```sh
docker exec -i example-web python -m appguard_host install /dev/stdin \
  < .data/demo-customer.license
```

使用命令行导入后，刷新浏览器中的“产品授权”页面即可看到新状态。最后，在终端检查业务接口和授权状态：

```sh
curl -i 'http://localhost:8000/api/answer?value=8'
docker exec example-web python -m appguard_host status
```

激活后接口应返回 HTTP 200 和 `{"answer":50}`。排查启动问题可执行 `docker logs --tail 100 example-web`。

容器运行时，还可执行示例业务命令：

```sh
docker exec example-web python /app/scripts/cli.py --value 8
```

该脚本通过 `example_web.service` 调用相同的业务逻辑，上述命令应输出 `50`。它也经过源码加密，但不检查许可证；AppGuard 的授权中间件只控制 HTTP 业务请求，因此此命令在尚未激活时也能执行。

试用结束后执行 `docker stop example-web`；再次启动使用 `docker start example-web`，授权卷继续保留。

## 自动验证完整流程

完成第 1 步后，也可直接运行自动验证，无需先执行第 2 至第 5 步：

```sh
python -m pip install 'requests>=2.32,<3'
python examples/flask/scripts/verify_delivery.py \
  --image appguard-ci:local --out .data/delivery-verification.json
```

验证脚本会打印执行的命令，在临时目录生成测试密钥和许可证，调用 `scripts/build.sh` 构建镜像，再执行镜像审计、HTTP 授权测试和 `/app/scripts/cli.py` 业务命令验证，并在结束时移除测试容器。它使用独立容器名和本机临时端口，不读取已有客户授权。镜像内的发行工具来自 Dockerfile 固定的公共 PyPI 版本。

每条 AppGuard 命令的参数和输入输出见[命令参考](../../docs/cli.md)；更换自己的应用、导出镜像和后续发布见[首次发行指南](../../docs/first-release.md)；收到发行方镜像文件后的部署步骤见[客户部署说明](DEPLOY.md)。
