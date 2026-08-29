# 免费节点自动聚合工具

解决的问题：以前要一个个去 GitHub 上找免费节点、手动拼 Clash 配置，很麻烦。
这个工具自动从几个公开的免费节点聚合仓库 + Telegram 免费节点频道抓取节点，解析、去重，
直接生成一份 Clash Meta 能用的 YAML 配置文件。

## 原理

1. `fetch_nodes.py` 从 `SOURCES` 列表里的几个订阅地址下载节点（目前接的是
   MatinGhanbari、barry-far、Epodonios 这三个仓库），同时从 `telegramchannels.json`
   里的 Telegram 免费节点频道抓取（走 `https://t.me/s/<频道>` 公开预览页，无需登录 / Bot Token）。
2. 解析 vmess / vless / trojan / ss 四种协议的节点链接。
3. 按 `协议+地址+端口` 去重，避免同一个节点重复出现。
4. 生成一个「自动选择」（url-test）策略组 + 「手动选择」策略组的 Clash Meta 配置。
   **节点是否能连通，交给 Clash Meta 客户端自己测速筛选**——脚本本身不对外批量
   探测端口，这样比较安全，也不会重蹈之前 GitHub Actions 账号因为批量 socket
   探测被限制的问题。

## Telegram 免费节点频道源

`telegramchannels.json` 里是频道用户名列表（当前 181 个，沿用了之前 TGParse 项目里
在用的那批频道），脚本会并发抓取每个频道的公开预览页，提取 vmess / vless / trojan / ss
链接并入节点池。

- **管理频道**：直接在 `telegramchannels.json` 里增删用户名（JSON 数组，一个元素一个
  频道名），删掉不想要的、加你想加的，push 之后下次自动更新就生效。
- **关闭 Telegram 源**：跑的时候加 `--no-telegram`，就只用 `SOURCES` 里的订阅地址。
- **并发控制**：默认 12 并发抓取频道，可用 `--tg-concurrency` 调整。
- **协议范围**：目前只解析 vmess / vless / trojan / ss 四种协议；如果频道里发的是
  hysteria2 / tuic 等其他协议，会跳过（后续可以扩展）。
- **网络注意**：本机直连 t.me 不一定通（部分地区网络无法直接访问），Telegram 源主要
  靠 GitHub Actions 云端跑（CI 机器能访问 t.me）；想在本地跑 Telegram 源，需要你自己的
  网络能访问 t.me。

## 使用方法

只需要电脑上装了 Python 3（不用装任何第三方库，纯标准库写的）。

```bash
python3 fetch_nodes.py --out clash_config_003.yaml
```

- `--out` 指定输出文件名，你可以按自己原来的习惯继续编号
  （`clash_config_002.yaml` → `clash_config_003.yaml` ...）。
- `--limit` 控制最多保留多少个节点，默认 0（不限制，源里抓到多少新节点就出多少）。
  节点数量大的话客户端加载、批量测速会变慢，嫌太多可以自己传个数字限制，比如 `--limit 300`

跑完之后终端会告诉你抓到几个、去重后剩几个，最后生成文件。

## 导入客户端

- **Clash Meta for Android / NekoBox**：设置里「从文件导入」选生成的 yaml 即可。
- **v2rayN（Windows）**：v2rayN 也支持导入 Clash 格式配置，或者你可以把
  `proxies` 部分单独拿出来按 vmess/ss 链接导入，看你平时习惯哪种。
- 导入后选「自动选择」这个策略组，客户端会自动测速切换最快的节点，
  不用你自己一个个试。

## 想让它自动定时跑（不用每次手动执行）

**Windows：**
双击 `run_update.bat` 试一下能不能跑通，没问题的话把它加到
「任务计划程序」（Task Scheduler）里，设置每天/每隔几小时触发一次，
文件名会自动按日期命名（如 `clash_config_20260823.yaml`），不会互相覆盖。

**手机（Android）：**
手机上不方便跑 Python，建议还是在电脑上定时生成好，
然后配置文件放到一个你手机能访问到的地方（比如自己的网盘、或者局域网共享），
Clash Meta for Android 支持配置「订阅链接」，如果你愿意，
后续可以把生成的 yaml 传到一个免费静态托管（比如 GitHub Pages 或者
Cloudflare Pages）上，Clash Meta 就能直接填订阅链接自动更新了——
这个我可以下一步帮你搭，告诉我一声就行。

