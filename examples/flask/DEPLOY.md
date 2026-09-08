# 应用部署说明

需要已安装 Docker 的 Linux amd64 环境，以及发行方提供的 `example-web-001.tar`。该示例的依赖已包含在镜像内，部署和激活无需访问外部网络。

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

## 申请与激活

1. 打开 `http://服务器地址:8000/_license/`，点击“下载授权申请”。本机试用可使用 `localhost`。
2. 将下载的 `activation-request.json` 发给发行方。
3. 收到许可证后，在同一页面上传文件并点击“激活授权”。
4. 确认显示“授权有效”，再打开 `/api/answer?value=8`，示例返回 `{"answer":50}`。

许可证由发行方收到本次部署的申请后提供。不要使用其他部署或版本的许可证。

## 重启与续期

```sh
docker restart example-web
docker logs --tail 100 example-web
```

续期时把当前授权申请发给发行方，收到新许可证后在 `/_license/` 导入，无需重启。升级镜像后需重新申请对应版本的许可证。

授权数据保存在 `example-web-license` 数据卷中，必须持续保留并备份；重建容器时挂载同一数据卷。若授权页无法打开，先检查容器日志、端口映射及服务器防火墙。

通过 HTTPS 访问时，在创建容器的命令中添加 `-e APPGUARD_SECURE_COOKIE=1`。反向代理应转发 `/_license/` 路径。
