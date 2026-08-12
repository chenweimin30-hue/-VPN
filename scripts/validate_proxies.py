#!/usr/bin/env python3
import requests
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

TIMEOUT = 5
MAX_WORKERS = 30
TEST_URL = 'http://www.gstatic.com/generate_204'

def test_proxy_connectivity(proxy):
    """测试代理连接性和响应时间"""
    try:
        parts = proxy.replace('http://', '').replace('https://', '').split(':')
        if len(parts) != 2:
            return None, None, "Invalid format"
        
        host, port = parts[0], parts[1]
        
        # 测试 TCP 连接
        start = time.time()
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        sock.close()
        latency = (time.time() - start) * 1000
        
        # 测试 HTTP 代理
        proxy_url = f"http://{host}:{port}"
        try:
            response = requests.get(
                TEST_URL,
                proxies={'http': proxy_url, 'https': proxy_url},
                timeout=TIMEOUT
            )
            if response.status_code == 204:
                return latency, "active", None
            else:
                return latency, "dead", f"Status: {response.status_code}"
        except requests.exceptions.RequestException as e:
            return latency, "dead", str(e)
            
    except socket.timeout:
        return None, "timeout", "Socket timeout"
    except socket.error as e:
        return None, "unreachable", str(e)
    except Exception as e:
        return None, "error", str(e)

def main():
    print("🔍 开始检测代理节点...")
    print(f"📍 超时时间: {TIMEOUT}s")
    print(f"👷 并发线程: {MAX_WORKERS}\n")
    
    # 读取原始代理列表
    with open('proxies.txt', 'r') as f:
        raw_proxies = [line.strip() for line in f if line.strip()]
    
    print(f"📊 原始节点数: {len(raw_proxies)}\n")
    
    # 测试所有代理
    valid_proxies = []
    invalid_proxies = []
    duplicate_proxies = defaultdict(int)
    stats = {
        'active': 0,
        'timeout': 0,
        'unreachable': 0,
        'dead': 0,
        'error': 0,
        'duplicate': 0
    }
    
    # 检查重复代理
    seen = set()
    unique_proxies = []
    for proxy in raw_proxies:
        if proxy not in seen:
            seen.add(proxy)
            unique_proxies.append(proxy)
        else:
            stats['duplicate'] += 1
            duplicate_proxies[proxy] += 1
    
    if stats['duplicate'] > 0:
        print(f"⚠️  检测到 {stats['duplicate']} 个重复节点，已去重\n")
    
    # 并发测试代理
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_proxy_connectivity, p): p for p in unique_proxies}
        completed = 0
        
        for future in as_completed(futures):
            completed += 1
            proxy = futures[future]
            latency, status, error = future.result()
            
            # 显示进度
            if completed % 10 == 0:
                print(f"⏳ 检测进度: {completed}/{len(unique_proxies)}")
            
            if status == "active":
                valid_proxies.append(proxy)
                stats['active'] += 1
                print(f"✅ {proxy} - {latency:.0f}ms")
            elif status == "timeout":
                stats['timeout'] += 1
                invalid_proxies.append((proxy, "Timeout"))
            elif status == "unreachable":
                stats['unreachable'] += 1
                invalid_proxies.append((proxy, "Unreachable"))
            elif status == "dead":
                stats['dead'] += 1
                invalid_proxies.append((proxy, error))
            else:
                stats['error'] += 1
                invalid_proxies.append((proxy, error))
    
    # 排序有效代理（按延迟从小到大）
    print("\n" + "="*60)
    print("📊 检测结果统计")
    print("="*60)
    print(f"✅ 有效节点: {stats['active']}")
    print(f"❌ 无效节点:")
    print(f"   - Timeout: {stats['timeout']}")
    print(f"   - Unreachable: {stats['unreachable']}")
    print(f"   - Dead: {stats['dead']}")
    print(f"   - Error: {stats['error']}")
    print(f"🔁 重复节点: {stats['duplicate']}")
    print(f"\n原始: {len(raw_proxies)} → 去重: {len(unique_proxies)} → 有效: {stats['active']}")
    print("="*60 + "\n")
    
    # 保存有效代理
    with open('proxies.txt', 'w') as f:
        for proxy in valid_proxies:
            f.write(proxy + '\n')
    
    # 保存无效代理日志
    with open('invalid_proxies.log', 'w') as f:
        f.write("无效代理列表\n")
        f.write("="*60 + "\n")
        f.write(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"无效总数: {len(invalid_proxies)}\n")
        f.write("="*60 + "\n\n")
        
        for proxy, reason in invalid_proxies:
            f.write(f"❌ {proxy} - {reason}\n")
    
    # 保存重复代理日志
    if duplicate_proxies:
        with open('duplicate_proxies.log', 'w') as f:
            f.write("重复代理列表\n")
            f.write("="*60 + "\n")
            f.write(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"重复总数: {stats['duplicate']}\n")
            f.write("="*60 + "\n\n")
            
            for proxy, count in duplicate_proxies.items():
                f.write(f"🔁 {proxy} (出现 {count+1} 次)\n")
    
    print(f"✅ 有效代理已保存到: proxies.txt ({stats['active']} 个)")
    print(f"📋 无效代理日志: invalid_proxies.log")
    if duplicate_proxies:
        print(f"📋 重复代理日志: duplicate_proxies.log")
    print(f"\n💡 建议: 定期运行此脚本以保持代理列表的有效性")

if __name__ == '__main__':
    main()
