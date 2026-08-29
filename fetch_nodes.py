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


def build_yaml(pool, auto_select, out_path: str, interval: int = 600, tolerance: int = 100):
    """生成 Clash Meta YAML（分层结构）。

    pool:        全量节点（进「全部节点」select 组，不做健康检查，零测速开销）。
    auto_select: 稳定度评分 top N 节点（进「自动选择」url-test 组，客户端自动测速）。
                 只有这一小组会被客户端周期性测速，所以即便全量有上千节点，
                 客户端每次也只测这几十个，几秒完成、且大概率都是活的。

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
    # 自动选择：只放 top N，lazy + 较长 interval，客户端只测这几个
    lines.append("  - name: 自动选择")
    lines.append("    type: url-test")
    lines.append('    url: "http://www.gstatic.com/generate_204"')
    lines.append(f"    interval: {interval}")
    lines.append(f"    tolerance: {tolerance}")
    lines.append("    lazy: true")
    lines.append("    proxies:")
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
    ap.add_argument("--auto-select-size", type=int, default=60,
                     help="放进「自动选择」url-test 测速组的节点数（按稳定度评分取 top N），默认 60")
    ap.add_argument("--pool-cap", type=int, default=400,
                     help="全量节点池上限（按稳定度评分截断），默认 400；0 表示不限")
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

    # --- 按稳定度评分排序：评分高（出现轮数多）的排前面；评分相同用 key 哈希打散，
    #     避免永远偏向 SOURCES 列表里排在最前面的那几个源（修复之前 --limit 截断的源顺序偏差）---
    def _sort_key(n):
        rec = stats.get(node_key(n), {})
        return (-rec.get("rounds", 0), hash(node_key(n)) & 0xFFFFFFFF)
    nodes.sort(key=_sort_key)

    # 节点池上限（默认 400，0 表示不限）；--limit 作为旧参数别名
    pool_cap = args.limit if args.limit else args.pool_cap
    if pool_cap and len(nodes) > pool_cap:
        nodes = nodes[:pool_cap]
        print(f"节点池上限 {pool_cap}，截取评分靠前的 {pool_cap} 个")

    pool = nodes
    auto_select = pool[: max(1, args.auto_select_size)] if pool else []

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

    build_yaml(pool, auto_select, args.out)
    print(f"\n✅ 已生成: {args.out}（全量 {len(pool)} 个节点，其中 {len(auto_select)} 个进入「自动选择」测速组）")
    print("导入 Clash Meta 后：日常用「自动选择」策略组（客户端只测这几十个，几秒完成）；"
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
