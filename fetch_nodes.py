#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_nodes.py
从多个公开聚合仓库 + Telegram 免费节点频道自动抓取节点，解析、去重，
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
- 所有源都是公开 http(s) 订阅文件 / Telegram 公开频道，抓取频率建议不超过每小时一次。
"""

import argparse
import base64
import hashlib
import concurrent.futures
import json
import os
import re
import socket
import sys
import time
import urllib.request
import urllib.error
from html import unescape
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
    "https://ghfast.top/https://raw.githubusercontent.com/free18/v2ray/refs/heads/main/v.txt",
    "https://cdn.jsdelivr.net/gh/0xRadikal/Free-v2ray-Configs@main/all/configs.txt",
    "https://raw.githubusercontent.com/Surfboardv2ray/TGParse/main/splitted/mixed",
]

# ---------------------------------------------------------------------------
# Telegram 免费节点频道源。
# 频道清单放在同目录的 telegramchannels.json（JSON 数组，或每行一个用户名）。
# 抓取方式：访问频道的公开预览页 https://t.me/s/<频道>（无需登录 / Bot Token），
# 从页面里提取代理链接。
# 解析协议：vmess / vless / trojan / ss / hysteria2（含 hy2:// 前缀）。
# 失效频道自动清理：抓到页面但连续多次没有任何节点链接的频道，会被记进
# invalidtelegramchannels.json，下次开始自动跳过（避免每次都在死频道上浪费时间）。
# ---------------------------------------------------------------------------
TELEGRAM_CHANNELS_FILE = "telegramchannels.json"
INVALID_TG_FILE = "invalidtelegramchannels.json"
TG_PAGE_URL = "https://t.me/s/{}"
TG_PROTO_PREFIXES = ("vmess://", "vless://", "trojan://", "ss://", "hysteria2://", "hy2://")
TG_INVALID_THRESHOLD = 2  # 连续几次抓到空内容就判定为失效频道并自动跳过

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


# ---------------------------------------------------------------------------
# Telegram 频道抓取
# ---------------------------------------------------------------------------

def load_telegram_channels(path: str) -> list:
    """读取频道清单：支持 JSON 数组、{"channels": [...]} 或"每行一个用户名"的文本。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        print(f"! Telegram 频道清单读取失败: {path} ({e})", file=sys.stderr)
        return []
    names = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            names = data
        elif isinstance(data, dict):
            for key in ("channels", "names", "telegram"):
                if isinstance(data.get(key), list):
                    names = data[key]
                    break
    except Exception:
        # 不是 JSON：按"每行一个用户名"处理，支持 # 注释和空行
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    out = []
    for n in names:
        s = str(n).strip().lstrip("@")
        if s:
            out.append(s)
    return list(dict.fromkeys(out))  # 去重、保序


