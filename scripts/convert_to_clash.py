#!/usr/bin/env python3
import yaml

# 读取代理列表
with open('proxies.txt', 'r') as f:
    proxies = [line.strip() for line in f if line.strip()]

# 构建 Clash 配置
clash_config = {
    'version': 1,
    'mixed-port': 7890,
    'allow-lan': True,
    'bind-address': '*',
    'mode': 'rule',
    'log-level': 'info',
    'external-controller': '127.0.0.1:9090',
    'dns': {
        'enable': True,
        'default-nameserver': ['8.8.8.8', '1.1.1.1'],
        'nameserver': ['8.8.8.8', '1.1.1.1']
    },
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
            'interval': 300,
            'tolerance': 50
        }
    ],
    'rules': [
        'MATCH,Proxies'
    ]
}

# 添加代理到配置
proxy_count = 0
for i, proxy in enumerate(proxies):
    if proxy_count >= 50:  # 限制50个代理
        break
    
    try:
        parts = proxy.split(':')
        if len(parts) != 2:
            continue
        
        host, port_str = parts[0].strip(), parts[1].strip()
        
        # 验证 host 和 port
        if not host or not port_str:
            continue
        
        try:
            port = int(port_str)
            # 验证端口号范围
            if port <= 0 or port > 65535:
                continue
        except ValueError:
            continue
        
        # 验证 IP 格式（简单检查）
        ip_parts = host.split('.')
        if len(ip_parts) != 4:
            continue
        
        try:
            for part in ip_parts:
                num = int(part)
                if num < 0 or num > 255:
                    raise ValueError
        except (ValueError, TypeError):
            continue
        
        proxy_name = f"HTTP-{proxy_count + 1}"
        
        clash_config['proxies'].append({
            'name': proxy_name,
            'type': 'http',
            'server': host,
            'port': port
        })
        
        clash_config['proxy-groups'][0]['proxies'].append(proxy_name)
        clash_config['proxy-groups'][1]['proxies'].append(proxy_name)
        
        proxy_count += 1
        
    except Exception as e:
        print(f"⚠️  跳过无效代理 {proxy}: {e}")
        continue

# 保存为 YAML 文件
with open('clash_config.yaml', 'w', encoding='utf-8') as f:
    yaml.dump(clash_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

print(f"✅ Clash 配置已生成: clash_config.yaml")
print(f"📊 已添加 {proxy_count} 个有效代理")
print(f"\n📝 使用方法:")
print(f"   1. 在 Clash 中选择 'Profile' > 'Import from File'")
print(f"   2. 选择生成的 clash_config.yaml 文件")
print(f"   3. 在 Proxy Groups 中选择 'Proxies' 或 'Auto'")
print(f"\n💡 提示:")
print(f"   - Proxies: 手动选择代理")
print(f"   - Auto: 自动选择延迟最低的代理 (每 5 分钟检测一次)")
