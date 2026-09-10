# AppGuard 命令参考

本文说明 AppGuard 的全部命令、执行位置、参数和输出。第一次使用并希望直接运行应用时，请先按 [Flask 示例](../examples/flask/README.md)操作；准备交付自己的产品时，参考[首次发行指南](first-release.md)。

许可证的 JSON 结构、字段和运行时校验见[许可证格式](keys.md#license-format)；签发后如何生成客户使用的 Base64 授权码，见 [Base64 交付与解码](keys.md#base64-delivery)。

## 安装与查看帮助

发行方在自己的电脑或构建环境中安装公共 PyPI 包，使用 CPython 3.11：

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install appguard-runtime==0.0.2
appguard --help
```

`appguard-runtime` 是公共发行工具包的名称，安装后提供 `appguard` 命令。使用这些命令无需克隆 AppGuard 仓库；本文的构建示例引用了仓库中的 Flask 文件，需要先取得示例，并在仓库根目录执行。接入自己的项目时，将这些路径替换为自己的文件即可。

每个子命令都支持 `-h` / `--help`：

```sh
appguard keygen --help
appguard code-keygen --help
appguard build --help
appguard build-runtime --help
appguard issue --help
appguard inspect --help
```

`appguard` 与 `python -m appguard` 调用同一套发行命令。例如下面两条命令等价：

```sh
appguard issue --help
python -m appguard issue --help
```

如果终端提示找不到 `appguard`，先激活安装时的虚拟环境，或使用该环境的 `python -m appguard`。

## 按使用阶段查找命令

| 执行位置 | 阶段 | 命令 | 用途 |
| --- | --- | --- | --- |
| 发行方 | 首次准备 | `appguard keygen` | 生成发行方签名私钥和公钥 |
| 发行方 | 新建产品 | `appguard code-keygen` | 生成该产品的代码加密密钥 |
| 发行方构建环境 | 构建版本 | `appguard build` | 加密 Python 模块并签署业务包清单 |
| 发行方构建环境 | 构建运行时 | `appguard build-runtime` | 编译包含公钥和产品代码密钥的私有运行时 wheel |
| 发行方 | 首次授权、续期 | `appguard issue` | 给指定产品和客户签发许可证文件 |
| 发行方 | 查看文件 | `appguard inspect` | 读取许可证或清单的元数据，不验证签名 |
| 客户应用环境或容器内 | 查看授权 | `python -m appguard_host status` | 校验并输出当前授权状态 |
| 客户应用环境或容器内 | 激活、续期 | `python -m appguard_host install` | 验证并安装许可证 |

**使用仓库的 Dockerfile 时，`build` 和 `build-runtime` 已在镜像构建期间自动执行。** 宿主机只需生成密钥、执行 [Docker 构建命令](../examples/flask/README.md#3-根据-dockerfile-构建镜像)、签发许可证，无需先手动运行这两个构建命令。

部署端的 `appguard_host` 来自产出的私有包 `appguard-product-runtime`，与公共发行工具包不同。示例镜像已安装私有运行时，客户直接在容器中执行部署命令即可。

## 通用约定

- 所有命令行相对路径都相对于执行命令时的当前目录。例如，在仓库根目录执行时，`.data/issuer.key` 指仓库中的 `.data/issuer.key`。
- `build` 配置中的 `include` / `exclude` 路径相对于 `--source`，不是相对于配置文件所在目录。`--config` 本身仍相对于当前目录。
- 生成密钥、许可证、加密包和运行时的命令会创建缺失的父目录；输出文件或输出目录必须尚不存在，即使已有目录为空也不能作为构建输出。已有密钥应继续复用，后续发布和续期使用新的输出路径。
- 除帮助外，命令成功时在终端标准输出打印 JSON。文件和目录写入各自的输出路径；终端中的 JSON 是结果说明，不是许可证文件本身。
- 命令正常完成时退出码为 `0`；缺少参数等用法错误通常为 `2`，命令处理失败通常为 `1`，错误写入标准错误。部署端 `status` 的授权结果还必须读取 JSON 的 `valid`，详见下文。

## `keygen`：生成发行方签名密钥

首次准备时执行一次。私钥用于签署业务包清单和许可证，公钥用于编译客户运行时；后续普通发布、给新客户授权和续期均复用对应密钥。

```sh
appguard keygen --out .data/issuer.key
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--out PATH` | 是 | 签名私钥的输出文件路径 |

同时生成 `.data/issuer.key` 和 `.data/issuer.pub`。公钥路径通过将私钥路径的后缀替换为 `.pub` 得到，因此私钥输出不能以 `.pub` 结尾。两个文件只要已有一个存在，命令就会失败，不覆盖原文件。

终端输出示例：

```json
{"private_key": ".data/issuer.key", "public_key": ".data/issuer.pub"}
```

两个文件均用 64 个十六进制字符保存 32 字节密钥，创建权限为 `0600`。私钥由发行方保存并备份，不能交付给客户。密钥用途和更换影响见[密钥说明](keys.md)。

## `code-keygen`：生成产品代码密钥

每个产品首次构建前执行一次。该密钥用于加密 Python 模块，并编入这个产品的私有运行时；同产品后续普通发布继续复用。

```sh
appguard code-keygen --out .data/products/example-web/code.key
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--out PATH` | 是 | 产品 AES-256 代码密钥的输出文件路径 |

生成指定文件，内容为随机 32 字节密钥对应的 64 个十六进制字符，创建权限为 `0600`。文件已存在时失败。终端输出示例：

```json
{"code_key": ".data/products/example-web/code.key"}
```

此命令不接收产品标识，发行方通过保存目录管理产品与密钥的对应关系。产品标识在 `guard.toml` 中设置；同一批加密包和运行时必须使用同一份代码密钥。

## `build`：构建加密业务包

每次发布业务代码时执行。Docker 示例自动执行本命令；以下示例用于理解参数或自行管理构建流程，需要 CPython 3.11，以及前面生成的密钥。

```sh
appguard build \
  --source examples/flask \
  --config examples/flask/guard.toml \
  --issuer-key .data/issuer.key \
  --code-key .data/products/example-web/code.key \
  --out .data/releases/example-001
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--source DIR` | 是 | 已存在的应用源码目录 |
| `--config FILE` | 是 | TOML 构建配置文件路径 |
| `--issuer-key FILE` | 是 | `keygen` 生成的签名私钥文件 |
| `--code-key FILE` | 是 | `code-keygen` 生成的产品代码密钥文件 |
| `--out DIR` | 是 | 尚不存在的发布输出目录 |

示例的 `examples/flask/guard.toml` 内容为：

```toml
product_id = "example-web"
include = ["src/", "scripts/cli.py"]
exclude = ["**/__pycache__/", "**/*.pyc", "**/.env*"]
```

此配置的源码根目录是 `examples/flask/`，不是它的 `src/` 子目录。它选择 `src/example_web/` 中的三个模块（含 `__init__.py`）和 `scripts/cli.py`，共四个 Python 模块。构建、验证脚本不在选择范围内，因此不会被加密或复制进交付包。

采用自己的 `src/` 项目时，在项目根目录执行构建，将上述命令的路径改为 `--source . --config guard.toml`，配置可继续使用 `include = ["src/", "scripts/cli.py"]`，并按实际文件调整。若只想以 `src/` 为源码根目录，则使用 `--source src` 并将配置改为例如 `include = ["example_web/"]`；此时根目录旁的 `scripts/` 不在源码范围内，不能使用 `../scripts/` 引入。示例同时交付业务包和业务脚本，所以使用项目根目录作为 `--source`。

| 配置字段 | 必填 | 说明 |
| --- | --- | --- |
| `product_id` | 是 | 产品标识，签发许可证时的 `--product` 必须与它一致 |
| `include` | 是 | 非空列表，可填写相对于 `--source` 的文件、目录或 glob 模式 |
| `exclude` | 否 | 要排除的相对路径或模式列表，默认 `[]` |

只支持以上三个配置字段。产品标识须为 1 至 128 个字符，不能有首尾空白或控制字符。文件选择不能使用绝对路径或 `..`，符号链接会被跳过，且必须选中至少一个 `.py` 文件。

选中的 `.py` 文件会整体编译并加密；其他资源文件会原样复制。不要将签名私钥或代码密钥纳入选择范围；选中的 `.pyc`、`.pyo`、`.pyw` 会导致构建失败，应在配置中排除。

构建成功后生成以下目录：

```text
.data/releases/example-001/
+-- bundle/
    +-- tree/           加载入口及原样复制的资源
    +-- modules/        加密模块文件（.agc）
    +-- manifest.json   已签名的业务包清单
```

`tree/` 保留相对于 `--source` 的目录层级，示例会生成 `tree/src/example_web/` 和 `tree/scripts/cli.py` 加载入口。启动应用时将 `tree/src/` 的绝对路径加入 `PYTHONPATH`，用 `example_web.web_app:app` 加载 Web 应用；示例 Dockerfile 已设置 `PYTHONPATH=/app/src`。

终端 JSON 包含 `product_id`、本次生成的 `build_id`、加密模块数量 `modules` 和 `bundle` 路径。每次构建生成新的 `build_id`，不会同时签发许可证或生成运行时。后续发布将 `--out` 改为新的目录，例如 `.data/releases/example-002`。

## `build-runtime`：编译产品运行时

将发行方公钥和产品代码密钥编入原生扩展，生成供该产品部署使用的私有 wheel。Docker 示例会安装编译器并执行本命令；在宿主机手动执行时，需要 C 编译器和 CPython 3.11 开发头文件。

```sh
appguard build-runtime \
  --public-key .data/issuer.pub \
  --code-key .data/products/example-web/code.key \
  --out .data/runtime/example-web
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--public-key FILE` | 是 | `keygen` 生成的签名公钥文件，不是私钥 |
| `--code-key FILE` | 是 | 与 `build` 使用的相同产品代码密钥文件 |
| `--out DIR` | 是 | 尚不存在的运行时输出目录 |

命令通过隔离构建环境编译，构建时需要获取构建依赖。成功后，输出目录中有一个 `appguard_product_runtime-0.0.2-*.whl` 文件，其中版本来自已安装的公共发行工具包；文件名的其余部分标明 Python 和平台信息。

终端 JSON 包含 `wheel`（生成的 wheel 文件路径）和 `distribution`（固定为 `appguard-product-runtime`）。该 wheel 只适用于对应系统、CPU 架构和 CPython 3.11 环境，包含产品代码密钥，应随对应产品私下交付，不能上传公共包仓库。

非 Docker 部署需在匹配的平台安装这个 wheel、业务依赖，并配置加密包路径，见[不使用 Docker 的步骤](first-release.md#可选不使用-docker)。公共包 `appguard-runtime` 本身不能代替这个产品运行时。

## `issue`：签发许可证或续期

由发行方在首次授权或续期时执行。需要产品标识和签名私钥，不需要客户机器信息、代码密钥或本次构建的 `build_id`。

```sh
appguard issue \
  --product example-web \
  --issuer-key .data/issuer.key \
  --customer demo-customer \
  --expires 2027-12-31T23:59:59Z \
  --out .data/demo-customer.license
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--product ID` | 是 | 与业务包 `product_id` 完全一致的产品标识 |
| `--issuer-key FILE` | 是 | 与产品运行时所含公钥对应的签名私钥文件 |
| `--customer NAME` | 是 | 客户标识或名称；含空格时使用引号，例如 `"Customer A"` |
| `--expires DATETIME` | 是 | 授权到期时刻，必须包含时区且晚于签发时的当前时间 |
| `--out FILE` | 是 | 尚不存在的许可证输出文件路径 |
| `--not-before DATETIME` | 否 | 授权开始生效的时刻，默认本次签发时的当前时间 |

产品标识限制与 `guard.toml` 相同。客户名称须为 1 至 512 个字符，不能有首尾空白或控制字符。

时间使用带时区的 ISO 8601 格式，例如 `2027-12-31T23:59:59Z` 中的 `Z` 表示 UTC；`2027-12-31T23:59:59+08:00` 表示北京时间。两个示例不是同一个时刻。不能省略时区，也不能只写日期；时间必须在支持的 Unix 时间范围内，即 1970 年起至 UTC 9999 年末。

需要指定生效时间时，增加 `--not-before`，并确保它早于到期时间：

```sh
appguard issue \
  --product example-web --issuer-key .data/issuer.key \
  --customer demo-customer \
  --not-before 2027-01-01T00:00:00+08:00 \
  --expires 2028-01-01T00:00:00+08:00 \
  --out .data/demo-customer-2027.license
```

许可证在开始时刻生效，到达到期时刻立即失效，即有效区间为 `not_before <= 当前时间 < expires_at`。部署端安装时也会检查时间，因此将来才生效的许可证必须等到生效后才能导入。执行示例时，应根据实际签发日期和合同期限调整时间。

生成的 `.license` 文件是包含 `payload` 和 `signature` 的原始签名 JSON 文件；字段定义和校验规则见[许可证格式](keys.md#license-format)。终端 JSON 包含文件路径 `license`、新生成的 `license_id`、以 UTC 表示的 `expires_at`，不是许可证文件内容。

`issue` 的输出仍为 JSON。交付时按 [Base64 交付步骤](keys.md#base64-delivery)对整个文件额外编码，得到单行的 `.b64.license`，让客户在授权页面上传文件或粘贴其完整内容。Base64 是可逆编码，不是加密；签名负责验证来源和防篡改。发行方保留原始 JSON，供 `inspect`、`appguard_host install` 等工具使用。

续期时再次执行 `issue`，使用相同产品标识和对应私钥，填写新的到期时间，并选择新的文件名，例如 `.data/demo-customer-renewed.license`。默认立即生效，客户导入后替换原许可证，运行中的应用无需重启；签发文件本身不会自动更新客户部署。

许可证面向产品，不绑定某次构建。同产品、同发行方的普通应用版本更新可继续使用原有效许可证。

## `inspect`：查看许可证或业务包清单

在发行方环境中查看文件内容，便于确认产品标识、客户名称、有效期或构建版本。本命令仅接受原始签名 JSON，不能直接读取 Base64 交付文件；手上只有 `.b64.license` 时，先按[解码步骤](keys.md#base64-delivery)还原后再查看。

```sh
appguard inspect .data/demo-customer.license
appguard inspect .data/releases/example-001/bundle/manifest.json
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `path` | 是 | 位置参数，直接填写许可证或签名清单的文件路径，不使用 `--path` |

终端输出格式为：

```json
{
  "verified": false,
  "metadata": {}
}
```

实际的 `metadata` 会填入文件中解码出的字段。许可证包括 `product_id`、`customer`、`license_id`、`issued_at`、`not_before`、`expires_at` 等，时间字段为 Unix 秒数；清单包括 `product_id`、`build_id` 等，但会省略 `modules` 模块列表。

**`verified: false` 表示本命令没有验证签名，不表示验签失败。** 即使显示成功，也不能据此认定文件可信、许可证未过期或可用于当前产品。部署端导入许可证时会执行实际验证。

## 部署端命令：`python -m appguard_host`

以下命令在已安装对应产品运行时、已配置加密业务包的应用环境中执行。使用 Flask 示例时，先按[示例 README](../examples/flask/README.md)启动名为 `example-web` 的容器，然后从宿主机终端执行以下 `docker exec` 命令。

查看帮助：

```sh
docker exec example-web python -m appguard_host --help
docker exec example-web python -m appguard_host status --help
docker exec example-web python -m appguard_host install --help
```

### `status`：查看当前授权状态

```sh
docker exec example-web python -m appguard_host status
```

`status` 不需要其他参数。未安装许可证时，终端 JSON 例如：

```json
{"valid": false, "code": "LICENSE_MISSING", "product_id": "example-web"}
```

授权有效时，`valid` 为 `true`，`code` 为 `LICENSE_VALID`，并包含 `product_id`、`customer`、`expires_at`（Unix 秒数）和 `license_id`。业务包无法加载时，`product_id` 可能为 `null`。

常见无效状态包括 `LICENSE_MISSING`（尚未激活）、`LICENSE_EXPIRED`（已到期）、`LICENSE_NOT_YET_VALID`（尚未生效）、`LICENSE_INVALID`（许可证无效）、`LICENSE_WRONG_PRODUCT`（产品不匹配）和 `BUNDLE_INVALID`（业务包校验失败）。

**读取状态成功时，即使 `valid` 为 `false`，命令也会正常退出，退出码为 `0`。** 自动化脚本须解析 JSON 的 `valid` / `code` 判断授权，不能仅检查退出码。

### `install`：导入或更新许可证

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `path` | 是 | 位置参数，填写执行环境中可读取的许可证文件路径 |

许可证保存在宿主机时，推荐通过标准输入传入容器。以下使用发行方留存的原始 JSON `.data/demo-customer.license`：

```sh
docker exec -i example-web python -m appguard_host install /dev/stdin \
  < .data/demo-customer.license
```

这里 `< .data/demo-customer.license` 由宿主机读取文件，`-i` 保持容器标准输入开放，`/dev/stdin` 是容器内供安装命令读取的路径。不要把宿主机的 `.data/...` 直接当成容器内已经存在的文件路径。

在非 Docker 的应用环境中，或文件已位于容器内时，直接指定该环境中的实际文件路径：

```sh
python -m appguard_host install /path/to/customer.license
python -m appguard_host status
```

导入文件须为 `issue` 生成的原始签名 JSON，大小不超过 64 KiB；不能直接传入 Base64 交付文件，需先按[解码步骤](keys.md#base64-delivery)恢复 JSON。命令验证签名、产品和有效期后，将许可证保存到授权目录中的 `license.json`，并打印与 `status` 相同结构的 JSON。安装失败时输出错误并以非零状态退出；未通过验证的文件不会替换现有许可证。

续期使用同一条安装命令，只需换成新许可证的原始 JSON。浏览器支持直接使用 Base64 交付文件：本机运行示例容器时，打开 [http://localhost:8000/_license/](http://localhost:8000/_license/)，在“许可证文件”处选择 `.b64.license` 文件，或把其完整单行内容粘贴到“授权码”，再点击“激活授权”。

### 部署路径配置

部署端通过环境变量指定路径，这些不是 `status` / `install` 的命令行参数：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APPGUARD_BUNDLE` | `/opt/appguard/bundle` | 包含 `manifest.json` 和 `modules/` 的加密业务包目录 |
| `APPGUARD_LICENSE_DIR` | `/var/lib/appguard` | 保存 `license.json` 和时间校验记录 `last-seen` 的可写持久目录 |

Flask 示例镜像已经使用默认业务包路径；启动命令把授权卷挂载到默认授权目录，因此上述 `docker exec` 无需额外参数。自定义部署时，应用进程和管理命令须使用相同的路径配置。状态检查也可能更新时间校验记录，因此授权目录需要可写。
