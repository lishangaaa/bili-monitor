import os
import re
import requests

# ================= 配置区域 =================
UID = "356171176"  # 目标 UP 主 UID
TARGET_KEYWORD = "洛天依"  # 仅监控包含此关键词的标题或标签
# ===========================================

PUSH_TOKEN = os.environ.get("PUSH_TOKEN")
RECORD_FILE = "last_bvid.txt"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_latest_videos(mid):
    """获取 UP 主最新投稿列表"""
    url = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&ps=5&pn=1"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10).json()
        if resp.get("code") == 0:
            return resp["data"]["list"]["vlist"]
    except Exception as e:
        print(f"获取投稿列表异常: {e}")
    return []

def get_video_tags(bvid):
    """获取指定视频的全部 Tag 标签"""
    url = f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
    tags = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10).json()
        if resp.get("code") == 0 and resp.get("data"):
            tags = [item["tag_name"] for item in resp["data"]]
    except Exception as e:
        print(f"获取视频标签异常: {e}")
    return tags

def match_rules(title, tags):
    """判断标题或 Tag 是否包含 洛天依"""
    # 1. 检查标题是否包含
    if re.search(TARGET_KEYWORD, title, re.IGNORECASE):
        return True, f"标题命中【{TARGET_KEYWORD}】"

    # 2. 检查标签是否包含
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
    requests.post(url, json=data, timeout=10)

def main():
    last_bvid = ""
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            last_bvid = f.read().strip()

    vlist = get_latest_videos(UID)
    if not vlist:
        print("未能获取到投稿列表")
        return

    latest_video = vlist[0]
    current_bvid = latest_video["bvid"]

    # 发现新投稿
    if current_bvid != last_bvid:
        title = latest_video["title"]
        desc = latest_video["description"]
        pic = latest_video["pic"]
        author = latest_video["author"]
        video_url = f"https://www.bilibili.com/video/{current_bvid}"

        # 获取该视频的所有标签
        tags = get_video_tags(current_bvid)
        tag_display = "、".join(tags) if tags else "无标签"

        print(f"检测到新视频: {title}")
        print(f"视频标签: {tag_display}")

        is_matched, reason = match_rules(title, tags)

        if is_matched:
            msg_title = f"【洛天依提醒】{author} 发布了新视频！"
            msg_html = f"""
            <h3><a href="{video_url}">{title}</a></h3>
            <p><b>UP主：</b>{author}</p>
            <p><b>命中原因：</b>{reason}</p>
            <p><b>视频标签：</b>{tag_display}</p>
            <p><b>简介：</b>{desc}</p>
            <img src="{pic}" style="max-width:100%; border-radius:8px;" />
            <p><a href="{video_url}">👉 点击直达 B 站播放页面</a></p>
            """
            send_pushplus(msg_title, msg_html)
            print(f"推送成功: {reason}")
        else:
            print(f"跳过推送: {reason}")

        # 记录已处理过的视频 ID
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            f.write(current_bvid)
    else:
        print("无新投稿")

if __name__ == "__main__":
    main()
