#!/usr/bin/env python3
import json
import base64

# 读取代理列表
with open('proxies.txt', 'r') as f:
    proxies = [line.strip() for line in f if line.strip()]

# 构建 V2Ray 配置
v2ray_config = {
    "log": {
        "loglevel": "info"
    },
    "inbounds": [
        {
            "port": 10808,
            "protocol": "http",
            "settings": {}
        },
        {
            "port": 10809,
            "protocol": "socks",
            "settings": {
                "auth": "noauth"
            }
        }
    ],
    "outbounds": [],
    "routing": {
        "rules": [
            {
                "type": "field",
                "ip": ["geoip:private"],
                "outboundTag": "direct"
            }
        ],
        "balancers": [
            {
                "tag": "balancer",
                "selector": []
            }
        ]
    }
}

# 添加代理到 outbounds
for i, proxy in enumerate(proxies[:50]):  # 只用前50个代理
    host, port = proxy.split(':')
    outbound_name = f"proxy-{i+1}"
    
    v2ray_config['outbounds'].append({
        "protocol": "http",
        "settings": {
            "servers": [
                {
                    "address": host,
                    "port": int(port)
                }
            ]
        },
        "tag": outbound_name,
        "streamSettings": {
            "network": "tcp"
        }
    })
    
    v2ray_config['routing']['balancers'][0]['selector'].append(outbound_name)

# 添加直连和默认出站
v2ray_config['outbounds'].append({
    "protocol": "freedom",
    "tag": "direct"
})

v2ray_config['outbounds'].append({
    "protocol": "blackhole",
    "tag": "blocked"
})

# 保存为 JSON 文件
with open('v2ray_config.json', 'w', encoding='utf-8') as f:
    json.dump(v2ray_config, f, indent=2, ensure_ascii=False)

# 生成 V2RayN 订阅链接格式
print("📋 生成 V2RayN 订阅链接...")

v2rayn_links = []
for i, proxy in enumerate(proxies[:50]):
    host, port = proxy.split(':')
    
    # V2RayN 使用 vmess:// 或 http:// 格式，这里用简单的 HTTP 代理格式
    # 格式: http://ip:port
    link = f"http://{host}:{port}"
    v2rayn_links.append(link)

# 保存为订阅链接文件
with open('v2rayn_links.txt', 'w', encoding='utf-8') as f:
    for link in v2rayn_links:
        f.write(link + '\n')

print(f"✅ V2Ray 配置已生成: v2ray_config.json")
print(f"✅ V2RayN 订阅链接已生成: v2rayn_links.txt")
print(f"📊 已添加 {len(v2ray_config['outbounds']) - 2} 个代理")
print(f"\n📝 使用方法:")
print(f"   1. V2Ray 使用 v2ray_config.json 配置文件")
print(f"   2. V2RayN 可导入 v2rayn_links.txt 中的链接")
print(f"   3. 或在 V2RayN 中: 订阅设置 > 新增 > 输入 v2rayn_links.txt 的内容")