def load_invalid_channels(path: str) -> dict:
    """读取失效频道记录：{频道名: 连续空内容次数}。
    兼容 {…}（次数）和 […]（数组里的都视为已失效，次数按阈值算）两种格式。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
        if isinstance(data, list):
            return {str(c).strip().lstrip("@"): TG_INVALID_THRESHOLD for c in data if str(c).strip()}
    except Exception as e:
        print(f"! 失效频道记录读取失败: {path} ({e})", file=sys.stderr)
    return {}


def save_invalid_channels(path: str, invalid: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(invalid, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"! 失效频道记录保存失败: {path} ({e})", file=sys.stderr)


def random_sleep() -> float:
    """随机短等待（0.5~2s），降低对 t.me 的访问压力。"""
    return 0.5 + ((time.time() * 1000) % 1500) / 1000.0


def fetch_tg_channel(channel: str, timeout: int = 20, retries: int = 1) -> str:
    """抓取一个 Telegram 频道的公开预览页 HTML（无需登录/Token），失败重试一次。"""
    url = TG_PAGE_URL.format(channel)
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(random_sleep())
            else:
                print(f"  ! 频道抓取失败: {channel} ({e})", file=sys.stderr)
    return ""


def clean_uri(uri: str) -> str:
    """清理从网页里切出来的链接：HTML 反转义、去掉空白和尾部截断符号。"""
    uri = unescape(uri)
    uri = "".join(uri.split())  # 去掉所有空白（代理链接里不会有真实空格）
    while uri and uri[-1] in ("…", "»", "%", "`", "\\"):
        uri = uri[:-1]
    uri = re.sub(r"^amp;", "", uri)
    return uri


def extract_links_from_html(html: str) -> list:
    """从 t.me/s/<频道> 页面 HTML 里按协议前缀抽取代理链接。"""
    pat = re.compile(r"((?:vmess|vless|trojan|ss|hysteria2|hy2)://[^\s<\"'<>]+)", re.IGNORECASE)
    links = []
    for m in pat.finditer(html):
        uri = clean_uri(m.group(1))
        if uri:
            links.append(uri)
    return links


def fetch_tg_channel_links(channel: str):
    """抓取一个频道：返回其中的代理链接列表；
    抓取失败（网络问题，HTML 为空）返回 None，不参与失效频道判定。"""
    html = fetch_tg_channel(channel)
    if not html:
        return None
    return extract_links_from_html(html)


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
        if not node["server"] or not node["port"] or not node["cipher"] or not node["password"]:
            return None
        return node
    except Exception:
        return None


def parse_hysteria2(uri: str):
    """hysteria2://password@host:port/?sni=xxx&insecure=1&obfs=...&obfs-password=...&up=...&down=...#名字
    同时兼容 hy2:// 前缀（同一协议）。"""
    try:
        u = urlparse(uri)
        qs = parse_qs(u.query)
        name = unquote(u.fragment) or f"hy2-{u.hostname}"
        node = {
            "name": name,
            "type": "hysteria2",
            "server": u.hostname,
            "port": u.port,
            "password": u.username or "",
            "skip-cert-verify": True,
        }
        if qs.get("sni"):
            node["sni"] = qs["sni"][0]
        if qs.get("obfs"):
            node["obfs"] = qs["obfs"][0]
        if qs.get("obfs-password"):
            node["obfs-password"] = qs["obfs-password"][0]
        if qs.get("up"):
            node["up"] = qs["up"][0]
        if qs.get("down"):
            node["down"] = qs["down"][0]
        if not node["server"] or not node["port"] or not node["password"]:
            return None
        return node
    except Exception:
        return None


