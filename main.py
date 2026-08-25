import os
import re
import requests

# ================= 配置区域 =================
UID = "356171176"          # 目标 UP 主 UID
TARGET_KEYWORD = "洛天依"    # 监控关键词
MAX_HISTORY_COUNT = 20     # 最多保留的历史 BVID 数量（自动清理旧数据）
# ===========================================

PUSH_TOKEN = os.environ.get("PUSH_TOKEN")
RECORD_FILE = "last_bvid.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"https://space.bilibili.com/{UID}/video",
    "Origin": "https://space.bilibili.com",
    "Accept": "application/json, text/plain, */*"
}

def get_latest_videos(mid):
    """获取 UP 主最新 5 条投稿"""
    url = f"https://api.bilibili.com/x/space/arc/search?mid={mid}&ps=5&pn=1"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15).json()
        if resp.get("code") == 0 and "data" in resp and "list" in resp["data"]:
            return resp["data"]["list"]["vlist"]
    except Exception as e:
        print(f"获取投稿列表异常: {e}")
    return []

def get_video_tags(bvid):
    """获取指定视频的 Tag 标签"""
    url = f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15).json()
        if resp.get("code") == 0 and resp.get("data"):
            return [item["tag_name"] for item in resp["data"]]
    except Exception as e:
        print(f"获取视频标签异常: {e}")
    return []

def match_rules(title, tags):
    """判断标题或 Tag 是否包含关键词"""
    if re.search(TARGET_KEYWORD, title, re.IGNORECASE):
        return True, f"标题命中【{TARGET_KEYWORD}】"
    for tag in tags:
        if re.search(TARGET_KEYWORD, tag, re.IGNORECASE):
            return True, f"标签命中【{tag}】"
    return False, "未命中"

def send_pushplus(title, content):
    """发送微信推送"""
    if not PUSH_TOKEN:
        print("未检测到 PUSH_TOKEN")
        return
    url = "https://www.pushplus.plus/send"
    data = {
        "token": PUSH_TOKEN,
        "title": title,
        "content": content,
        "template": "html"
    }
    try:
        requests.post(url, json=data, timeout=15)
    except Exception as e:
        print(f"推送异常: {e}")

def load_history():
    """读取历史 BVID 列表"""
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return []

def save_history(history_list):
    """保存并自动清理超出数量的旧 BVID"""
    cleaned_list = history_list[:MAX_HISTORY_COUNT]
    with open(RECORD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(cleaned_list))

def main():
    history_bvids = load_history()
    vlist = get_latest_videos(UID)
    if not vlist:
        print("未能获取到投稿列表，退出")
        return

    # 首次运行：将当前最新的 5 个视频全部写入记录，防止首次运行产生历史轰炸
    if not history_bvids:
        initial_bvids = [v["bvid"] for v in vlist]
        save_history(initial_bvids)
        print(f"首次初始化完成，已记录最近 {len(initial_bvids)} 个视频作为基础数据。")
        return

    new_found = False
    # 倒序遍历（先处理较早发布的，后处理最新的）
    for video in reversed(vlist):
        bvid = video["bvid"]
        if bvid not in history_bvids:
            new_found = True
            title = video["title"]
            desc = video["description"]
            pic = video["pic"]
            author = video["author"]
            video_url = f"https://www.bilibili.com/video/{bvid}"

            tags = get_video_tags(bvid)
            tag_display = "、".join(tags) if tags else "无标签"
            print(f"发现新投稿: {title} ({bvid})")

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
                <p><a href="{video_url}">👉 点击直达 B 站观看</a></p>
                """
                send_pushplus(msg_title, msg_html)
                print(f"推送成功: {reason}")
            else:
                print(f"跳过推送: {reason}")

            # 插入到记录列表最前
            history_bvids.insert(0, bvid)

    if new_found:
        save_history(history_bvids)
    else:
        print("无新视频投稿")

if __name__ == "__main__":
    main()
