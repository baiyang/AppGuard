# AppGuard

为 Python Web 项目提供代码加密交付和离线授权。发行方打包应用，部署方启动后申请许可证，导入许可证即可使用受保护功能。

当前支持 **CPython 3.11**，提供 **Flask 插件和 WSGI 中间件**。其他 WSGI 项目可以接入中间件；ASGI 项目需要另外适配。运行环境的系统、CPU 架构和 Python 版本必须与构建产物匹配。

## 从这里开始

- **第一次制作交付包**：按[首次发行指南](docs/first-release.md)运行内置 Flask 示例，完成打包、部署和授权。
- **已收到交付包**：按发行方提供的部署说明启动应用，再按下方步骤激活。[示例部署说明](examples/flask/DEPLOY.md)可随镜像一起交付。
- **接入自己的项目**：参考下方接入配置，再使用首次发行指南中的打包步骤。

## 首次部署与激活

流程：**发行方交付应用 → 部署方启动并下载授权申请 → 发行方签发许可证 → 部署方导入激活**。部署环境无需联网验证授权。

发行方需要提供：

| 文件 | 提供时间 | 用途 |
| --- | --- | --- |
| 应用镜像，如 `example-web-001.tar` | 首次部署前 | 包含应用、加密代码、匹配的 AppGuard 运行时和依赖 |
| 部署说明及必要的配置模板 | 首次部署前 | 明确启动命令、端口、数据卷，以及项目所需的数据库等配置 |
| 许可证，如 `customer-001.license` | 收到授权申请后 | 授权当前部署使用指定版本 |

### 1. 部署方启动应用

以下命令适用于本仓库示例的交付包；自己的项目使用发行方提供的镜像名和配置。

```sh
docker load -i example-web-001.tar
docker volume create example-web-license
docker run -d --name example-web -p 8000:8000 \
  --mount type=volume,source=example-web-license,target=/var/lib/appguard \
  example-web:001
```

浏览器打开 `http://localhost:8000/_license/`，点击“下载授权申请”，将 `activation-request.json` 发给发行方。远程部署时把 `localhost` 替换为服务器地址。

授权目录必须可写且持久保存，重建容器时继续挂载同一个数据卷。应用需要的数据库初始化等操作仍由项目自己的部署流程完成。

### 2. 发行方签发许可证

在 AppGuard 仓库根目录、已准备好的 Python 3.11 环境中运行。使用与交付镜像对应的发布记录，将 `--request` 换成收到的申请文件路径，并设置客户标识和到期时间：

```sh
python -m appguard issue \
  --release .data/releases/example-001/release.json \
  --issuer-key .data/issuer.key \
  --request activation-request.json \
  --customer customer-001 --expires 2027-12-31T23:59:59Z \
  --out .data/customer-001.license
```

把生成的 `customer-001.license` 交给部署方。签名私钥 `issuer.key` 和发布记录 `release.json` 由发行方备份留存，不放入交付包。

### 3. 部署方导入激活

在 `/_license/` 页面上传许可证并点击“激活授权”。状态变为“授权有效”后即可进入应用，无需重启。

续期时由发行方更新到期时间，并为 `--out` 指定新的文件名，重新签发后在同一页面导入。换用新构建的应用版本后，需要重新下载申请并签发对应许可证。

## 接入自己的项目

Flask 项目在创建和配置 `app` 后注册插件，继续使用原有启动命令：

```python
from appguard_flask import AppGuard

AppGuard().init_app(app)
```

其他 WSGI 项目包装其应用入口：

```python
from appguard_host import LicenseMiddleware

application = LicenseMiddleware(application)
```

在 `guard.toml` 中选择交付文件和需要授权的函数。路径相对于构建命令的 `--source`：

```toml
product_id = "my-web-app"
include = ["web_app.py", "service.py", "templates/", "static/"]
exclude = ["**/__pycache__/", "**/.env*", "**/*.pyc"]

[protected_functions]
"service.py" = ["answer"]
```

`include` 支持文件、目录和通配符；Python 文件加密处理，其他文件原样复制。至少选择一个受保护函数，并排除私密配置和开发文件。应用创建、导入、数据库初始化时必须调用的函数不要设为受保护函数，以便未激活时也能启动授权页面。闭包和异步生成器暂不支持作为受保护函数。

接入后的应用需要安装发行方为本次构建生成的 AppGuard 运行时；具体命令见[首次发行指南](docs/first-release.md)。代码加密不保证阻止拥有主机管理权限的人提取运行中的代码。

## 常用配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `APPGUARD_BUNDLE` | `/opt/appguard/bundle` | 加密代码及发布清单目录 |
| `APPGUARD_LICENSE_DIR` | `/var/lib/appguard` | 可写、持久化的授权目录 |
| `APPGUARD_SECURE_COOKIE` | `0` | 使用 HTTPS 时设为 `1` |

无法激活时，先查看 `/_license/` 的状态提示，确认许可证对应当前部署和版本、系统时间正确，以及授权目录可写。