PARSERS = {
    "vmess://": parse_vmess,
    "vless://": parse_vless,
    "trojan://": parse_trojan,
    "ss://": parse_ss,
    "hysteria2://": parse_hysteria2,
    "hy2://": parse_hysteria2,
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
    """节点唯一键：类型+地址+端口+凭据短哈希。
    加上凭据哈希可避免「同一 host:port 换了 uuid/密码」被判为重复而丢弃新凭据，
    也避免多人共用端口被错误折叠。"""
    cred = n.get("uuid") or n.get("password") or ""
    cred_h = hashlib.md5(cred.encode("utf-8")).hexdigest()[:8] if cred else "nocred"
    return f"{n['type']}|{n['server']}|{n['port']}|{cred_h}"


def filter_seen(nodes: list, seen: dict) -> list:
    """去掉历史记录里还没过期的节点，只保留没见过（或者已经过期忘记）的节点"""
    return [n for n in nodes if node_key(n) not in seen]


# ---------------------------------------------------------------------------
# 节点稳定度统计（node_stats.json）
# 思路：免费源里的节点「连续多轮都还在列表里」的，实际存活率远高于只闪现一次
# 就消失的（前者多半是正经公益节点，后者常是临时扫描出来的）。我们不去测速，
# 而是统计每个节点「出现过多少轮」「最近一次出现是哪天」「连续多少轮没出现」，
# 按出现轮数降序排序，取 top N 进客户端的 url-test 测速组——客户端只需测很少
# 几个节点，几秒完成，且这少数几个大概率是活的。
# ---------------------------------------------------------------------------

STATS_FILE = "node_stats.json"
DEAD_THRESHOLD = 3  # 连续多少轮没出现就从稳定度统计里剔除（约 18 小时，按 6h 一轮）


def load_stats(path: str) -> dict:
    """读取 {key: {first, last, rounds, miss}}。key 同 node_key。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"! 节点统计读取失败，当作空处理: {path} ({e})", file=sys.stderr)
        return {}


def save_stats(path: str, stats: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"! 节点统计保存失败: {path} ({e})", file=sys.stderr)


def update_stats(stats: dict, current_keys: list, today: str, dead_threshold: int = DEAD_THRESHOLD) -> dict:
    """根据本轮出现的节点更新稳定度统计，返回更新后的 stats。
    - 本轮出现的：rounds += 1，last = 今天，miss 清零；
    - 本轮没出现的：miss += 1，达到 dead_threshold 直接从统计里删除。"""
    cur = set(current_keys)
    for k in cur:
        rec = stats.get(k)
        if rec is None:
            stats[k] = {"first": today, "last": today, "rounds": 1, "miss": 0}
        else:
            rec["rounds"] = rec.get("rounds", 0) + 1
            rec["last"] = today
            rec["miss"] = 0
    to_drop = []
    for k, rec in stats.items():
        if k not in cur:
            rec["miss"] = rec.get("miss", 0) + 1
            if rec["miss"] >= dead_threshold:
                to_drop.append(k)
    for k in to_drop:
        del stats[k]
    return stats


# ---------------------------------------------------------------------------
# 地区识别：按节点名里的关键字自动分出地区子组，方便手动挑选
# ---------------------------------------------------------------------------

REGION_KEYWORDS = {
    "香港": ["香港", "hk", "hongkong", "hong kong"],
    "台湾": ["台湾", "tw", "taiwan"],
    "日本": ["日本", "jp", "japan", "tokyo", "osaka", "大阪", "东京"],
    "新加坡": ["新加坡", "sg", "singapore"],
    "美国": ["美国", "us", "usa", "united states", "los angeles", "la", "sf", "ny", "纽约", "洛杉矶", "硅谷", "silicon"],
    "韩国": ["韩国", "kr", "korea", "seoul", "首尔"],
    "欧洲": ["欧洲", "eu", "germany", "france", "uk", "nl", "de", "fr", "英国", "德国", "法国", "荷兰"],
}


def region_of(name: str) -> str:
    low = (name or "").lower()
    for region, kws in REGION_KEYWORDS.items():
        for kw in kws:
            if kw in low:
                return region
    return "其它"


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
    s = str(s)
    # YAML 不允许大部分控制字符，即使包在引号里也不行；节点名字/备注这些字段来自
    # 第三方数据源，偶尔会混进乱七八糟的字节，这里统一清掉，避免生成非法 yaml
    s = "".join(ch for ch in s if ch == "\t" or ord(ch) >= 0x20)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_proxy(n: dict) -> str:
    parts = [f"{k}: {yaml_str(v) if isinstance(v, str) else v}" for k, v in n.items()
              if k not in ("ws-opts", "grpc-opts", "_raw")]
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


# ---------------------------------------------------------------------------
# YAML 字符串清洗：去掉单字节控制字符与 U+FFFD，避免 Mihomo 严格校验失败。
# 原因：部分 ss/vless 节点的密码字段是从 base64 解码出的非 UTF-8 二进制，
# 上游 parser 用 errors="ignore" 保留了下来，写进 YAML 后 Mihomo 报
# "control characters are not allowed" 并拒收整份配置。
# ---------------------------------------------------------------------------
_FORBIDDEN_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u0080-\u009f\ufffd]')

def sanitize_text(s):
    if not isinstance(s, str):
        return s
    return _FORBIDDEN_CHARS.sub('', s)


# 关键凭据字段：清洗后为空意味着密码丢失，连不上，留着也是死节点
_SENSITIVE_FIELDS = {"cipher", "password", "uuid", "id", "alterId"}

# SS2022 系列加密的密钥长度要求（字节）。密钥必须是 base64 且解码后长度正确。
# 免费源里大量"2022-blake3 加密 + 普通文本密码"的假节点，Mihomo 加载时
# 会报 decode key: illegal base64 data 并拒绝整份配置，必须在生成阶段剔除。
_SS2022_KEY_LEN = {
    "2022-blake3-aes-128-gcm": 16,
    "2022-blake3-aes-256-gcm": 32,
    "2022-blake3-chacha20-poly1305": 32,
}


def _valid_ss2022_part(part: str, want_len: int) -> bool:
    """校验单个 SS2022 密钥段：补 padding 后必须是合法 base64，且解码长度正确。
    同时兼容 base64url 字符（- 和 _），按标准 base64 转换后再试一次。"""
    if not part:
        return False
    for candidate in (part, part.replace("-", "+").replace("_", "/")):
        try:
            pad = candidate + "=" * (-len(candidate) % 4)
            key = base64.b64decode(pad, validate=True)
            if len(key) == want_len:
                return True
        except Exception:
            continue
    return False


def valid_ss2022_key(cipher: str, password: str) -> bool:
    """SS2022 (2022-blake3-*) 密钥校验，密钥非法返回 False（该节点应丢弃）。
    多用户格式 serverkey:userkey（SIP022）按冒号分段校验。"""
    want_len = _SS2022_KEY_LEN.get((cipher or "").strip().lower())
    if want_len is None:
        return True  # 不是 SS2022 系列，不在此校验
    for part in (password or "").split(":"):
        if not _valid_ss2022_part(part, want_len):
            return False
    return True


def sanitize_nodes(nodes):
    """清洗所有字符串字段；关键凭据被清空、或 SS2022 密钥非法（Mihomo 会拒收
    整份配置）的节点直接丢弃。返回 (清洗后节点, 被丢弃数)。"""
    out = []
    dropped = 0
    for n in nodes:
        ok = True
        for k, v in list(n.items()):
            if isinstance(v, str):
                cleaned = sanitize_text(v)
                n[k] = cleaned
                if k in _SENSITIVE_FIELDS and not cleaned.strip():
                    ok = False
                    break
        if ok and n.get("type") == "ss" and not valid_ss2022_key(n.get("cipher", ""), n.get("password", "")):
            ok = False
        if ok:
            out.append(n)
        else:
            dropped += 1
    return out, dropped


def build_yaml(pool, auto_select, out_path: str, interval: int = 600, tolerance: int = 100,
               batch_size: int = 100, batch_count: int = 0):
    """生成 Clash Meta YAML（分层结构）。

    pool:        全量节点（进「全部节点」select 组，不做健康检查，零测速开销）。
    auto_select: 进测速的节点。batch_count>0 时切成多个 url-test 批次组，
                 每批独立测速出冠军，再由「自动选择」汇总组比较各组冠军取最快——
                 分摊并发、避免单组几千节点互相争抢超时误判。
    batch_size / batch_count: 二选一。batch_size>0 按每批 N 个自动切；
                 batch_count>0 强制切成 N 批；都为 0 不分批（自动选择直连全部节点）。

    region_of 依赖节点名里的地区关键字，第三方源命名不规范时部分节点会落进
    「其它」组，不影响使用，只是少一个快捷分类。
    """
    lines = []
    lines.append("# 自动生成 - fetch_nodes.py")
    lines.append("mixed-port: 7890")
    lines.append("allow-lan: false")
    lines.append("mode: rule")
    lines.append("log-level: info")
    lines.append("external-controller: 127.0.0.1:9090")
    lines.append("")
    lines.append("proxies:")
    for n in pool:
        lines.append(dump_proxy(n))
    lines.append("")
    lines.append("proxy-groups:")

    # --- 分批测速组：每批一个 url-test，各自测出最快的节点 ---
    batch_groups = []
    if batch_size > 0 and batch_count == 0 and auto_select:
        batch_count = (len(auto_select) + batch_size - 1) // batch_size
    if batch_count > 0 and auto_select:
        per = (len(auto_select) + batch_count - 1) // batch_count
        for i in range(batch_count):
            chunk = auto_select[i * per:(i + 1) * per]
            if not chunk:
                break
            gname = f"测速组-{i + 1:02d}"
            batch_groups.append(gname)
            lines.append(f"  - name: {gname}")
            lines.append("    type: url-test")
            lines.append('    url: "http://www.gstatic.com/generate_204"')
            lines.append(f"    interval: {interval}")
            lines.append(f"    tolerance: {tolerance}")
            lines.append("    lazy: true")
            lines.append("    proxies:")
            for n in chunk:
                lines.append(f"      - {yaml_str(n['name'])}")

    # --- 自动选择：分批时引用各批次组（比较各组冠军），否则直连全部节点 ---
    lines.append("  - name: 自动选择")
    lines.append("    type: url-test")
    lines.append('    url: "http://www.gstatic.com/generate_204"')
    lines.append(f"    interval: {interval}")
    lines.append(f"    tolerance: {tolerance}")
    lines.append("    lazy: true")
    lines.append("    proxies:")
    if batch_groups:
        for g in batch_groups:
            lines.append(f"      - {yaml_str(g)}")
    else:
        for n in auto_select:
            lines.append(f"      - {yaml_str(n['name'])}")
    # 全部节点：select，放全量（不做健康检查），首项指向自动选择
    lines.append("  - name: 全部节点")
    lines.append("    type: select")
    lines.append("    proxies:")
    lines.append("      - 自动选择")
    for n in pool:
        lines.append(f"      - {yaml_str(n['name'])}")
    # 地区子组：方便手动挑选
    regions = {}
    for n in pool:
        r = region_of(n["name"])
        regions.setdefault(r, []).append(n["name"])
    for r in sorted(regions.keys()):
        lines.append(f"  - name: 地区-{r}")
        lines.append("    type: select")
        lines.append("    proxies:")
        for nm in regions[r]:
            lines.append(f"      - {yaml_str(nm)}")
    lines.append("")
    lines.append("rules:")
    lines.append("  - MATCH,全部节点")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build_v2ray_sub(nodes, out_path: str):
    """生成 v2rayN / v2rayNG / NekoBox 等通用的订阅格式：
    原始 vmess://、vless://、trojan://、ss:// 链接拼一起，整体 base64 编码。

    hysteria2 节点一律过滤掉：v2rayN 的 Xray 核心不支持 hy2 协议，
    导入后只会显示成连不上的死节点（实测印度 fastervpn 节点即此问题），
    Clash 配置里已包含 hy2，需要 hy2 请用 Clash/Mihomo 客户端订阅。"""
    raws = [n["_raw"] for n in nodes
            if n.get("_raw") and n.get("type") not in ("hysteria2", "tuic")]
    dropped = sum(1 for n in nodes if n.get("type") in ("hysteria2", "tuic"))
    if dropped:
        print(f"v2ray 订阅：过滤掉 {dropped} 个 hysteria2/tuic 节点（Xray 核心不支持，避免死节点）")
    blob = "\n".join(raws).encode("utf-8")
    b64 = base64.b64encode(blob).decode("ascii")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(b64)
    return len(raws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="clash_config_new.yaml", help="输出文件名")
    ap.add_argument("--limit", type=int, default=0,
                     help="最多保留多少个节点，默认 0 表示不限制、全部抓进来。"
                          "客户端节点太多可能加载慢，想控制数量的话自己传个数字，比如 --limit 300")
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
    ap.add_argument("--tg-channels-file", default=TELEGRAM_CHANNELS_FILE,
                     help="Telegram 频道清单文件路径（默认 telegramchannels.json）")
    ap.add_argument("--tg-concurrency", type=int, default=12,
                     help="抓取 Telegram 频道的并发数，默认 12")
    ap.add_argument("--tg-invalid-file", default=INVALID_TG_FILE,
                     help="失效频道记录文件路径（默认 invalidtelegramchannels.json）")
    ap.add_argument("--tg-invalid-threshold", type=int, default=TG_INVALID_THRESHOLD,
                     help="连续几次抓到空内容就判定为失效频道（默认 2）")
    ap.add_argument("--reset-tg-invalid", action="store_true",
                     help="清空失效频道记录，重新开始判定")
    ap.add_argument("--no-telegram", action="store_true",
                     help="跳过 Telegram 频道源（只用 SOURCES 里的订阅地址）")
    ap.add_argument("--snapshot", action="store_true",
                     help="输出全部当前节点（不做跨天去重过滤），适合云端定时任务；"
                          "配合 node_stats.json 稳定度打分，发布的就是「当前全部可用节点」而非增量")
    ap.add_argument("--auto-select-size", type=int, default=300,
                     help="放进「自动选择」url-test 测速组的节点数，默认 300；"
                          "传 0 表示全部节点都进测速组（客户端会把几千个全测一遍，很吃资源且易触发上游限流，慎用）。"
                          "注意：无论这个值是多少，全部节点都会写进配置，可在「全部节点」组手动挑选")
    ap.add_argument("--pool-cap", type=int, default=0,
                     help="全量节点池上限，默认 0 表示不限制（保留全部去重后的节点）；"
                          "想控制节点数量就传个数字，比如 --pool-cap 500")
    ap.add_argument("--interval", type=int, default=600,
                     help="「自动选择」组的测速间隔（秒），默认 600；想更快感知节点变慢就调小，比如 300")
    ap.add_argument("--tolerance", type=int, default=100,
                     help="「自动选择」组的延迟容差（毫秒），默认 100："
                          "只有当更快的节点比当前节点快超过这个值才切换，调大可避免频繁跳节点")
    ap.add_argument("--test-batch-size", type=int, default=100,
                     help="分批测速组：每批放多少个节点（默认 100）。"
                          "把进测速的节点切成多个独立 url-test 组，各批测出冠军后再由"
                          "「自动选择」汇总组比较各组冠军取最快，分摊并发避免单组过载")
    ap.add_argument("--test-batch-count", type=int, default=0,
                     help="分批测速组：强制切成多少批（默认 0=按 batch-size 自动算）。"
                          "比如 3000 节点设 30 批 = 每批 100 个")
    ap.add_argument("--dead-threshold", type=int, default=DEAD_THRESHOLD,
                     help="节点连续多少轮未出现就从稳定度统计里剔除（默认 3）")
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

    # Telegram 频道源（带失效频道自动清理）
    channels = [] if args.no_telegram else load_telegram_channels(args.tg_channels_file)
    if channels:
        invalid = {} if args.reset_tg_invalid else load_invalid_channels(args.tg_invalid_file)
        active = [c for c in channels if invalid.get(c, 0) < args.tg_invalid_threshold]
        skipped = len(channels) - len(active)
        if skipped:
            print(f"\nTelegram 频道源: 共 {len(channels)} 个，已自动跳过 {skipped} 个连续失效频道"
                  f"（{args.tg_invalid_file} 里记录）")
        else:
            print(f"\nTelegram 频道源: {len(channels)} 个频道（并发 {args.tg_concurrency}）")
        tg_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.tg_concurrency) as ex:
            futures = {ex.submit(fetch_tg_channel_links, c): c for c in active}
            for fut in concurrent.futures.as_completed(futures):
                c = futures[fut]
                links = fut.result()
                if links is None:
                    continue  # 抓取失败（网络问题），不参与失效判定
                if not links:
                    invalid[c] = invalid.get(c, 0) + 1  # 页面能开但没节点，失效计数 +1
                    continue
                invalid.pop(c, None)  # 有节点内容，恢复正常
                nodes = parse_all("\n".join(links))
                print(f"  {c}: {len(nodes)} 个节点")
                all_nodes.extend(nodes)
                tg_count += len(nodes)
        print(f"Telegram 频道合计解析节点: {tg_count}，当前失效频道 {len(invalid)} 个")
        if not args.no_history:
            save_invalid_channels(args.tg_invalid_file, invalid)

    print(f"\n合计原始节点: {len(all_nodes)}")
    nodes = dedupe(all_nodes)
    print(f"去重后: {len(nodes)}")

    history_path = args.history_file
    seen = {}
    if args.reset_history and os.path.exists(history_path):
        os.remove(history_path)
        print(f"已清空历史记录: {history_path}")

    # --- 稳定度统计：用「去重后全部节点」更新，无论后面是否过滤，统计都反映本轮全貌 ---
    stats = {} if args.no_history else load_stats(STATS_FILE)
    today = time.strftime("%Y-%m-%d")
    current_keys = [node_key(n) for n in nodes]
    if not args.no_history:
        stats = update_stats(stats, current_keys, today, args.dead_threshold)

    # --- 快照 vs 增量 ---
    # 快照（--snapshot，云端定时任务用）：输出全部当前节点，配合上面的稳定度打分，
    # 发布的就是「当前全部可用节点」，客户端拿到的是完整池子，不再出现「增量越更新越少」的问题。
    # 增量（默认 / 本地一次性）：按历史记录过滤，只输出没见过的「新节点」。
    if args.snapshot:
        print("快照模式：输出全部当前节点（不做跨天去重过滤）")
    elif not args.no_history:
        seen = load_history(history_path, args.history_days)
        before = len(nodes)
        nodes = filter_seen(nodes, seen)
        print(f"跨天去重（历史记录 {len(seen)} 条）: 剔除 {before - len(nodes)} 个已出现过的，剩 {len(nodes)} 个新节点")

    nodes = rename_duplicates(nodes)

    if args.test:
        nodes = test_nodes(nodes, args.test_concurrency, args.test_timeout)

    # --- 清洗 + 去重 + 限池 ---
    # 清洗：去掉控制字符与 U+FFFD，避免 Mihomo 校验失败拒绝整份配置
    nodes, dropped_dirty = sanitize_nodes(nodes)
    if dropped_dirty:
        print(f"清洗：丢弃 {dropped_dirty} 个节点（凭据为空 / SS2022 密钥非法的假节点——"
              "非法 SS2022 密钥会导致 Mihomo 拒收整份配置）")
    # 排序：已停用「稳定度评分」排序（老节点 rounds 越滚越高会永久霸榜，新加的源永远挤不进来）。
    # 默认只做确定性排序——按节点 key 排，保证每次输出的顺序稳定，
    # 避免节点顺序无意义抖动导致 git diff 巨大。不参与任何节点的取舍。
    # 例外：开了 --test 时 nodes 已按「实测延迟」升序排好，这里不能覆盖，
    # 否则测速白做，--pool-cap 也就截不到「最快的那批」了。
    if not args.test:
        nodes.sort(key=lambda n: node_key(n))

    # 节点池上限（默认 400，0 表示不限）；--limit 作为旧参数别名
    pool_cap = args.limit if args.limit else args.pool_cap
    if pool_cap and len(nodes) > pool_cap:
        nodes = nodes[:pool_cap]
        print(f"节点池上限 {pool_cap}，截取靠前的 {pool_cap} 个")

    pool = nodes
    # 0 或负数 = 全部节点都进「自动选择」url-test 组，由客户端测速后挑最快的
    if not pool:
        auto_select = []
    elif args.auto_select_size <= 0:
        auto_select = pool
    else:
        auto_select = pool[: args.auto_select_size]

    if not pool:
        if not all_nodes:
            print("没有抓到任何节点（源可能都访问失败了），未生成文件。", file=sys.stderr)
            sys.exit(1)
        if args.snapshot:
            print("本次没有抓到任何节点，未生成文件。", file=sys.stderr)
            sys.exit(1)
        print("本次抓到的节点都在历史记录里出现过，没有新节点，跳过本次生成（旧配置文件保持不变）。")
        print("想强制看到全部节点的话，加 --reset-history 或者 --no-history。")
        sys.exit(0)

    # 分批测速：节点量大时不分批会互相争抢导致大量超时误判。
    # 默认 --test-batch-size 100：auto_select 300 个 → 3 批，每批独立 url-test，
    # 「自动选择」汇总组只比较 3 个批次冠军，测速又快又稳。
    batch_size = args.test_batch_size
    batch_count = args.test_batch_count
    # auto_select 少于一批的量就没必要分批，直接单组
    if auto_select and batch_size > 0 and len(auto_select) <= batch_size and batch_count == 0:
        batch_size = 0

    build_yaml(pool, auto_select, args.out,
               interval=args.interval, tolerance=args.tolerance,
               batch_size=batch_size, batch_count=batch_count)
    n_batches = 0
    if batch_size > 0 or batch_count > 0:
        n_batches = max(1, (len(auto_select) + (batch_size or 1) - 1) // (batch_size or 1)) if batch_count == 0 else batch_count
    print(f"\n✅ 已生成: {args.out}（全量 {len(pool)} 个节点，{len(auto_select)} 个进测速"
          + (f"，分成 {n_batches} 个批次组并行测速" if n_batches else "") + "）")
    print("导入 Clash Meta 后：日常用「自动选择」策略组（汇总各批次冠军取最快）；"
          "想手动挑就用「全部节点」或「地区-xxx」子组。")

    v2ray_out = args.out_v2ray
    if not v2ray_out:
        base, _ = os.path.splitext(args.out)
        v2ray_out = f"{base}_v2ray.txt"
    n_written = build_v2ray_sub(pool, v2ray_out)
    print(f"✅ 已生成: {v2ray_out}（{n_written} 个节点，v2rayN/v2rayNG/NekoBox 通用订阅格式）")

    if not args.no_history:
        save_stats(STATS_FILE, stats)
        print(f"已更新节点稳定度统计: {STATS_FILE}（累计 {len(stats)} 条，连续 {args.dead_threshold} 轮未出现自动剔除）")
        # 跨天历史（增量模式用；快照模式不写，避免 seen 无限膨胀）
        if not args.snapshot:
            seen.update({node_key(n): today for n in pool})
            save_history(history_path, seen)
            print(f"已更新历史记录: {history_path}（累计 {len(seen)} 条，{args.history_days} 天后自动过期）")


if __name__ == "__main__":
    main()
