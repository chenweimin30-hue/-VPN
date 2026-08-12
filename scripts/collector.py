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
    except Exception:
        return []

def main():
    all_raw = set()
    for url in SOURCES:
        print(f"Fetching {url}")
        proxies = fetch_source(url)
        all_raw.update(proxies)
    print(f"Total raw proxies: {len(all_raw)}")

    good = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_proxy, p): p for p in all_raw}
        for future in as_completed(futures):
            result = future.result()
            if result:
                good.append(result)
                print(f"Good: {result}")

    with open(OUTPUT_FILE, 'w') as f:
        f.write('\n'.join(good))
    print(f"Done, saved {len(good)} proxies.")

    # ========== 自动生成 Clash 配置文件 ==========
    if not good:
        print("No good proxies, skipping Clash config generation.")
        return

    print("Generating clash_config.yaml...")
    clash_proxies = []
    for idx, proxy in enumerate(good, 1):
        server, port = proxy.split(':')
        clash_proxies.append(f"""  - name: "Proxy{idx}"
    type: http
    server: {server}
    port: {port}""")

    group_proxy_list = '\n'.join([f'      - "Proxy{idx}"' for idx in range(1, len(good)+1)])

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