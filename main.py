#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili to Feishu Bot Notification Service
Enterprise-grade content monitoring, rich card builder, and instant dispatching agent.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ==============================================================================
# Logging Configuration
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BiliFeishuMonitor")


# ==============================================================================
# Configuration Management
# ==============================================================================
@dataclass(frozen=True)
class ServiceConfig:
    """Service runtime configuration loaded from environment variables."""
    # Feishu App Credentials
    feishu_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", ""))
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", ""))
    feishu_receive_id: str = field(default_factory=lambda: os.getenv("FEISHU_RECEIVE_ID", ""))
    feishu_receive_id_type: str = field(default_factory=lambda: os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id"))

    # Bilibili Target & Rule
    target_uid: str = field(default_factory=lambda: os.getenv("TARGET_UID", "356171176"))
    target_keyword: str = field(default_factory=lambda: os.getenv("TARGET_KEYWORD", "洛天依"))

    # Storage & Persistence
    max_history_count: int = field(default_factory=lambda: int(os.getenv("MAX_HISTORY_COUNT", "20")))
    record_file_path: Path = field(default_factory=lambda: Path(os.getenv("RECORD_FILE", "last_bvid.txt")))

    # Network Policies
    request_timeout: int = 15
    max_retries: int = 3
    backoff_factor: float = 0.5

    def validate(self) -> None:
        """Validate core secrets and config integrity."""
        missing = []
        if not self.feishu_app_id:
            missing.append("FEISHU_APP_ID")
        if not self.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not self.feishu_receive_id:
            missing.append("FEISHU_RECEIVE_ID")
        if missing:
            raise ValueError(f"Missing mandatory environment configurations: {', '.join(missing)}")


# ==============================================================================
# Domain Data Models
# ==============================================================================
@dataclass
class VideoEntity:
    """Represents a Bilibili Video entity with rich metadata."""
    bvid: str
    title: str
    description: str
    cover_url: str
    author: str
    tags: List[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def formatted_tags(self) -> str:
        if not self.tags:
            return "无标签"
        return " ".join([f"`#{tag}`" for tag in self.tags[:5]])

    @property
    def description_preview(self) -> str:
        clean_desc = self.description.strip().replace("\n", " ")
        if not clean_desc:
            return "该视频暂未填写简介。"
        return f"{clean_desc[:110]}..." if len(clean_desc) > 110 else clean_desc


# ==============================================================================
# HTTP Client Wrapper with Connection Pool & Retry
# ==============================================================================
class HttpClient:
    """Thread-safe HTTP client with connection pooling, exponential backoff, and standard headers."""

    def __init__(self, timeout: int = 15, max_retries: int = 3, backoff: float = 0.5) -> None:
        self._timeout = timeout
        self.session = requests.Session()

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs: Any) -> requests.Response:
        return self.session.get(url, headers=headers, timeout=self._timeout, **kwargs)

    def post(self, url: str, headers: Optional[Dict[str, str]] = None, **kwargs: Any) -> requests.Response:
        return self.session.post(url, headers=headers, timeout=self._timeout, **kwargs)


# ==============================================================================
# Feishu API Client & Card Builder
# ==============================================================================
class FeishuClient:
    """Client for Feishu Open Platform APIs and aesthetic Interactive Card constructor."""

    AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    UPLOAD_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"
    SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(self, config: ServiceConfig, http_client: HttpClient) -> None:
        self.cfg = config
        self.http = http_client
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def get_access_token(self) -> Optional[str]:
        """Fetch or return cached tenant access token."""
        if self._token and time.time() < (self._token_expires_at - 60):
            return self._token

        payload = {
            "app_id": self.cfg.feishu_app_id,
            "app_secret": self.cfg.feishu_app_secret
        }
        try:
            resp = self.http.post(self.AUTH_URL, json=payload).json()
            if resp.get("code") == 0:
                self._token = resp.get("tenant_access_token")
                expire_in = resp.get("expire", 7200)
                self._token_expires_at = time.time() + expire_in
                logger.info("Successfully refreshed Feishu tenant access token.")
                return self._token
            logger.error(f"Feishu authentication failed: {resp}")
        except requests.RequestException as exc:
            logger.error(f"Feishu authentication network exception: {exc}")
        return None

    def upload_image(self, image_url: str, referer_headers: Dict[str, str]) -> Optional[str]:
        """Download remote image and upload to Feishu IM assets."""
        token = self.get_access_token()
        if not token or not image_url:
            return None

        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        elif image_url.startswith("http://"):
            image_url = image_url.replace("http://", "https://", 1)

        try:
            img_res = self.http.get(image_url, headers=referer_headers)
            img_res.raise_for_status()

            headers = {"Authorization": f"Bearer {token}"}
            files = {"image": ("cover.jpg", img_res.content, "image/jpeg")}
            data = {"image_type": "message"}

            upload_resp = self.http.post(self.UPLOAD_IMAGE_URL, headers=headers, data=data, files=files).json()
            if upload_resp.get("code") == 0 and "data" in upload_resp:
                image_key = upload_resp["data"]["image_key"]
                logger.info(f"Image uploaded to Feishu asset store: {image_key}")
                return image_key
            logger.error(f"Feishu image upload failed: {upload_resp}")
        except Exception as exc:
            logger.error(f"Image processing pipeline exception: {exc}")
        return None

    def _build_rich_card(self, video: VideoEntity, match_reason: str, image_key: Optional[str]) -> Dict[str, Any]:
        """Construct a high-polish, structured Feishu Interactive Card."""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 顶部标题与匹配原因微件
        elements: List[Dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"### 📺 [{video.title}]({video.url})\n"
                        f"**🎯 命中状态：** <font color='green'>**{match_reason}**</font>"
                    )
                }
            }
        ]

        # 2. 多列结构化元数据卡片 (Column Set)
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": (
                                    f"**👤 投稿作者**\n{video.author}\n\n"
                                    f"**🆔 稿件 BV 号**\n`{video.bvid}`"
                                )
                            }
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": (
                                    f"**🏷️ 稿件标签**\n{video.formatted_tags}\n\n"
                                    f"**⏰ 监控发现时间**\n`{now_str}`"
                                )
                            }
                        }
                    ]
                }
            ]
        })

        # 3. 原生封面图 (大图自适应展示，支持点击全屏查看)
        if image_key:
            elements.append({
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": f"{video.title} 封面"},
                "mode": "fit_horizontal",
                "preview": True
            })

        # 4. 视频简介引用块
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📝 视频简介摘要**\n> {video.description_preview}"
            }
        })

        # 5. 分割线
        elements.append({"tag": "hr"})

        # 6. 双行动号召按钮组 (Primary 观看 + Default 空间)
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "▶️ 立即在 B 站观看"
                    },
                    "type": "primary",
                    "url": video.url
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "👤 访问 UP 主空间"
                    },
                    "type": "default",
                    "url": f"https://space.bilibili.com/{self.cfg.target_uid}"
                }
            ]
        })

        # 7. 底部运维注脚
        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"🤖 哔哩哔哩动态监控服务 • 监控目标 UID: {self.cfg.target_uid} • 关键词: {self.cfg.target_keyword}"
                }
            ]
        })

        return {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🎬 {video.author} 发布了新动态！"
                },
                "subtitle": {
                    "tag": "plain_text",
                    "content": f"Bilibili 动态监测 · 匹配到【{self.cfg.target_keyword}】"
                },
                "template": "indigo"  # 典雅靛蓝色主题
            },
            "elements": elements
        }

    def send_interactive_card(self, video: VideoEntity, match_reason: str, image_key: Optional[str]) -> bool:
        """Push rich interactive card message."""
        token = self.get_access_token()
        if not token:
            logger.error("Abort notification dispatch: Missing valid Feishu token.")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        params = {"receive_id_type": self.cfg.feishu_receive_id_type}

        card_data = self._build_rich_card(video, match_reason, image_key)
        payload = {
            "receive_id": self.cfg.feishu_receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_data)
        }

        try:
            resp = self.http.post(self.SEND_MESSAGE_URL, params=params, headers=headers, json=payload).json()
            if resp.get("code") == 0:
                logger.info(f"Feishu rich card dispatched successfully for BVID: {video.bvid}")
                return True
            logger.error(f"Feishu dispatch returned error: {resp}")
        except requests.RequestException as exc:
            logger.error(f"Feishu message dispatch network exception: {exc}")
        return False


