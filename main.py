import os
import re
import json
import requests

# ================= 配置区域 =================
UID = "356171176"          # 目标 UP 主 UID
TARGET_KEYWORD = "洛天依"    # 监控关键词
MAX_HISTORY_COUNT = 20     # 最多保留历史记录数量
# ===========================================

APP_ID = os.environ.get("FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
RECEIVE_ID = os.environ.get("FEISHU_RECEIVE_ID")
RECEIVE_ID_TYPE = os.environ.get("FEISHU_RECEIVE_ID_TYPE", "chat_id")
RECORD_FILE = "last_bvid.txt"

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*"
}

def get_tenant_access_token():
    """获取飞书应用凭证"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10).json()
        if resp.get("code") == 0:
            return resp.get("tenant_access_token")
        print(f"飞书 Token 获取失败: {resp}")
    except Exception as e:
        print(f"飞书 Token 请求异常: {e}")
    return None

def upload_image_to_feishu(pic_url, token):
    """下载 B 站封面并上传到飞书，获取合规的 image_key"""
    if not pic_url or not token:
        return None

    if pic_url.startswith("//"):
        pic_url = f"https:{pic_url}"
    elif pic_url.startswith("http://"):
        pic_url = pic_url.replace("http://", "https://", 1)

    try:
        # 1. 突破防盗链下载图片
        img_res = requests.get(pic_url, headers=COMMON_HEADERS, timeout=15)
        if img_res.status_code != 200:
            print(f"下载封面失败: 状态码 {img_res.status_code}")
            return None

        # 2. 上传到飞书开放平台
        upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
        upload_headers = {"Authorization": f"Bearer {token}"}
        data = {"image_type": "message"}
        files = {"image": ("cover.jpg", img_res.content, "image/jpeg")}

        resp = requests.post(upload_url, headers=upload_headers, data=data, files=files, timeout=20).json()
        if resp.get("code") == 0 and "data" in resp:
            image_key = resp["data"]["image_key"]
            print(f"封面成功转存至飞书，Key: {image_key}")
            return image_key
        else:
            print(f"上传封面至飞书失败: {resp}")
    except Exception as e:
        print(f"图片中转异常: {e}")
    return None

def send_feishu_card(title, author, reason, tags_str, desc, video_url, pic_url):
    """发送带原生封面图的飞书卡片"""
    token = get_tenant_access_token()
    if not token or not RECEIVE_ID:
        print("缺少飞书 Token 或 RECEIVE_ID 配置")
        return

    # 上传封面获取 key
    image_key = upload_image_to_feishu(pic_url, token)

    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={RECEIVE_ID_TYPE}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**📺 视频标题：** [{title}]({video_url})\n"
                    f"**🎯 命中原因：** {reason}\n"
                    f"**🏷️ 视频标签：** {tags_str}\n"
                    f"**📝 视频简介：** {desc[:120]}..."
                )
            }
        }
    ]

    # 如果封面上传成功，追加原生图片组件
    if image_key:
        elements.append({
            "tag": "img",
            "img_key": image_key,
            "alt": {
                "tag": "plain_text",
                "content": "视频封面"
            },
            "mode": "fit_horizontal"
        })

    # 追加底部直达按钮
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": "👉 点击在 B 站中播放"
                },
                "type": "primary",
                "url": video_url
            }
        ]
    })

    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🎬【洛天依提醒】{author} 发布了新视频！"
            },
            "template": "blue"
        },
        "elements": elements
    }

    payload = {
        "receive_id": RECEIVE_ID,
        "msg_type": "interactive",
        "content": json.dumps(card_content)
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15).json()
        print(f"飞书消息推送响应: {res}")
    except Exception as e:
        print(f"飞书推送异常: {e}")

def get_latest_videos(mid):
    """获取最新投稿列表"""
    url = f"https://api.bilibili.com/x/v2/medialist/resource/list?type=1&biz_id={mid}&ps=5"
    try:
        resp = requests.get(url, headers=COMMON_HEADERS, timeout=15).json()
        if resp.get("code") == 0 and "data" in resp and "media_list" in resp["data"]:
            media_list = resp["data"]["media_list"]
            if media_list:
                result = []
                for item in media_list:
                    result.append({
                        "bvid": item.get("bv_id") or item.get("bvid"),
                        "title": item.get("title", ""),
                        "description": item.get("intro", ""),
                        "pic": item.get("cover", ""),
                        "author": item.get("upper", {}).get("name", "UP主")
                    })
                return result
        print(f"接口返回异常: {resp}")
    except Exception as e:
        print(f"获取视频列表异常: {e}")
    return []

def get_video_tags(bvid):
    """获取视频标签"""
    url = f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
    try:
        resp = requests.get(url, headers=COMMON_HEADERS, timeout=10).json()
        if resp.get("code") == 0 and resp.get("data"):
            return [item["tag_name"] for item in resp["data"]]
    except Exception as e:
        print(f"获取标签异常: {e}")
    return []

def match_rules(title, tags):
    """判断是否命中关键词"""
    if re.search(TARGET_KEYWORD, title, re.IGNORECASE):
        return True, f"标题命中【{TARGET_KEYWORD}】"
    for tag in tags:
        if re.search(TARGET_KEYWORD, tag, re.IGNORECASE):
            return True, f"标签命中【{tag}】"
    return False, "未命中"

def load_history():
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return []

def save_history(history_list):
    cleaned_list = history_list[:MAX_HISTORY_COUNT]
    with open(RECORD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(cleaned_list))

def main():
    history_bvids = load_history()
    vlist = get_latest_videos(UID)
    if not vlist:
        print("未获取到视频列表，退出")
        return

    # 首次运行测试模式
    if not history_bvids:
        print("检测到首次运行，正在对最新一条视频进行全流程图片推送测试...")
        test_video = vlist[0]
        bvid = test_video["bvid"]
        title = test_video["title"]
        desc = test_video.get("description", "") or "暂无简介"
        author = test_video.get("author", "目标UP主")
        pic = test_video.get("pic", "")
        video_url = f"https://www.bilibili.com/video/{bvid}"

        tags = get_video_tags(bvid)
        tag_display = "、".join(tags) if tags else "无标签"

        is_matched, reason = match_rules(title, tags)
        if is_matched:
            send_feishu_card(title, author, reason, tag_display, desc, video_url, pic)
            print(f"测试推送成功: {reason}")
        else:
            print(f"最新视频未命中【{TARGET_KEYWORD}】")

        initial_bvids = [v["bvid"] for v in vlist if v.get("bvid")]
        save_history(initial_bvids)
        return

    # 日常监控模式
    new_found = False
    for video in reversed(vlist):
        bvid = video.get("bvid")
        if not bvid:
            continue
        if bvid not in history_bvids:
            new_found = True
            title = video["title"]
            desc = video.get("description", "") or "暂无简介"
            author = video.get("author", "目标UP主")
            pic = video.get("pic", "")
            video_url = f"https://www.bilibili.com/video/{bvid}"

            tags = get_video_tags(bvid)
            tag_display = "、".join(tags) if tags else "无标签"
            print(f"发现新投稿: {title} ({bvid})")

            is_matched, reason = match_rules(title, tags)
            if is_matched:
                send_feishu_card(title, author, reason, tag_display, desc, video_url, pic)
                print(f"已推送飞书: {reason}")
            else:
                print(f"跳过推送: {reason}")

            history_bvids.insert(0, bvid)

    if new_found:
        save_history(history_bvids)
    else:
        print("无新视频投稿")

if __name__ == "__main__":
    main()