## 放到云端定时跑（推荐，手机也能自动更新）

不想每次都手动开电脑跑脚本的话，可以把这个脚本放到你现有的 `-VPN` 仓库里，
用 GitHub Actions 每 6 小时自动跑一次，生成的配置文件自动提交回仓库，
手机 Clash Meta / NekoBox 直接填订阅链接就能自动刷新，全程不用碰电脑。

**这个方案只做"下载订阅 + 解析 + 生成 yaml + git commit"，不对节点做任何
批量端口连通性探测，跟之前账号被限制的"批量 socket 测试"是两回事，
正常使用不会再触发限制。**

### 部署步骤

1. 把 `fetch_nodes.py` 放到你的 `-VPN` 仓库根目录（和 `.git` 同一层）。
2. 把 `.github/workflows/update-nodes.yml` 这个文件也放进仓库对应路径
   （`.github/workflows/` 文件夹，文件夹名必须一字不差）。
3. 去仓库 **Settings → Actions → General → Workflow permissions**，
   选择「**Read and write permissions**」并保存
   （不开这个，Actions 没法把生成的配置提交回仓库）。
4. `git add . && git commit -m "加入自动更新节点" && git push` 推上去。
5. 去仓库的 **Actions** 标签页，能看到「自动更新节点配置」这个工作流，
   点 **Run workflow** 手动跑一次试试，正常的话几十秒后仓库里会多出
   `clash_config_latest.yaml` 这个文件，并且以后每 6 小时自动更新一次
   （也可以改 workflow 文件里的 cron 表达式调整频率）。

### 手机订阅链接

工作流跑成功之后，用这个链接（把仓库名换成你实际的）作为订阅地址：

```
https://raw.githubusercontent.com/chenweimin30-hue/-VPN/main/clash_config_latest.yaml
```

Clash Meta for Android / NekoBox 里「新建订阅」，把这个链接填进去，
设置自动更新间隔（比如 6 小时），之后就是全自动的了，不需要电脑参与。

> 如果仓库默认分支不是 `main` 而是 `master`，把链接里的 `main` 换成 `master`。

## v2ray 系列客户端怎么用（v2rayN / v2rayNG / NekoBox）

`clash_config_latest.yaml` 是 Clash Meta 格式，v2rayN 较新版本虽然也支持
Clash 内核、能直接导入，但为了保险起见，脚本现在会**额外多生成一份**
`clash_config_latest_v2ray.txt`——这是 v2ray 系那批客户端最通用的订阅格式
（一堆 vmess://、trojan://、ss:// 链接整体做了一次 base64），
v2rayN、v2rayNG、NekoBox 基本都认这个格式，不挑版本。

云端跑起来之后，这份文件的订阅链接是：

```
https://raw.githubusercontent.com/chenweimin30-hue/-VPN/main/clash_config_latest_v2ray.txt
```

**v2rayN（Windows）导入步骤：**
1. 打开 v2rayN，菜单栏「订阅」→「订阅设置」→「添加」
2. 地址栏粘贴上面这个链接，备注随便填，保存
3. 菜单栏「订阅」→「更新订阅」（或者右键那条订阅单独更新）
4. 服务器列表里就会出现这批节点，右键某个节点「设为活动服务器」，
   或者用 v2rayN 自带的「批量测速」筛一下connect

**v2rayNG / NekoBox（Android）：**
1. 右上角「+」→「从剪贴板导入」或者「订阅设置」里新增一条订阅
2. 粘贴同样的链接，保存后点「更新」
3. 列表刷新出节点后，长按某个节点测速，或者用批量测速功能

两份文件（Clash 版和 v2ray 版）内容对应的是同一批节点，选哪个看你习惯用哪个客户端，
Clash Meta 系（Clash Meta for Android、Clash Verge 等）用 yaml 那份，
v2ray 系（v2rayN、v2rayNG、NekoBox）用 v2ray 那份就行。

## 跨天去重（避免每天导入都混着昨天已经删掉的节点）

脚本会自动记录"哪些节点之前已经生成过"，存在 `seen_nodes.json` 这个文件里
（和 `fetch_nodes.py` 同目录）。默认行为：

