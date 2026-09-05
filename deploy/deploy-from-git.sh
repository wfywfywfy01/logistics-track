#!/usr/bin/env bash
# 从 GitHub 远程仓库构建并部署物流小助手
# 仓库: https://github.com/wfywfywfy01/logistics-track (公有)
#
# 发版流程:
#   1. 本地: git add -A && git commit -m "..." && git push
#   2. 构建(云端 BuildKit clone; 用完整 commit SHA, ghfast 代理会缓存旧 commit, 直连 GitHub 更稳):
#      docker -H tcp://10.100.0.176:2375 build "https://github.com/wfywfywfy01/logistics-track.git#$(git rev-parse HEAD)" -f deploy/Dockerfile -t logistics-track:latest
#      备选(ghfast, 可能拿到旧代码): docker -H ... build "https://ghfast.top/https://github.com/wfywfywfy01/logistics-track.git#master" -f deploy/Dockerfile -t logistics-track:latest
#   3. 部署(先 tag prev 留回滚点):
#      docker -H tcp://10.100.0.176:2375 tag logistics-track:latest logistics-track:prev
#      docker -H tcp://10.100.0.176:2375 rm -f logistics-track
#      docker -H tcp://10.100.0.176:2375 run -d --name logistics-track --restart unless-stopped \
#        --shm-size=1g --memory=1536m --memory-swap=2048m --env-file deploy/.env \
#        --log-opt max-size=20m --log-opt max-file=3 \
#        -v logistics-data:/app/data -v logistics-tmp:/app/tmp logistics-track:latest
#   回滚: rm -f 后用 logistics-track:prev 跑同一条 run
#
# 要点:
#   - 仓库公有; vertu-cli 自带鉴权(APP_KEY), 暴露无敏感信息
#   - .env(APP_KEY/OCR 密钥/xray 密码)不入库, --env-file 在 run 时注入
echo "见文档: 3 步发版(commit/push -> build from git -> run)"
