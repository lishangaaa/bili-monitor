import os
import re
import requests

# ================= 配置区域 =================
UID = "356171176"  # 目标 UP 主 UID
TARGET_KEYWORD = "洛天依"
# ===========================================

PUSH_TOKEN = os.environ.get("PUSH_TOKEN")
RECORD_FILE = "last_bvid.txt"

# 模拟真实浏览器请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"https://space.bilibili.com/{UID}/video",
    "Origin": "https://space.bilibili.com",
    "Accept": "application/json, text/plain, */*"
}

def get_latest_videos(mid):
    """获取 UP 主最新投稿列表"""
    url = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&ps=5&pn=1"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15).json()
        print(f"API 响应状态: {resp.get('code')}")
        if resp.get("code") == 0 and "data" in resp and "list" in resp["data"]:
            return resp["data"]["list"]["vlist"]
        else:
            print(f"B站接口返回信息: {resp}")
    except Exception as e:
        print(f"请求投稿列表异常: {e}")
    return []

def get_video_tags(bvid):
    """获取视频 Tag 标签"""
    url = f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
    tags = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15).json()
        if resp.get("code") == 0 and resp.get("data"):
            tags = [item["tag_name"] for item in resp["data"]]
    except Exception as e:
        print(f"获取视频标签异常: {e}")
    return tags

def match_rules(title, tags):
    """判断是否命中规则"""
    if re.search(TARGET_KEYWORD, title, re.IGNORECASE):
        return True, f"标题命中【{TARGET_KEYWORD}】"
    for tag in tags:
        if re.search(TARGET_KEYWORD, tag, re.IGNORECASE):
            return True, f"标签命中【{tag}】"
    return False, "未命中洛天依相关内容"

def send_pushplus(title, content):
    """发送微信推送"""
    if not PUSH_TOKEN:
        print("未检测到 PUSH_TOKEN 环境变量")
        return
    url = "https://www.pushplus.plus/send"
    data = {
        "token": PUSH_TOKEN,
        "title": title,
        "content": content,
        "template": "html"
    }
    try:
        res = requests.post(url, json=data, timeout=15).json()
        print(f"PushPlus 推送响应: {res}")
    except Exception as e:
        print(f"推送异常: {e}")

def main():
    last_bvid = ""
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            last_bvid = f.read().strip()

    vlist = get_latest_videos(UID)
    if not vlist:
        print("未能获取到投稿列表，等待下次重试")
        return

    latest_video = vlist[0]
    current_bvid = latest_video["bvid"]

    # 首次运行或发现新视频
    if current_bvid != last_bvid:
        title = latest_video["title"]
        desc = latest_video["description"]
        pic = latest_video["pic"]
        author = latest_video["author"]
        video_url = f"https://www.bilibili.com/video/{current_bvid}"

        tags = get_video_tags(current_bvid)
        tag_display = "、".join(tags) if tags else "无标签"

        print(f"当前最新视频: {title}")
        print(f"视频标签: {tag_display}")

        # 如果不是首次初始化（即之前已有记录），且命中规则才发推送
        if last_bvid != "":
            is_matched, reason = match_rules(title, tags)
            if is_matched:
                msg_title = f"【洛天依提醒】{author} 发布了新视频！"
                msg_html = f"""
                <h3><a href="{video_url}">{title}</a></h3>
                <p><b>UP主：</b>{author}</p>
                <p><b>命中原因：</b>{reason}</p>
                <p><b>标签：</b>{tag_display}</p>
                <p><b>简介：</b>{desc}</p>
                <img src="{pic}" style="max-width:100%; border-radius:8px;" />
                <p><a href="{video_url}">👉 点击直达 B 站播放</a></p>
                """
                send_pushplus(msg_title, msg_html)
                print(f"已发送推送: {reason}")
            else:
                print(f"跳过推送: {reason}")
        else:
            print("首次初始化，已记录当前最新视频，下次发新视频时开始提醒。")

        # 写入记录文件
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            f.write(current_bvid)
    else:
        print("无新投稿")

if __name__ == "__main__":
    main()
