# 密钥与授权文件说明

AppGuard 将代码加密和产品授权分开处理。系统只维护三种长期密钥值，不再生成部署密钥、临时密钥交换、函数密钥或每次发布的秘密记录。部署命令见[首次发行指南](first-release.md)，接入与边界见 [README](../README.md)。

## 三种密钥

| 密钥 | 谁生成、何时生成 | 保存与使用 |
| --- | --- | --- |
| `issuer.key`：Ed25519 签名私钥 | 发行方首次执行 `keygen`，后续复用 | 发行方私密备份；签署模块清单和产品许可证，绝不交付 |
| `issuer.pub`：Ed25519 验签公钥 | 与 `issuer.key` 同时生成 | 可公开，编入 Cython 运行时，验证清单和许可证来源 |
| `code.key`：AES-256 产品代码密钥 | 每个产品执行一次 `code-keygen`，普通更新复用 | 发行方按产品备份；构建端加密全部模块，部署运行时内置其值用于解密 |

这三个文件均保存 32 字节密钥的十六进制文本。不要手工替换为口令、重新编码或填入任意字符串。公钥无需保密；签名私钥与代码密钥的作用不同，拿到代码密钥不能签发许可证。

```sh
python -m appguard keygen --out .data/issuer.key
python -m appguard code-keygen --out .data/products/example-web/code.key
```

每个产品应使用独立代码密钥。多个业务构建可以复用同一产品密钥，因此对应运行时也可复用；这同时意味着该密钥泄露会影响所有使用它加密的构建。

## 从构建到运行

| 环节 | 使用的密钥 | 结果 |
| --- | --- | --- |
| 构建模块 | `code.key` 加密 `compile` / `marshal` 生成的整模块字节码；`issuer.key` 签署清单 | Dockerfile 内调用 `appguard build`，生成 `.agc` 密文、Python 加载入口及 `manifest.json` |
| 编译运行时 | `issuer.pub` 和 `code.key` | Dockerfile 内调用 `appguard build-runtime`，公钥和代码密钥编入原生扩展，目标 Python/系统/架构需匹配 |
| 签发产品许可证 | `issuer.key` | 签署产品、客户、许可证标识和有效期信息；不读取代码密钥或构建目录 |
| 模块导入 | 内置公钥验签，内置代码密钥解密 | 在进程内执行模块；不要求许可证有效，所以授权页可以正常启动 |
| HTTP 业务请求 | 内置公钥和许可证 | 检查产品、签名与有效期；失败时由中间件返回 403 JSON |
| 导入或续期 | 内置公钥验证新许可证 | 验证通过后原子替换 `license.json`，无需重启 |

模块加密使用 AES-GCM；每个密文使用随机 nonce，密文与构建标识和模块路径关联。清单保存密文摘要及 `code_key_sha256`，使运行时能检测产物篡改或产品代码密钥错配。每个构建仍有 `build_id` 用于识别代码包，但许可证不再绑定它。

签名清单和许可证使用版本化封装。清单为 `format: 2`、`kind: manifest`、`layout: modules-v1`；产品许可证为 `format: 2`、`kind: license`。许可证内容只有 `product_id`、`customer`、`license_id`、`issued_at`、`not_before`、`expires_at` 等授权元数据，不含代码密钥、部署身份或 `wrapped_key`。

<a id="license-format"></a>

## 授权许可证格式

许可证有三个层次：授权数据、带签名的原始 JSON 文件、用于交付的 Base64 文本。`appguard issue` 生成的是第二层；交付前再编码为第三层。**Base64 是可逆编码，不是加密。授权信息可以解码查看，防篡改和来源校验由 Ed25519 数字签名保证。**

### 1. 授权数据

以下 JSON 是解码后的 `payload` 示例，仅用于说明字段，不能直接导入：

```json
{
  "format": 2,
  "kind": "license",
  "license_id": "0123456789abcdef0123456789abcdef",
  "product_id": "example-web",
  "customer": "customer-001",
  "issued_at": 1788937200,
  "not_before": 1788937200,
  "expires_at": 1830268799
}
```

