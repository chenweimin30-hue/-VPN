#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_nodes.py
从多个公开聚合仓库自动抓取免费节点，解析、去重，
输出 Clash Meta 可直接使用的 YAML 配置文件。

用法:
    python3 fetch_nodes.py --out clash_config_003.yaml
    python3 fetch_nodes.py --out clash_config_003.yaml --test   # 额外做连通性测速

说明:
- 默认只做"抓取 + 解析 + 生成配置"，不做逐个节点的 socket 存活探测；
  真正的连通性测试交给 Clash Meta 客户端自带的 url-test 策略组去做。
- 加 --test 参数可以在生成配置前先做一轮 TCP 连通性测速，过滤掉连不上的
  节点、按延迟排序。这一步只应该在自己的电脑/自己的服务器上跑——
  如果检测到运行在 GitHub Actions 里会自动跳过，不会重蹈之前账号因为
  在共享 CI 基础设施上批量探测第三方主机而被限制的问题。
- 所有源都是公开 http(s) 订阅文件，抓取频率建议不超过每小时一次。
"""

import argparse
import base64
import concurrent.futures
import json
import os
import re
import socket
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------------------
# 节点来源列表（可自行增减）。都是公开的免费节点聚合仓库。
# ---------------------------------------------------------------------------
SOURCES = [
    "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/all_sub.txt",
    "https://raw.githubusercontent.com/barry-far/V2ray-config/main/All_Configs_Sub.txt",
    "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/vmess.txt",
    "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/vless.txt",
    "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt",
    "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/ss.txt",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) clash-config-builder/1.0"


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  ! 抓取失败: {url} ({e})", file=sys.stderr)
        return ""
    # 尝试整体 base64 解码（部分仓库把整份订阅编码成一坨 base64）
    text = raw.decode("utf-8", errors="ignore")
    stripped = "".join(l for l in text.splitlines() if l and not l.startswith("#"))
    if stripped and re.fullmatch(r"[A-Za-z0-9+/=\s]+", stripped):
        try:
            pad = stripped + "=" * (-len(stripped) % 4)
            decoded = base64.b64decode(pad).decode("utf-8", errors="ignore")
            if "://" in decoded:
                return decoded
        except Exception:
            pass
    return text


def b64pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def safe_b64decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(b64pad(s))


# ---------------------------------------------------------------------------
# 各协议解析函数：URI -> Clash Meta proxy dict（None 表示解析失败/跳过）
# ---------------------------------------------------------------------------

def parse_vmess(uri: str):
    try:
        payload = uri[len("vmess://"):]
        data = json.loads(safe_b64decode(payload).decode("utf-8", errors="ignore"))
        node = {
            "name": data.get("ps") or f"vmess-{data.get('add')}",
            "type": "vmess",
            "server": data.get("add"),
            "port": int(data.get("port", 0)),
            "uuid": data.get("id"),
            "alterId": int(data.get("aid", 0) or 0),
            "cipher": data.get("scy") or "auto",
            "udp": True,
        }
        net = data.get("net", "tcp")
        node["network"] = net
        if data.get("tls") == "tls":
            node["tls"] = True
            if data.get("sni"):
                node["servername"] = data["sni"]
            node["skip-cert-verify"] = True
        if net == "ws":
            node["ws-opts"] = {
                "path": data.get("path") or "/",
                "headers": {"Host": data.get("host")} if data.get("host") else {},
            }
        elif net == "grpc":
            node["grpc-opts"] = {"grpc-service-name": data.get("path") or ""}
        if not node["server"] or not node["port"] or not node["uuid"]:
            return None
        return node
    except Exception:
        return None


def parse_vless(uri: str):
    try:
        u = urlparse(uri)
        qs = parse_qs(u.query)
        name = unquote(u.fragment) or f"vless-{u.hostname}"
        node = {
            "name": name,
            "type": "vless",
            "server": u.hostname,
            "port": u.port,
            "uuid": u.username,
            "udp": True,
            "network": qs.get("type", ["tcp"])[0],
        }
        if qs.get("security", [""])[0] == "tls":
            node["tls"] = True
            if qs.get("sni"):
                node["servername"] = qs["sni"][0]
            node["skip-cert-verify"] = True
        if qs.get("flow"):
            node["flow"] = qs["flow"][0]
        if node["network"] == "ws":
            node["ws-opts"] = {
                "path": unquote(qs.get("path", ["/"])[0]),
                "headers": {"Host": qs["host"][0]} if qs.get("host") else {},
            }
        elif node["network"] == "grpc":
            node["grpc-opts"] = {"grpc-service-name": qs.get("serviceName", [""])[0]}
        if not node["server"] or not node["port"] or not node["uuid"]:
            return None
        return node
    except Exception:
        return None


def parse_trojan(uri: str):
    try:
        u = urlparse(uri)
        qs = parse_qs(u.query)
        name = unquote(u.fragment) or f"trojan-{u.hostname}"
        node = {
            "name": name,
            "type": "trojan",
            "server": u.hostname,
            "port": u.port,
            "password": u.username,
            "udp": True,
            "skip-cert-verify": True,
        }
        if qs.get("sni"):
            node["sni"] = qs["sni"][0]
        if qs.get("type", ["tcp"])[0] == "ws":
            node["network"] = "ws"
            node["ws-opts"] = {"path": unquote(qs.get("path", ["/"])[0])}
        if not node["server"] or not node["port"] or not node["password"]:
            return None
        return node
    except Exception:
        return None


def parse_ss(uri: str):
    try:
        body = uri[len("ss://"):]
        name = ""
        if "#" in body:
            body, frag = body.split("#", 1)
            name = unquote(frag)
        if "@" in body:
            # SIP002: base64(method:password)@host:port  或明文 method:password@host:port
            userinfo, hostport = body.rsplit("@", 1)
            try:
                userinfo = safe_b64decode(userinfo).decode("utf-8")
            except Exception:
                pass
            method, password = userinfo.split(":", 1)
            host, port = hostport.rsplit(":", 1)
        else:
            decoded = safe_b64decode(body).decode("utf-8", errors="ignore")
            methodpass, hostport = decoded.rsplit("@", 1)
            method, password = methodpass.split(":", 1)
            host, port = hostport.rsplit(":", 1)
        node = {
            "name": name or f"ss-{host}",
            "type": "ss",
            "server": host,
            "port": int(port.split("/")[0].split("?")[0]),
            "cipher": method,
            "password": password,
            "udp": True,
        }
        return node
    except Exception:
        return None


PARSERS = {
    "vmess://": parse_vmess,
    "vless://": parse_vless,
    "trojan://": parse_trojan,
    "ss://": parse_ss,
}


def parse_all(raw_text: str):
    nodes = []
    for line in raw_text.splitlines():
        line = line.strip()
        for prefix, fn in PARSERS.items():
            if line.startswith(prefix):
                node = fn(line)
                if node:
                    node["_raw"] = line  # 保留原始链接，供生成 v2ray 通用订阅用
                    nodes.append(node)
                break
    return nodes


def dedupe(nodes):
    seen = set()
    out = []
    for n in nodes:
        key = (n["type"], n["server"], n["port"])
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def rename_duplicates(nodes):
    """避免节点名重复导致 Clash 加载报错"""
    counts = {}
    for n in nodes:
        base = n["name"].strip() or n["server"]
        counts[base] = counts.get(base, 0) + 1
        if counts[base] > 1:
            n["name"] = f"{base} #{counts[base]}"
        else:
            n["name"] = base
    return nodes


# ---------------------------------------------------------------------------
# 历史记录：跨天去重，避免每天都重复导入之前出现过的节点
# ---------------------------------------------------------------------------

def load_history(path: str, max_age_days: float) -> dict:
    """返回 {key: 记录日期字符串}，并顺手把超过 max_age_days 的旧记录过期掉
    （否则历史记录只增不减，用不了多久就会把节点池"耗尽"，见 README 说明）"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("seen", {})
        # 兼容旧版本（list 格式，没有日期）：当天记录处理
        if isinstance(raw, list):
            today = time.strftime("%Y-%m-%d")
            raw = {k: today for k in raw}
        cutoff = time.time() - max_age_days * 86400
        fresh = {}
        for k, date_str in raw.items():
            try:
                ts = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            except Exception:
                ts = time.time()
            if ts >= cutoff:
                fresh[k] = date_str
        expired = len(raw) - len(fresh)
        if expired:
            print(f"历史记录中有 {expired} 条超过 {max_age_days} 天，已过期清除（这些节点如果还活着，之后可能重新出现）")
        return fresh
    except Exception:
        print(f"! 历史记录文件读取失败，当作空历史处理: {path}", file=sys.stderr)
        return {}


