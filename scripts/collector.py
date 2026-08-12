#!/usr/bin/env python3
import requests
import time
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

SOURCES = [
    'https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt',
    'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/HTTP.txt',
]

OUTPUT_FILE = 'proxies.txt'
TIMEOUT = 5
MAX_WORKERS = 30
MAX_LATENCY = 3000
MAX_PROXIES_TO_TEST = 800  # 单次最多测试的代理数量，防止源过大导致跑太久
GLOBAL_TIME_BUDGET = 240   # 整个测试阶段最多跑 240 秒（4分钟），超时直接收工

def test_proxy(proxy):
    try:
        if '://' in proxy:
            proxy = proxy.split('://')[1]
        parts = proxy.split(':')
        if len(parts) != 2:
            return None
        host, port = parts[0], int(parts[1])
        start = time.time()
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        sock.close()
        latency = (time.time() - start) * 1000
        if latency <= MAX_LATENCY:
            return f"{host}:{port}"
    except Exception:
        pass
    return None

def fetch_source(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.splitlines()
        proxies = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#'):
                proxies.append(line)
        return proxies
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return []

def main():
    all_raw = set()
    for url in SOURCES:
        print(f"Fetching {url}")
        proxies = fetch_source(url)
        all_raw.update(proxies)
    print(f"Total raw proxies: {len(all_raw)}")

    # 限制测试数量，避免代理源过大导致整体运行时间失控
    all_raw = list(all_raw)
    if len(all_raw) > MAX_PROXIES_TO_TEST:
        print(f"Too many proxies ({len(all_raw)}), sampling {MAX_PROXIES_TO_TEST}")
        all_raw = all_raw[:MAX_PROXIES_TO_TEST]

    good = []
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_proxy, p): p for p in all_raw}
        for future in as_completed(futures):
            # 全局时间预算保护：超时直接停止等待剩余任务，拿现有结果收工
            if time.time() - start_time > GLOBAL_TIME_BUDGET:
                print(f"Time budget ({GLOBAL_TIME_BUDGET}s) exceeded, stopping early with {len(good)} good proxies so far.")
                break
            result = future.result()
            if result:
                good.append(result)
                print(f"Good: {result}")

    with open(OUTPUT_FILE, 'w') as f:
        f.write('\n'.join(good))
    print(f"Done, saved {len(good)} good proxies.")

    # ========== 自动生成 Clash 配置文件 ==========
    if not good:
        print("No good proxies, skipping Clash config generation.")
        return

    print("Generating clash_config.yaml...")
    clash_proxies = []
    for idx, proxy in enumerate(good, 1):
        server, port = proxy.split(':')
        clash_proxies.append(
f"""  - name: "Proxy{idx}"
    type: http
    server: {server}
    port: {port}""")

    group_proxy_list = '\n'.join([
        f'      - "Proxy{idx}"' for idx in range(1, len(good)+1)])

    clash_content = f"""mixed-port: 7890
allow-lan: false
mode: rule
log-level: info

proxies:
{chr(10).join(clash_proxies)}

proxy-groups:
  - name: "PROXY"
    type: select
    proxies:
      - DIRECT
{group_proxy_list}

rules:
  - 'MATCH,PROXY'
"""
    with open('clash_config.yaml', 'w') as f:
        f.write(clash_content)
    print(f"clash_config.yaml generated with {len(good)} proxies!")

if __name__ == '__main__':
    main()