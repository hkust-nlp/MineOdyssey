# 网络与 ARNOLD 环境

---

## IPv6 网络

### Q: 为什么所有 Java 进程都要配 IPv6？

ARNOLD 集群的机器通常只有 IPv6 出口，没有 IPv4。Java 默认走 IPv4 会报 `java.net.SocketException: Network is unreachable`。

相关脚本（`bootstrap-runtime-assets.sh`、`run-openha-eval-core-inside.sh`、`run-server-only-inside.sh`、`start-single-bot-inside.sh`）开头都有自动检测：

```bash
if ! curl -4 -s --connect-timeout 3 https://maven.neoforged.net -o /dev/null 2>/dev/null; then
    export JAVA_TOOL_OPTIONS="... -Djava.net.preferIPv4Stack=false -Djava.net.preferIPv6Addresses=true"
fi
```

不需要手动配置，脚本会自动处理。

---

## 跨机器联机

### Q: 集群内其他机器能加入我的 MC 服务端吗？

可以。已验证 ARNOLD 集群内 IPv6 跨机器联机正常。

**步骤**：

1. 启动 server（端口自动分配，如 20008）
2. 确认本机 IPv6：`hostname -I` 或 `bash get_merlin_config.sh`
3. 其他机器连入：
   ```bash
   SERVER_HOST="[目标IPv6地址]" SERVER_PORT=20008 \
     bash scripts/launch/start-single-bot-inside.sh Bot2
   ```

### Q: 怎么验证端口可达？

```bash
# 在远程机器上
curl -6 --connect-timeout 5 [目标IPv6]:端口
# 能收到数据（哪怕乱码）就说明端口可达
```

### Q: 需要正版验证吗？

不需要。服务端默认 `online-mode=false`（离线模式），`server-entrypoint.sh` 自动设置。

### Q: 自动分配的端口（20000+）外部能访问吗？

可以。自动分配的端口和 ARNOLD 分配的端口一样，对集群内其他机器都可达。

---

## get_merlin_config.sh

### Q: 这个脚本干什么的？

检测当前 ARNOLD 环境的可用端口、GPU、IPv4/IPv6 地址：

```bash
bash get_merlin_config.sh
# AVAILABLE_PORTS=10684,9386,...
# AVAILABLE_GPUS=[-1]
# IPV4ADDRESS=127.0.0.1
# IPV6ADDRESS=fdbd:dc61:20:199:2e28:c019:d200:4
```

### Q: IPv6 地址为什么不用 ARNOLD 环境变量里的？

ARNOLD 环境变量（如 `ARNOLD_RESERVED_0_CURRENT_HOST`）报告的 IPv6 地址和实际网卡地址可能不同。脚本优先取 `hostname -I` 返回的地址，这是实际可达的 eth0 地址。
