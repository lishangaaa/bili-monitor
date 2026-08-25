#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili to Feishu Bot Notification Service
Enterprise-grade content monitoring and high-aesthetic card builder.
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
    feishu_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", ""))
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", ""))
    feishu_receive_id: str = field(default_factory=lambda: os.getenv("FEISHU_RECEIVE_ID", ""))
    feishu_receive_id_type: str = field(default_factory=lambda: os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id"))

    target_uid: str = field(default_factory=lambda: os.getenv("TARGET_UID", "356171176"))
    target_keyword: str = field(default_factory=lambda: os.getenv("TARGET_KEYWORD", "洛天依"))

    max_history_count: int = field(default_factory=lambda: int(os.getenv("MAX_HISTORY_COUNT", "20")))
    record_file_path: Path = field(default_factory=lambda: Path(os.getenv("RECORD_FILE", "last_bvid.txt")))

    request_timeout: int = 15
    max_retries: int = 3
    backoff_factor: float = 0.5

    def validate(self) -> None:
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
    """Represents a Bilibili Video entity."""
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
            return "无相关标签"
        return "  ".join([f"`#{tag}`" for tag in self.tags[:6]])

    @property
    def description_preview(self) -> str:
        clean = self.description.strip().replace("\r\n", "\n")
        if not clean:
            return "*作者未填写简介*"
        lines = clean.splitlines()
        preview = " ".join([line.strip() for line in lines if line.strip()])
        return f"{preview[:140]}..." if len(preview) > 140 else preview


# ==============================================================================
# HTTP Client Wrapper with Connection Pool & Retry
# ==============================================================================
class HttpClient:
    """Thread-safe HTTP client with connection pooling and retry mechanism."""

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
# Feishu API Client & Modern Card Builder
# ==============================================================================
class FeishuClient:
    """Client for Feishu Open Platform with modern visual card templates."""

    AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    UPLOAD_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"
    SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    def __init__(self, config: ServiceConfig, http_client: HttpClient) -> None:
        self.cfg = config
        self.http = http_client
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def get_access_token(self) -> Optional[str]:
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
                return self._token
            logger.error(f"Feishu token retrieval failed: {resp}")
        except requests.RequestException as exc:
            logger.error(f"Feishu network exception: {exc}")
        return None

    def upload_image(self, image_url: str, referer_headers: Dict[str, str]) -> Optional[str]:
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
                return upload_resp["data"]["image_key"]
            logger.error(f"Feishu image upload failed: {upload_resp}")
        except Exception as exc:
            logger.error(f"Image pipeline exception: {exc}")
        return None

    def _build_modern_card(self, video: VideoEntity, match_reason: str, image_key: Optional[str]) -> Dict[str, Any]:
        """Builds a streamlined, magazine-style modern card."""
        now_time = datetime.datetime.now().strftime("%H:%M")
        elements: List[Dict[str, Any]] = []

        # 1. 顶置大图封面 (Hero Image)
        if image_key:
            elements.append({
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": video.title},
                "mode": "fit_horizontal",
                "preview": True
            })

        # 2. 核心主标题与命中状态条
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"## [{video.title}]({video.url})\n"
                    f"<font color='green'>●</font> **规则命中：** <font color='carmine'>**{match_reason}**</font>"
                )
            }
        })

        # 3. 规整信息区 (采用轻量级两行排版，兼顾电脑端与移动端)
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**UP主：** {video.author}  ｜  **BV号：** `{video.bvid}`\n"
                    f"**标签：** {video.formatted_tags}"
                )
            }
        })

        # 4. 引用式优雅简介
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"> {video.description_preview}"
            }
        })

        elements.append({"tag": "hr"})

        # 5. 极简按钮栏
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "立即前往 B 站观看 →"
                    },
                    "type": "primary",
                    "url": video.url
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "UP主主页"
                    },
                    "type": "default",
                    "url": f"https://space.bilibili.com/{self.cfg.target_uid}"
                }
            ]
        })

        # 6. 轻量注脚
        elements.append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"Bilibili 动态监测 • 捕获时间 {now_time} • UID: {self.cfg.target_uid}"
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
                    "content": f"✨ {video.author} 投稿了新视频"
                },
                "subtitle": {
                    "tag": "plain_text",
                    "content": f"命中监控词「{self.cfg.target_keyword}」"
                },
                "template": "carmine"  # Bilibili 标志性绯红/粉系风格
            },
            "elements": elements
        }

    def send_interactive_card(self, video: VideoEntity, match_reason: str, image_key: Optional[str]) -> bool:
        token = self.get_access_token()
        if not token:
            logger.error("Abort send: Missing valid Feishu token.")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        params = {"receive_id_type": self.cfg.feishu_receive_id_type}
        card_data = self._build_modern_card(video, match_reason, image_key)

        payload = {
            "receive_id": self.cfg.feishu_receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_data)
        }

        try:
            resp = self.http.post(self.SEND_MESSAGE_URL, params=params, headers=headers, json=payload).json()
            if resp.get("code") == 0:
                logger.info(f"Feishu modern card dispatched successfully for BVID: {video.bvid}")
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
        if re.search(self.cfg.target_keyword, video.title, re.IGNORECASE):
            return True, f"标题包含「{self.cfg.target_keyword}」"

        for tag in video.tags:
            if re.search(self.cfg.target_keyword, tag, re.IGNORECASE):
                return True, f"标签包含「{tag}」"

        return False, "未命中任何规则"

    def process_video(self, video: VideoEntity) -> None:
        video.tags = self.bili_service.fetch_video_tags(video.bvid)
        is_matched, reason = self.match_rules(video)

        if not is_matched:
            logger.info(f"Skipping BV [{video.bvid}]: {reason}")
            return

        logger.info(f"Target matched [{video.bvid}]: {reason}. Processing cover...")
        image_key = self.feishu_client.upload_image(video.cover_url, self.bili_service.COMMON_HEADERS)
        self.feishu_client.send_interactive_card(video, reason, image_key)

    def run(self) -> None:
        logger.info("Starting Bilibili video scan pipeline...")
        history = self.history_store.load()
        videos = self.bili_service.fetch_latest_videos(self.cfg.target_uid)

        if not videos:
            logger.warning("Empty video payload or failed to connect to Bilibili.")
            return

        if not history:
            logger.info("Initial run detected. Executing test pipeline on latest post...")
            latest = videos[0]
            self.process_video(latest)
            self.history_store.save([v.bvid for v in videos])
            return

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