| 字段 | 类型 | 含义与运行时校验 |
| --- | --- | --- |
| `format` | 整数 | 当前固定为 `2` |
| `kind` | 字符串 | 固定为 `license`，不能使用业务包清单代替许可证 |
| `license_id` | 字符串 | 签发时生成的许可证编号；运行时检查为 1 至 128 个字符，不做全局查重或在线吊销查询 |
| `product_id` | 字符串 | 产品标识，1 至 128 个字符，必须与当前加密业务包中的产品标识完全一致 |
| `customer` | 字符串 | 签发对象，1 至 512 个字符；记录许可证签给谁，不与部署端的客户身份比对 |
| `issued_at` | 整数 | 签发时刻，Unix 秒数；运行时检查类型和范围，不用它判断当前是否到期 |
| `not_before` | 整数 | 开始生效的时刻，Unix 秒数 |
| `expires_at` | 整数 | 到期时刻，Unix 秒数；当前时间达到该值时授权立即失效 |

八个字段都必须存在，不能增加额外字段。三个标识文本不能有首尾空白或控制字符；三个时间值须为整数且在 `0` 至 `253402300799` 范围内。有效期满足 `not_before <= 当前时间 < expires_at`，开始时间必须早于到期时间。

示例中的签发和生效时刻是北京时间 `2026-09-09 15:00:00`，到期时刻是北京时间 `2027-12-31 23:59:59`。签发命令接收带时区的日期时间，文件中保存的是 Unix 秒数，详见 [`issue` 参数](cli.md#issue签发许可证或续期)。许可证中没有机器指纹、代码解密密钥或构建编号 `build_id`。

### 2. 原始签名文件

`appguard issue --out .data/customer-001.license` 输出的文件为以下结构；其中的占位文本仅作示意：

```json
{
  "payload": "<授权数据 JSON 原始字节的 Base64 编码>",
  "signature": "<Ed25519 签名字节的 Base64 编码>"
}
```

外层只允许 `payload` 和 `signature` 两个字段。发行工具将授权数据按键排序、去除无关空白并转义非 ASCII 字符后序列化为 UTF-8 JSON，对这段原始字节签名，再分别对原始字节和签名字节做标准 Base64 编码。Ed25519 签名原始长度为 64 字节。

验签针对的是 `payload` 解码后的原始字节，不是 Base64 字符串，也不是整个外层 JSON。不要修改授权数据、重新序列化后仍沿用旧签名，或只把 `payload` 当作许可证交付。运行时使用产品原生模块内置的发行方公钥验证完整许可证。

### 3. Base64 交付文本

将上一步整个原始 `.license` 文件编码为单行标准 Base64，得到客户收到的授权码或 `.b64.license` 文件：

```text
授权数据 JSON -> Ed25519 签名 -> 原始签名 JSON -> 整体 Base64 编码 -> 客户交付文件
```

这里的整体编码是额外的一层，必须包含 `payload` 和 `signature`。使用标准 Base64 字母表及必要的 `=` 填充，保持单行，不加说明文字、引号或文件头尾标记。文件扩展名不决定内容格式；`.b64.license` 便于与发行方保留的原始 `.license` 文件区分，也可被授权页的文件选择器选中。

<a id="base64-delivery"></a>

## Base64 交付与导入

先按[签发命令](cli.md#issue签发许可证或续期)生成 `.data/customer-001.license`，然后在同一目录环境下执行：

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

脚本不会覆盖已有交付文件，重复执行时请使用新文件名。交给客户的是 `.data/delivery/customer-001.b64.license`；原始 `.data/customer-001.license` 留在发行方，用于查看元数据和管理。`issue` 在终端打印的路径、编号、到期时间等结果 JSON 不是许可证，不能对它编码后交付。

### 在浏览器中导入

1. 启动交付应用。在浏览器中打开它的“产品授权”页面；本机运行示例时地址为 `http://localhost:8000/_license/`。
2. 在“许可证文件”处选择收到的 `.b64.license` 文件，或用文本编辑器打开该文件，将完整的单行内容粘贴到“授权码”输入框。任选一种即可，同时填写时优先使用文件。
3. 点击“激活授权”。页面会先解码 Base64，再验证签名、产品和有效期；成功后显示“授权状态：授权有效”。

网页也兼容原始签名 JSON。Base64 文本的首尾空白会被去除，但中间的换行、空格会导致解码失败，因此不要使用自动按固定列宽换行的 Base64 输出。网页要求解码前的授权码或上传文件不超过 65,536 字节，整个提交表单不超过 100,000 字节。授权成功后，卷中的 `license.json` 保存的是解码后的签名 JSON。

### 使用命令行处理交付文件

当前版本的 `appguard inspect` 和 `python -m appguard_host install` 接收原始签名 JSON，不会自动解码外层 Base64。处理客户交付文件前，先还原为单独的文件：

```sh
python - <<'PY'
import base64
from pathlib import Path

source = Path(".data/delivery/customer-001.b64.license")
target = Path(".data/customer-001.decoded.license")
raw = base64.b64decode(source.read_bytes().strip(), validate=True)
with target.open("xb") as output:
    output.write(raw)
print(target)
PY
appguard inspect .data/customer-001.decoded.license
```

解码和 `inspect` 只用于还原、查看信息，不验证许可证是否可信或有效。要在运行中的示例容器中安装，使用：

```sh
docker exec -i example-web python -m appguard_host install /dev/stdin \
  < .data/customer-001.decoded.license
```

部署端安装时才会执行完整验证。原始文件和交付文件是同一份签名许可证的两种表示形式，Base64 编码不会改变客户、产品、有效期或授权范围。

## 交付与保密边界

| 文件或产物 | 处理方式 |
| --- | --- |
| `issuer.key` | 发行方私密保存，不放入镜像、源码交付包或客户卷 |
| `issuer.pub` | 公钥可公开，实际信任值已编入运行时，无需单独交付 |
| `code.key` | 独立文件不进入镜像，其密钥值编入运行时；发行方保留以便更新与重编 |
| `bundle/tree/`、`bundle/modules/`、`bundle/manifest.json` | 构建输出，随镜像交付，不生成 `functions/` 或密钥文件 |
| `guard_runtime` 原生扩展 | 随镜像交付，与发行方公钥、产品代码密钥及 Python/系统/架构匹配 |
| `.data/customer-001.license` | 发行方保留的原始签名 JSON，用于 `inspect` 或部署 CLI |
| `customer-001.b64.license` | 原始文件整体 Base64 编码后的客户交付文件，可在授权页上传或粘贴；CLI 使用前先解码 |
| `license.json`、`last-seen` | 客户授权卷内的许可证与辅助时钟记录，需持久化和备份，不预装进交付镜像 |

示例 Dockerfile 在独立构建阶段读取源码和密钥，最终镜像只复制运行依赖、私有运行时和加密应用。构建机器会接触源码和密钥，构建缓存中包含私有运行时产物，应由发行方私密管理；交付最终镜像，不发布构建环境和缓存。

**代码密钥的值随运行时交付。** 它以常量编入二进制，不等于对主机管理员保密。拥有二进制分析或进程管理能力的人可能提取密钥与运行代码；已提取密钥不会因许可证到期而失效。镜像检查可以发现独立密钥文件、业务明文源码等误交付，不能证明密钥无法从二进制提取。

当前目标是交付物没有直接可读的业务 Python 源码，并在正常部署中按有效期限制 HTTP 业务访问。许可证不参与模块解密，直接函数调用、CLI 和后台任务不受授权限制；移除中间件也能调用业务逻辑。已进入执行或开始输出的流式请求不会在到期时被强制中断。

产品许可证没有机器绑定，同一许可证可以在相同发行方和产品的多个安装实例使用。离线时钟回退检测只提供辅助判断，管理员能够修改系统时间或恢复授权卷快照；当前不提供在线吊销或可信硬件时间保证。

## 编译参数与运行配置

| 参数 | 设置时间 | 值的来源 |
| --- | --- | --- |
| `APPGUARD_PUBLIC_KEY` | 编译运行时 | `issuer.pub` 中的十六进制密钥文本 |
| `APPGUARD_CODE_KEY` | 编译运行时 | `code.key` 中的十六进制密钥文本 |
| `--secret id=issuer_key,src=...` | Docker 构建 | `issuer.key` 文件，用于签署加密包清单 |
| `--secret id=publisher_public,src=...` | Docker 构建 | `issuer.pub` 文件，用于编译运行时 |
| `--secret id=code_key,src=...` | Docker 构建 | 产品 `code.key` 文件，用于加密模块和编译运行时 |
| `--no-cache-filter protected-build` | 每次 Docker 构建 | 重新执行加密和运行时编译，确保使用当前密钥 |
| `APPGUARD_BUNDLE` | 部署运行时 | 加密包路径，默认 `/opt/appguard/bundle` |
| `APPGUARD_LICENSE_DIR` | 部署运行时 | 可写授权目录，默认 `/var/lib/appguard` |
| `APPGUARD_SECURE_COOKIE` | 部署运行时 | HTTPS 部署时设为 `1`，控制授权表单 Cookie |

示例 Dockerfile 通过 BuildKit secrets 临时挂载签名私钥、公钥和代码密钥，调用发行工具完成加密与编译。`appguard build-runtime` 会向编译过程传入前两个环境变量；客户不需要配置密钥环境变量，运行时也不从环境变量替换已编译的密钥。清单中的 `code_key_sha256` 是对解码后 32 字节代码密钥计算的匹配摘要，无需手工设置 Docker 构建摘要参数。

BuildKit secret 本身不影响缓存。**每次构建都必须保留 `--no-cache-filter protected-build`**，让源码加密和运行时编译使用当前挂载的密钥；普通发布和密钥轮换使用同一条构建命令。发行工具和系统依赖安装阶段仍可复用缓存。密钥值只能通过 secret 传递，不应成为 Docker 构建参数或日志内容。

## 续期、升级和密钥变更

| 场景 | 影响与处理 |
| --- | --- |
| 授权续期 | 使用原签名私钥、产品和客户信息重新签发，导入新许可证，不重新构建应用 |
| 普通应用升级 | 用原产品代码密钥重新构建镜像，继续使用原有效产品许可证；示例会重新加密模块并编译运行时 |
| 轮换产品代码密钥 | 重新加密全部模块并重编运行时；若发行方和产品不变，原有效许可证仍适用 |
| 更换签名私钥 | 重签代码清单、重编运行时并重新签发许可证；旧运行时不会自动信任新公钥 |
| 丢失产品代码密钥文件 | 已交付镜像仍可运行；后续普通构建需恢复备份，或轮换代码密钥并重建 |
| 丢失签名私钥 | 已有有效许可证继续有效；后续签发需恢复私钥，或按更换签名私钥处理 |
| 丢失客户许可证 | 恢复许可证备份或由发行方重新提供；没有需要恢复的部署私钥 |
| 密钥泄露 | 轮换密钥只影响后续交付，不能撤回已泄露密钥或已交付的离线旧版本 |

## 不是密钥的字段

| 字段 | 作用 |
| --- | --- |
| `nonce` | AES-GCM 随机数，与密文一同保存，不需要保密 |
| `signature` | Ed25519 签名，用于验证来源与完整性，可以公开 |
| `sha256`、`code_key_sha256` | 密文和代码密钥匹配摘要，不是解密密钥 |
| `product_id`、`build_id`、`license_id` | 产品、构建和许可证标识；知道标识不能签发许可证 |
| `last-seen` | 记录已观察到的时间，不是防篡改硬件时钟 |
| `appguard_csrf` Cookie 与表单 `csrf` | 授权导入表单的提交校验令牌，不参与代码加密或许可证签名 |

封装中的 `payload`、`signature` 使用 Base64 编码；交付时整个许可证再做一层 Base64 编码。网页直接支持这种交付格式，CLI 需先还原 JSON，见[交付与导入步骤](#base64-delivery)。Base64 不隐藏授权元数据；代码保护依赖业务模块的 AES-GCM 加密。

## 对应代码

| 环节 | 实现位置 |
| --- | --- |
| 生成密钥、构建入口、签发许可证 | [`appguard/__main__.py`](../appguard/__main__.py) |
| 签名和密钥文件处理 | [`appguard/crypto.py`](../appguard/crypto.py) |
| 整模块编译、加密、签署清单 | [`appguard/build.py`](../appguard/build.py) |
| 编译内置公钥及产品代码密钥 | [`appguard/_runtime/setup.py`](../appguard/_runtime/setup.py)、[示例 Dockerfile](../examples/flask/Dockerfile) |
| 清单验签、模块解密、许可证与时钟校验 | [`appguard/_runtime/guard_runtime.pyx`](../appguard/_runtime/guard_runtime.pyx) |
| 后端统一授权入口、授权页面和部署 CLI | [`appguard/_runtime/appguard_host.py`](../appguard/_runtime/appguard_host.py)、[`appguard/_runtime/appguard_flask.py`](../appguard/_runtime/appguard_flask.py) |
| 交付镜像全部层中的源码和已知秘密检查 | [`tools/audit_image.py`](../tools/audit_image.py) |
