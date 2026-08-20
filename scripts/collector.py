
import requests
 
资料来源 = [
    “https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt”
    “https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/HTTP.txt”
]
 
OUTPUT_FILE = 'proxies.txt'
 
def fetch_source（URL）：
    试试：
        r = 请求。get（URL，超时=10）
        R。raise_for_status（）
        线 = r。短信。分线（）
        代理 = []
        for line in lines：
            线 = 线。条（）
            if line and not 。开篇（'#'）：
                代理。附录（行）
        返回 代理
    除非 例外 as e：
        print（f“Failed to fetch {url}： {e}”）
        回归 []
 
def main（）：
    all_raw = set（）
    for url in SOURCES：
        print（f“Fetching {url}”）
        代理 = fetch_source（URL）
        all_raw。更新（代理）
    print（f“总原始代理数： {len（all_raw）}”）
 
    # 不再对第三方服务器发起连通性测试(socket连接探测)，
    # 仅做抓取、去重、格式整理，符合 GitHub Actions 使用条款。
    good = Sorted（all_raw）
 
    with open（OUTPUT_FILE， 'w'） as f：
        f。写（'\n'.加入（善））
    print（f“已完成，保存了{len（good）}代理（未测试）。”）
 
    # ========== 自动生成 Clash 配置文件 ==========
    如果 不 好：
        print（“无代理，跳过 Clash 配置生成。”）
        回归
 
    print（“生成clash_config.yaml...”）
    clash_proxies = []
    for idx， proxy in enumerate（good， 1）：
        if '：' 不在 proxy：
            继续
        服务器，port = proxy。rsplit（'：'， 1）


f“”“ - 名称：”Proxy{idx}”

    type: http
 服务器：{server}
 port： {port}“”“”） 
 







    clash_content = f“”“混合端口：7890




模式：规则
日志层面：信息
 
代理：






代理群：
 - 名称：“代理”
 类型：选择
代理：
 - 直接
{group_proxy_list}
 
规则：          











































