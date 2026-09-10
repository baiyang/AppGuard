# 应用部署说明

需要已安装 Docker 的 Linux amd64 环境，以及发行方提供的 `example-web-001.tar` 和 Base64 产品许可证，例如 `customer-001.b64.license`。示例依赖已包含在镜像内，部署、激活和运行无需访问发行方网络。

从示例源码构建镜像并试用，请先按[构建与试用说明](https://github.com/baiyang/AppGuard/blob/main/examples/flask/README.md)准备密钥并执行 Docker 构建。

## 首次启动

在交付文件所在目录执行：

```sh
docker load -i example-web-001.tar
docker volume create example-web-license
docker run -d --name example-web -p 8000:8000 \
  --mount type=volume,source=example-web-license,target=/var/lib/appguard \
  example-web:001
```

若 8000 端口被占用，可改为 `-p 8080:8000`，浏览器相应使用 8080 端口。

## 导入授权

启动容器后，等几秒钟，再在电脑上打开浏览器，例如 Chrome、Edge 或 Safari。根据 Docker 运行的位置，在浏览器顶部的地址栏输入对应地址并按回车：

| Docker 运行的位置 | 在浏览器中输入的地址 |
| --- | --- |
| 当前这台电脑 | `http://localhost:8000/_license/` |
| 另一台服务器 | `http://服务器的实际IP或域名:8000/_license/`，例如 `http://192.168.1.100:8000/_license/`，请替换为实际地址 |

`localhost` 始终指打开浏览器的电脑。远程服务器需允许访问映射的端口；若启动时使用 `-p 8080:8000`，以上浏览器地址中的端口也改为 8080。

1. 确认打开的是“产品授权”页面。首次部署应显示“授权状态：尚未激活”，下方有“更新授权”表单；这时即可导入许可证。
2. 在“许可证文件”处点击文件选择按钮，选中发行方提供的 `customer-001.b64.license`（以实际文件名为准），然后点击“激活授权”。“授权码”输入框可以留空；文件需位于当前使用浏览器的电脑上。收到的是授权码文本时，将完整单行内容粘贴到“授权码”输入框，无需选择文件，再点击“激活授权”。不要截取内容、插入换行或添加引号。
3. 确认页面显示“授权状态：授权有效”和到期时间。无需重启容器。
4. 在同一浏览器的地址栏中，将末尾的 `/_license/` 换为 `/api/answer?value=8` 并按回车。例如本机试用时输入 `http://localhost:8000/api/answer?value=8`，应看到 `{"answer":50}`。

Base64 是可逆的文本编码，不是加密。页面自动解码并验证许可证的签名、产品和有效期，无需手工处理文件；详细内容见[许可证格式](https://github.com/baiyang/AppGuard/blob/main/docs/keys.md#license-format)。

不需要下载授权申请或生成部署密钥。许可证对应产品 `example-web`，有效期内可用于该产品的后续版本。未导入、已到期或许可证无效时，所有业务路径都返回 HTTP 403 JSON，浏览器不会由后端自动跳转；授权页面始终可直接打开。带独立前端的实际产品由前端根据授权错误码处理跳转。

## 重启与续期

```sh
docker restart example-web
docker logs --tail 100 example-web
```

续期时联系发行方获取新的产品许可证，在 `/_license/` 导入，无需重启。普通镜像升级继续挂载原授权卷，原有效许可证可继续使用，不需要重新申请。

授权数据保存在 `example-web-license` 数据卷中，必须持续保留并备份；重建容器时挂载同一数据卷。卷中保存许可证和辅助时钟记录，没有部署私钥。若授权页无法打开，先检查容器日志、端口映射及服务器防火墙。

通过 HTTPS 访问时，在创建容器的命令中添加 `-e APPGUARD_SECURE_COOKIE=1`。反向代理应转发 `/_license/` 路径。

可以在容器中查看授权状态：

```sh
docker exec example-web python -m appguard_host status
```

授权只检查新进入的 HTTP 业务请求，不控制 CLI、后台任务或已经开始的流式响应。许可证不绑定机器；请按与发行方约定的部署范围使用。
