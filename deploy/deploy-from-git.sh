#!/usr/bin/env bash
# 从 Gitea 远程仓库构建并部署物流小助手
# 用法(本机或任何能连 docker daemon 的机器):
#   docker -H tcp://10.100.0.176:2375 build "http://frank:VpsGitea%402026@127.0.0.1:13000/frank/logistics-track.git#master" -f deploy/Dockerfile -t logistics-track:latest
#   docker -H tcp://10.100.0.176:2375 rm -f logistics-track
#   docker -H tcp://10.100.0.176:2375 run -d --name logistics-track --restart unless-stopped \
#     --shm-size=1g --memory=1536m --memory-swap=2048m --env-file deploy/.env \
#     -v logistics-data:/app/data -v logistics-tmp:/app/tmp logistics-track:latest
#
# 要点:
#   - 构建上下文 = git 远程仓库(BuildKit 在云端 clone), 本机不传代码
#   - .env(凭据)不入库, 由 --env-file 在 run 时注入
#   - 发版流程: 本地 commit -> push 到 Gitea -> 执行上面两条命令
echo "请执行文档中的 build + run 两条命令"
