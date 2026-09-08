# 首次发行指南

本文用仓库内的 Flask 示例完成第一次交付。发行方需要 Python 3.11、Docker 和支持命名构建上下文的 Docker Buildx。以下镜像命令以 `linux/amd64` 为例，其他架构需分别构建和验证。

## 1. 准备发行环境

```sh
git clone https://github.com/baiyang/AppGuard.git
cd AppGuard
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install cryptography==45.0.4
mkdir -p .data/releases .data/delivery
python -m appguard keygen --out .data/issuer.key
```

如果已经取得仓库，直接从创建虚拟环境开始。后续命令均在仓库根目录运行。签名密钥只生成一次，后续版本继续使用并妥善备份。

示例位于 `examples/flask/`，包含应用入口 `web_app.py`、受保护函数 `service.py:answer`、依赖清单和 `guard.toml`。授权前可以启动应用，授权后 `/api/answer?value=8` 返回 `{"answer":50}`。

## 2. 构建加密应用

```sh
python -m appguard build \
  --source examples/flask --config examples/flask/guard.toml \
  --issuer-key .data/issuer.key --out .data/releases/example-001
```

每次构建使用新的输出目录，如 `example-002`。发行方需要保存这些文件：

| 文件 | 用途 | 是否交付给部署方 |
| --- | --- | --- |
| `.data/issuer.key` | 签发许可证 | 否，私密备份 |
| `.data/issuer.pub` | 编译运行时 | 无需单独交付 |
| `.data/releases/example-001/release.json` | 为该版本签发、续期 | 否，私密备份 |
| `.data/releases/example-001/bundle/` | 制作应用镜像的输入 | 不直接交付整个目录 |

## 3. 制作交付镜像

```sh
docker buildx build --load --platform linux/amd64 \
  -f examples/flask/Dockerfile \
  --build-context release=.data/releases/example-001/bundle \
  --build-context application=examples/flask \
  --secret id=publisher_public,src=.data/issuer.pub \
  --secret id=bootstrap_key,src=.data/releases/example-001/bundle/bootstrap.key \
  -t example-web:001 .
```

Dockerfile 会编译与本次发布匹配的运行时，并把应用、依赖和加密文件装入最终镜像。编译使用的 `bootstrap.key` 不会复制到最终镜像；客户无需安装编译工具或拿到发行方密钥。

交付前检查镜像：

```sh
python tools/audit_image.py --image example-web:001 --out .data/image-audit.json
```

按[部署说明](../examples/flask/DEPLOY.md)在测试环境启动，再按 [README 的签发步骤](../README.md#2-发行方签发许可证)完成一次激活，确认 `/api/answer?value=8` 的结果。测试环境使用自己的授权数据卷；客户在其部署环境重新下载申请。

## 4. 导出并交付

```sh
docker image save -o .data/delivery/example-web-001.tar example-web:001
cp examples/flask/DEPLOY.md .data/delivery/DEPLOY.md
```

首次交付 `.data/delivery/` 中的镜像和部署说明。该示例不需要额外配置文件。实际项目还需提供必要的配置模板、初始化步骤和发行方联系方式；收到客户的授权申请后，再交付对应的许可证。

## 换成自己的项目

1. 按 [README](../README.md#接入自己的项目)接入插件或中间件，准备 `guard.toml`。
2. 将构建命令的 `--source` 和 `--config` 换成自己的路径。
3. 准备项目的 `requirements.txt`，包含 Web 服务进程所需依赖；将 `--build-context application` 指向该文件所在目录。
4. 复制并调整示例 Dockerfile 的启动命令、工作目录、端口和项目所需的系统依赖。例如包内入口可使用 `gunicorn myapp.web:app`。
5. 重新构建、验证和导出镜像，并把部署说明中的镜像名、端口、数据卷与配置改成项目实际值。

镜像检查默认检查 `/app` 下所有 Python 文件。如果应用目录包含无需加密的第三方 Python 文件，可用重复的 `--protected-path` 选项指定受保护文件或目录；使用自定义布局时通过 `--app-root` 和 `--bundle-root` 设置镜像内路径。

升级时一起交付新镜像和更新后的部署说明，保留授权数据卷，并按新版本重新申请许可证。