# ==============================================================================
# Bilibili API Service
# ==============================================================================
class BilibiliService:
    """Client for pulling public video feeds and tags from Bilibili."""

    COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*"
    }

    def __init__(self, http_client: HttpClient) -> None:
        self.http = http_client

    def fetch_latest_videos(self, mid: str, page_size: int = 5) -> List[VideoEntity]:
        """Fetch latest published videos by UP ID."""
        url = f"https://api.bilibili.com/x/v2/medialist/resource/list?type=1&biz_id={mid}&ps={page_size}"
        try:
            resp = self.http.get(url, headers=self.COMMON_HEADERS).json()
            if resp.get("code") == 0 and "data" in resp and "media_list" in resp["data"]:
                media_list = resp["data"]["media_list"] or []
                return [
                    VideoEntity(
                        bvid=item.get("bv_id") or item.get("bvid", ""),
                        title=item.get("title", "未知标题"),
                        description=item.get("intro", "") or "暂无简介",
                        cover_url=item.get("cover", ""),
                        author=item.get("upper", {}).get("name", "UP主")
                    )
                    for item in media_list if item.get("bv_id") or item.get("bvid")
                ]
            logger.warning(f"Bilibili API unexpected response: {resp}")
        except Exception as exc:
            logger.error(f"Failed to fetch Bilibili video list: {exc}")
        return []

    def fetch_video_tags(self, bvid: str) -> List[str]:
        """Fetch tags associated with the given BV ID."""
        url = f"https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
        try:
            resp = self.http.get(url, headers=self.COMMON_HEADERS).json()
            if resp.get("code") == 0 and resp.get("data"):
                return [item["tag_name"] for item in resp["data"] if "tag_name" in item]
        except Exception as exc:
            logger.warning(f"Failed to fetch tags for BVID {bvid}: {exc}")
        return []