- 每次运行只输出**没出现过的新节点**，之前生成过的会自动跳过，
  不会在你导入的时候又混进你已经删掉的那些
- 记录**保留 7 天后自动过期**——不是永久拉黑：免费节点池子就这几千个，
  如果不过期，历史记录会越攒越多，用不了几天就会把整个节点池"耗尽"，
  变成每次都找不到新节点。过期之后，如果那个节点还活着，会重新出现，
  相当于"忘记"了之前的判断，给它一个重新被看到的机会

**相关参数：**

```bash
python3 fetch_nodes.py --out x.yaml --history-days 3      # 改成3天过期，想更快"忘记"
python3 fetch_nodes.py --out x.yaml --no-history           # 这次不做跨天去重，也不记录（临时看全量）
python3 fetch_nodes.py --out x.yaml --reset-history        # 清空历史，相当于重新开始
```

云端定时跑（GitHub Actions）那边已经把 `seen_nodes.json` 也加进了自动提交里，
所以云端和本地共用同一份历史记录会持续累积，不用你额外做什么。

## 关于测速

**为什么云端 Actions 里不做测速：** 之前账号被限制，本质原因不是"测速这件事不合理"，
而是在 GitHub 共享的 CI 机器上，短时间内对几千个第三方外部主机发起批量 socket 连接，
这种流量模式很容易被平台判定为"借用共享 IP 做网络扫描"——这是平台对**自己基础设施**
使用方式的限制，跟你在自己的网络环境下做同样的事完全是两回事。所以现在的
`update-nodes.yml` 工作流固定不带 `--test`，脚本里也加了一层保险：只要检测到自己是在
GitHub Actions 里跑（`GITHUB_ACTIONS` 环境变量），就算你手滑加了 `--test` 也会自动跳过。

**测速想接进来，有两条可行路线：**

### 方案一：本地测速，只是多一步（最简单，推荐）

`fetch_nodes.py` 已经支持 `--test` 参数，在**自己电脑**上跑：

```bash
python3 fetch_nodes.py --out clash_config_local.yaml --limit 300 --test
```

会对候选节点逐个做 TCP 三次握手测试（默认 20 并发、3 秒超时），连不上的直接丢掉，
剩下的按延迟从低到高排序再写入配置。几千个节点全测一遍大概几分钟，
适合你偶尔想要一份"确认能连"的干净配置的时候用。

缺点：这一步依然需要你打开电脑手动跑一次，没法接进云端全自动流程。

### 方案二：自建 Runner，测速也能自动化

如果你希望"云端自动跑 + 测速"都要，可以把 Actions 的执行机器从 GitHub 提供的共享机器
换成**你自己的机器**（自建 Runner，即 self-hosted runner）。这样测速产生的连接流量
从你自己的网络出去，不经过 GitHub 的共享 IP，自然也就不涉及"共享基础设施被当成扫描
工具"这个问题。两种可行的机器来源：

- **自己电脑挂个 Runner**：装个 GitHub 官方的 Runner 程序，电脑开着的时候 Actions 触发了就在
  你电脑上跑（本质上和方案一差不多，只是变成了 GitHub 帮你按计划触发，而不是你自己记得跑）。
- **一台常年在线的免费/低价 VPS**（比如 Oracle Cloud 有长期免费的小机器，一些平台也有学生
  优惠），把 Runner 装在这台机器上，就能做到真正意义上「云端自动 + 带测速」，完全不需要
  自己的电脑参与。缺点是需要自己维护一台 Linux 机器，有一点门槛。

这个如果你想要往下走（不管是哪一种），告诉我一声，我可以给你写具体的部署步骤和
Runner 安装命令。

## 增删节点来源

- **订阅地址**：打开 `fetch_nodes.py`，改 `SOURCES` 这个列表就行，加一行新的订阅地址、
  或者删掉你觉得不稳定的来源。
- **Telegram 频道**：编辑 `telegramchannels.json`，加一行频道用户名（如 `"my_channel"`）、
  或者删掉不要的，push 后下次自动更新生效。

## 关于稳定性

这些都是公开免费节点，仓库每天更新，但节点本身存活率不高很正常
（免费节点大部分几小时到几天就会失效），所以建议配置文件本身也
不要留太久不更新，隔个一两天重新跑一次比较好。
