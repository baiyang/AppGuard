# 应用部署说明

需要已安装 Docker 的 Linux amd64 环境，以及发行方提供的 `example-web-001.tar` 和产品许可证。示例依赖已包含在镜像内，部署、激活和运行无需访问发行方网络。

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

1. 打开 `http://服务器地址:8000/_license/`，本机试用可使用 `localhost`。
2. 上传发行方提供的许可证文件，点击“激活授权”。
3. 确认显示“授权有效”，再打开 `/api/answer?value=8`，示例返回 `{"answer":50}`。

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

授权只检查新进入的 HTTP 业务请求，不控制 CLI、后台任务或已经开始的流式响应。许可证不绑定机器；请按与发行方约定的部署范围使用。旧版函数加密镜像迁移到本版本时，需要发行方重建镜像并提供一次新版产品许可证，不能直接沿用旧版绑定构建版本的许可证。