# ==============================================================================
# State & History Store
# ==============================================================================
class HistoryStore:
    """Atomic local state tracker for processed BV IDs."""

    def __init__(self, file_path: Path, max_limit: int = 20) -> None:
        self.file_path = file_path
        self.max_limit = max_limit

    def load(self) -> List[str]:
        if not self.file_path.exists():
            return []
        try:
            content = self.file_path.read_text(encoding="utf-8")
            return [line.strip() for line in content.splitlines() if line.strip()]
        except OSError as exc:
            logger.error(f"Failed to read history storage file: {exc}")
            return []

    def save(self, bvid_list: List[str]) -> None:
        cleaned = bvid_list[:self.max_limit]
        try:
            self.file_path.write_text("\n".join(cleaned), encoding="utf-8")
            logger.info(f"Updated local state history ({len(cleaned)} records).")
        except OSError as exc:
            logger.error(f"Failed to persist history file: {exc}")


# ==============================================================================
# Monitor Pipeline Coordinator
# ==============================================================================
class VideoMonitorEngine:
    """Business rule evaluator and execution pipeline."""

    def __init__(self, config: ServiceConfig) -> None:
        self.cfg = config
        self.http_client = HttpClient(
            timeout=config.request_timeout,
            max_retries=config.max_retries,
            backoff=config.backoff_factor
        )
        self.bili_service = BilibiliService(self.http_client)
        self.feishu_client = FeishuClient(self.cfg, self.http_client)
        self.history_store = HistoryStore(self.cfg.record_file_path, self.cfg.max_history_count)

    def match_rules(self, video: VideoEntity) -> Tuple[bool, str]:
        """Check if video title or tags hit monitoring keywords."""
        if re.search(self.cfg.target_keyword, video.title, re.IGNORECASE):
            return True, f"标题命中【{self.cfg.target_keyword}】"

        for tag in video.tags:
            if re.search(self.cfg.target_keyword, tag, re.IGNORECASE):
                return True, f"标签命中【{tag}】"

        return False, "未命中任何规则"

    def process_video(self, video: VideoEntity) -> None:
        """Fetch tags, evaluate rules and push notification if matched."""
        video.tags = self.bili_service.fetch_video_tags(video.bvid)
        is_matched, reason = self.match_rules(video)

        if not is_matched:
            logger.info(f"Skipping BV [{video.bvid}]: {reason}")
            return

        logger.info(f"Target matched [{video.bvid}]: {reason}. Uploading cover image...")
        image_key = self.feishu_client.upload_image(video.cover_url, self.bili_service.COMMON_HEADERS)
        self.feishu_client.send_interactive_card(video, reason, image_key)

    def run(self) -> None:
        """Main execution loop."""
        logger.info("Starting Bilibili video scan pipeline...")
        history = self.history_store.load()
        videos = self.bili_service.fetch_latest_videos(self.cfg.target_uid)

        if not videos:
            logger.warning("Empty video payload or failed to connect to Bilibili.")
            return

        # 首次冷启动：自动测试最新一条记录并初始化历史
        if not history:
            logger.info("Initial run detected. Executing test pipeline on latest post...")
            latest = videos[0]
            self.process_video(latest)
            self.history_store.save([v.bvid for v in videos])
            return

        # 日常扫描模式：按时间正序处理新投稿
        new_bvids = []
        for video in reversed(videos):
            if video.bvid not in history:
                logger.info(f"New video discovered: {video.title} ({video.bvid})")
                self.process_video(video)
                history.insert(0, video.bvid)
                new_bvids.append(video.bvid)

        if new_bvids:
            self.history_store.save(history)
            logger.info(f"Batch completed. Processed {len(new_bvids)} new entries.")
        else:
            logger.info("No newly published videos detected.")


# ==============================================================================
# Entry Point
# ==============================================================================
def main() -> None:
    config = ServiceConfig()
    try:
        config.validate()
    except ValueError as err:
        logger.critical(f"Config validation error: {err}")
        return

    engine = VideoMonitorEngine(config)
    engine.run()


if __name__ == "__main__":
    main()