def save_history(path: str, seen: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen": seen, "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                   f, ensure_ascii=False, indent=2)


def node_key(n: dict) -> str:
    return f"{n['type']}|{n['server']}|{n['port']}"


def filter_seen(nodes: list, seen: dict) -> list:
    """去掉历史记录里还没过期的节点，只保留没见过（或者已经过期忘记）的节点"""
    return [n for n in nodes if node_key(n) not in seen]


# ---------------------------------------------------------------------------
# 可选：TCP 连通性测速（仅限本地/自己的机器运行，见 --test 说明）
# ---------------------------------------------------------------------------

def tcp_probe(server: str, port: int, timeout: float):
    """单个节点做一次 TCP 三次握手测试，返回延迟（毫秒）或 None（不通）"""
    start = time.time()
    try:
        with socket.create_connection((server, port), timeout=timeout):
            return round((time.time() - start) * 1000)
    except Exception:
        return None


def test_nodes(nodes, concurrency: int, timeout: float):
    """
    并发做 TCP 连通性测试，过滤掉连不上的节点，并按延迟从低到高排序。

    注意：这一步只应该在你自己的电脑/自己的服务器上跑，不要放进
    GitHub Actions 等共享 CI 环境——之前账号被限制就是因为在共享 CI
    基础设施上对大批量第三方主机发起批量 socket 连接，这类行为容易被
    平台判定为"用共享 IP 做网络扫描"，跟测试本身是否合理无关，
    是平台对其自身基础设施使用方式的限制。在自己的网络环境下做同样
    的事情，是你自己的出口流量，不涉及这个问题。
    """
    print(f"开始测速：{len(nodes)} 个节点，并发 {concurrency}，超时 {timeout}s ...")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {
            ex.submit(tcp_probe, n["server"], n["port"], timeout): n for n in nodes
        }
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            n = futures[fut]
            latency = fut.result()
            done += 1
            if done % 50 == 0:
                print(f"  已测 {done}/{len(nodes)} ...")
            if latency is not None:
                n["_latency_ms"] = latency
                results.append(n)
    results.sort(key=lambda n: n["_latency_ms"])
    print(f"测速完成：{len(results)}/{len(nodes)} 个节点可连通")
    for n in results:
        n.pop("_latency_ms", None)
    return results


