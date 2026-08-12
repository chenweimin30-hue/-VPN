#!/usr/bin/env python3
import yaml

# 读取代理列表
with open('proxies.txt', 'r') as f:
    proxies = [line.strip() for line in f if line.strip()]

# 构建 Clash 配置
clash_config = {
    'mixed-port': 7890,
    'allow-lan': True,
    'bind-address': '*',
    'mode': 'rule',
    'log-level': 'info',
    'external-controller': '127.0.0.1:9090',
    'proxies': [],
    'proxy-groups': [
        {
            'name': 'Proxies',
            'type': 'select',
            'proxies': []
        },
        {
            'name': 'Auto',
            'type': 'url-test',
            'proxies': [],
            'url': 'http://www.gstatic.com/generate_204',
            'interval': 300
        }
    ],
    'rules': [
        'MATCH,Proxies'
    ]
}

# 添加代理到配置
for i, proxy in enumerate(proxies[:50]):  # 只用前50个代理以避免配置过大
    host, port = proxy.split(':')
    proxy_name = f"HTTP-{i+1}"
    
    clash_config['proxies'].append({
        'name': proxy_name,
        'type': 'http',
        'server': host,
        'port': int(port)
    })
    
    clash_config['proxy-groups'][0]['proxies'].append(proxy_name)
    clash_config['proxy-groups'][1]['proxies'].append(proxy_name)

# 保存为 YAML 文件
with open('clash_config.yaml', 'w', encoding='utf-8') as f:
    yaml.dump(clash_config, f, default_flow_style=False, allow_unicode=True)

print(f"✅ Clash 配置已生成: clash_config.yaml")
print(f"📊 已添加 {len(clash_config['proxies'])} 个代理")
print(f"📝 使用方法:")
print(f"   1. 在 Clash 中选择 'Profile' > 'File'")
print(f"   2. 选择生成的 clash_config.yaml 文件")
print(f"   3. 在 Proxy Groups 中选择代理")
