# 版本与 PyPI 发布

公共包为 [`appguard-runtime`](https://pypi.org/project/appguard-runtime/)，源码仓库为
[`baiyang/AppGuard`](https://github.com/baiyang/AppGuard)。首个公共版本为 `0.0.1`，
仅支持 CPython 3.11。公共发行工具包不包含产品密钥或预编译的产品运行时；
`appguard build-runtime` 生成的 `appguard-product-runtime` wheel 仅用于对应产品的私有交付。

## 版本规则

- `pyproject.toml` 的 `project.version` 是公共版本的唯一来源，格式为 `MAJOR.MINOR.PATCH`。
- 每次发版更新版本及 `CHANGELOG.md`，通过 PR 合入 `main`，然后推送同名 `vX.Y.Z` 标签。
- 标签必须指向 `main` 上的提交，且与包版本完全一致。当前自动发布流程只接受正式版本。
- `0.x` 阶段，不兼容变更提升次版本，兼容修复提升补丁版本；进入 `1.0.0` 后按语义化版本管理。
- 仓库早期的 `0.3.0` 是内部版本，未发布到 PyPI；公共版本从 `0.0.1` 起算。
- 不移动已发布的标签，不用相同版本覆盖不同内容。发布错误通过新补丁版本修复。

## 首次配置

在正式 [PyPI Trusted Publishing 页面](https://pypi.org/manage/account/publishing/)添加
pending publisher。需登录有权持有项目的 PyPI 账号并完成账号要求的双因素验证。

| 字段 | 值 |
| --- | --- |
| PyPI Project Name | `appguard-runtime` |
| GitHub Owner | `baiyang` |
| GitHub Repository | `AppGuard` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

在 [GitHub Environments](https://github.com/baiyang/AppGuard/settings/environments)创建 `pypi` 环境，
将部署限制为 `v*` 标签。发行任务仅获得所需的短期 OIDC 发布凭据，无需保存长期 PyPI API token。
首次成功发布后，pending publisher 自动转为该项目的 trusted publisher。

## 发版流程

准备新版本、更新日志并合入 `main` 后：

```sh
git switch main
git pull --ff-only
git tag -a v0.0.2 -m "AppGuard v0.0.2"
git push origin v0.0.2
```

后续版本替换以上标签。GitHub 的 `Release` 工作流执行以下步骤：

1. 检查标签、项目版本、更新日志与 `main` 提交归属。
2. 在 Linux 和 macOS 上运行 Python 3.11 测试，包含真实原生运行时测试。
3. 构建当前版本的 wheel 和源码包，检查元数据，并在仓库外安装、编译、验证 Flask 和 FastAPI 加密应用。
4. 使用 Flask 示例固定的已发布版本 `0.0.1` 构建 Docker 镜像，审计各层，并验证授权与业务接口。当前待发布版本的运行时由前两步测试和包安装检查覆盖。
5. 核对 PyPI 已有同版本文件，使用 Trusted Publishing 上传唯一的公共 wheel 和源码包。
6. 对照 PyPI 官方接口核验 SHA256，再从正式索引全新安装并运行命令。
7. 创建 GitHub Release 并附上同一批发行文件。

构建和发布分属不同任务；只有发布任务具有 `id-token: write` 权限。
Actions 固定到提交 SHA，由 Dependabot 定期提出更新。

## 本地检查

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install '.[test]' build twine
python -m pytest -q
python tools/check_release.py --ref refs/tags/v0.0.2
python -m build
python -m twine check --strict dist/*
python tools/verify_package.py dist/*.whl dist/*.tar.gz
python examples/flask/scripts/verify_delivery.py --image appguard-ci:local --out .data/ci-delivery.json
```

Docker 检查需要已运行的 Docker 与 Buildx。发布后可用
`python tools/verify_pypi.py --wait 300 --install` 核对同一批本地发行文件与官方索引。

## 失败恢复

- 测试或构建失败时，不会进入发布阶段；修复后重新通过 CI 再发布。
- `invalid-publisher` 表示 PyPI 上述五项配置与 GitHub 身份不一致，修正后重跑失败任务。
- 若上传部分成功，可重跑失败任务。上传前会检查所有已有文件的 SHA256，只有相同内容才允许跳过。
- 发布后安装检查或 GitHub Release 创建失败时，重跑失败任务，保留原标签和原构建产物。
- 不对已上传版本重新构建并强行覆盖；如果内容发生变化，递增版本并重新发布。

配置依据：[PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
和 [PyPA GitHub Actions 发布指南](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)。