# ---------------------------------------------------------------------------
# 生成 Clash Meta YAML
# ---------------------------------------------------------------------------

def yaml_str(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_proxy(n: dict) -> str:
    parts = [f"{k}: {yaml_str(v) if isinstance(v, str) else v}" for k, v in n.items()
              if k not in ("ws-opts", "grpc-opts")]
    line = "  - {" + ", ".join(parts)
    if "ws-opts" in n:
        wo = n["ws-opts"]
        wo_parts = [f'path: {yaml_str(wo.get("path", "/"))}']
        if wo.get("headers"):
            h = ", ".join(f"{yaml_str(k)}: {yaml_str(v)}" for k, v in wo["headers"].items())
            wo_parts.append("headers: {" + h + "}")
        line += ", ws-opts: {" + ", ".join(wo_parts) + "}"
    if "grpc-opts" in n:
        line += f', grpc-opts: {{grpc-service-name: {yaml_str(n["grpc-opts"].get("grpc-service-name",""))}}}'
    line += "}"
    return line


def build_yaml(nodes, out_path: str):
    names = [n["name"] for n in nodes]
    lines = []
    lines.append("# 自动生成 - fetch_nodes.py")
    lines.append("mixed-port: 7890")
    lines.append("allow-lan: false")
    lines.append("mode: rule")
    lines.append("log-level: info")
    lines.append("external-controller: 127.0.0.1:9090")
    lines.append("")
    lines.append("proxies:")
    for n in nodes:
        lines.append(dump_proxy(n))
    lines.append("")
    lines.append("proxy-groups:")
    lines.append("  - name: 自动选择")
    lines.append("    type: url-test")
    lines.append('    url: "http://www.gstatic.com/generate_204"')
    lines.append("    interval: 300")
    lines.append("    tolerance: 50")
    lines.append("    proxies:")
    for name in names:
        lines.append(f"      - {yaml_str(name)}")
    lines.append("  - name: 手动选择")
    lines.append("    type: select")
    lines.append("    proxies:")
    lines.append("      - 自动选择")
    for name in names:
        lines.append(f"      - {yaml_str(name)}")
    lines.append("")
    lines.append("rules:")
    lines.append("  - MATCH,手动选择")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build_v2ray_sub(nodes, out_path: str):
    """生成 v2rayN / v2rayNG / NekoBox 等通用的订阅格式：
    原始 vmess://、vless://、trojan://、ss:// 链接拼一起，整体 base64 编码。"""
    raws = [n["_raw"] for n in nodes if n.get("_raw")]
    blob = "\n".join(raws).encode("utf-8")
    b64 = base64.b64encode(blob).decode("ascii")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(b64)
    return len(raws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="clash_config_new.yaml", help="输出文件名")
    ap.add_argument("--limit", type=int, default=300, help="最多保留多少个节点（太多客户端加载会卡）")
    ap.add_argument("--test", action="store_true",
                     help="对节点做 TCP 连通性测速，过滤掉连不上的、按延迟排序（只建议在本地/自己的机器上用，见 README）")
    ap.add_argument("--test-concurrency", type=int, default=20, help="测速并发数，默认 20")
    ap.add_argument("--test-timeout", type=float, default=3.0, help="单个节点测速超时（秒），默认 3")
    ap.add_argument("--history-file", default="seen_nodes.json",
                     help="历史记录文件路径，用于跨天去重（默认 seen_nodes.json）")
    ap.add_argument("--no-history", action="store_true",
                     help="不做跨天去重，本次也不写入历史记录（临时看看全量用这个）")
    ap.add_argument("--history-days", type=float, default=7,
                     help="历史记录保留几天后自动过期（默认 7 天），过期的节点如果还活着会重新出现")
    ap.add_argument("--reset-history", action="store_true",
                     help='清空历史记录后重新开始记（相当于把之前"见过"的节点全部忘掉）')
    ap.add_argument("--out-v2ray", default=None,
                     help="额外生成一份 v2rayN/v2rayNG/NekoBox 通用订阅文件（base64 节点链接）。"
                          "不指定的话，默认根据 --out 自动生成同名 _v2ray.txt 文件")
    args = ap.parse_args()

    if args.test and os.environ.get("GITHUB_ACTIONS") == "true":
        print(
            "! 检测到当前运行在 GitHub Actions 里，已自动跳过 --test。\n"
            "  批量连通性测试请在自己的电脑或自己的服务器上跑，不要放进共享 CI 环境。",
            file=sys.stderr,
        )
        args.test = False

    all_nodes = []
    for url in SOURCES:
        print(f"抓取: {url}")
        text = fetch(url)
        if not text:
            continue
        nodes = parse_all(text)
        print(f"  解析到 {len(nodes)} 个节点")
        all_nodes.extend(nodes)

    print(f"\n合计原始节点: {len(all_nodes)}")
    nodes = dedupe(all_nodes)
    print(f"去重后: {len(nodes)}")

    history_path = args.history_file
    seen = {}
    if args.reset_history and os.path.exists(history_path):
        os.remove(history_path)
        print(f"已清空历史记录: {history_path}")

    if not args.no_history:
        seen = load_history(history_path, args.history_days)
        before = len(nodes)
        nodes = filter_seen(nodes, seen)
        print(f"跨天去重（历史记录 {len(seen)} 条）: 剔除 {before - len(nodes)} 个已出现过的，剩 {len(nodes)} 个新节点")

    nodes = rename_duplicates(nodes)

    if args.test:
        nodes = test_nodes(nodes, args.test_concurrency, args.test_timeout)

    if len(nodes) > args.limit:
        nodes = nodes[: args.limit]
        print(f"截取前 {args.limit} 个写入配置")

    if not nodes:
        if not all_nodes:
            print("没有抓到任何节点（源可能都访问失败了），未生成文件。", file=sys.stderr)
            sys.exit(1)
        print("本次抓到的节点都在历史记录里出现过，没有新节点，跳过本次生成（旧配置文件保持不变）。")
        print("想强制看到全部节点的话，加 --reset-history 或者 --no-history。")
        sys.exit(0)

    build_yaml(nodes, args.out)
    print(f"\n✅ 已生成: {args.out}（{len(nodes)} 个节点，Clash Meta 格式）")
    print("导入 Clash Meta 后，用「自动选择」策略组即可，客户端会自动测速切换最快节点。")

    v2ray_out = args.out_v2ray
    if not v2ray_out:
        base, _ = os.path.splitext(args.out)
        v2ray_out = f"{base}_v2ray.txt"
    n_written = build_v2ray_sub(nodes, v2ray_out)
    print(f"✅ 已生成: {v2ray_out}（{n_written} 个节点，v2rayN/v2rayNG/NekoBox 通用订阅格式）")

    if not args.no_history:
        today = time.strftime("%Y-%m-%d")
        seen.update({node_key(n): today for n in nodes})
        save_history(history_path, seen)
        print(f"已更新历史记录: {history_path}（累计 {len(seen)} 条，{args.history_days} 天后自动过期）")


if __name__ == "__main__":
    main()
