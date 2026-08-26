#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bilibili UP 主视频监控并推送到飞书。

功能：
1. 监控指定 UP 主的最新视频。
2. 检查视频标题或标签是否包含目标关键词。
3. 命中后推送飞书互动卡片。
4. 只有成功处理的视频才会写入历史记录。
5. 支持单次执行，也支持按间隔持续轮询。

默认单次执行：
    python monitor.py

持续轮询：
    设置环境变量 POLL_INTERVAL_SECONDS，例如：
    POLL_INTERVAL_SECONDS=300 python monitor.py
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry


# ==============================================================================
# 日志配置
# ==============================================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("BiliFeishuMonitor")


# ==============================================================================
# 配置管理
# ==============================================================================

def get_int_env(name: str, default: int, minimum: int = 0) -> int:
    """安全读取整数环境变量。"""
    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        return default

    try:
        value = int(raw_value)
    except ValueError:
        logger.warning(
            "环境变量 %s=%r 不是有效整数，使用默认值 %s。",
            name,
            raw_value,
            default,
        )
        return default

    if value < minimum:
        logger.warning(
            "环境变量 %s=%s 小于允许的最小值 %s，使用默认值 %s。",
            name,
            value,
            minimum,
            default,
        )
        return default

    return value


@dataclass(frozen=True)
class ServiceConfig:
    """服务运行配置。"""

    # 飞书配置
    feishu_app_id: str = field(
        default_factory=lambda: os.getenv("FEISHU_APP_ID", "").strip()
    )
    feishu_app_secret: str = field(
        default_factory=lambda: os.getenv("FEISHU_APP_SECRET", "").strip()
    )
    feishu_receive_id: str = field(
        default_factory=lambda: os.getenv("FEISHU_RECEIVE_ID", "").strip()
    )
    feishu_receive_id_type: str = field(
        default_factory=lambda: os.getenv(
            "FEISHU_RECEIVE_ID_TYPE",
            "chat_id",
        ).strip()
    )

    # 监控目标
    target_uid: str = field(
        default_factory=lambda: os.getenv(
            "TARGET_UID",
            "356171176",
        ).strip()
    )
    target_keyword: str = field(
        default_factory=lambda: os.getenv(
            "TARGET_KEYWORD",
            "洛天依",
        ).strip()
    )

    # 历史记录
    max_history_count: int = field(
        default_factory=lambda: get_int_env(
            "MAX_HISTORY_COUNT",
            20,
            minimum=1,
        )
    )
    record_file_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("RECORD_FILE", "last_bvid.txt")
        )
    )

    # HTTP 配置
    request_timeout: int = field(
        default_factory=lambda: get_int_env(
            "REQUEST_TIMEOUT",
            15,
            minimum=1,
        )
    )
    max_retries: int = field(
        default_factory=lambda: get_int_env(
            "MAX_RETRIES",
            3,
            minimum=0,
        )
    )
    backoff_factor: float = field(
        default_factory=lambda: float(
            os.getenv("BACKOFF_FACTOR", "0.5")
        )
    )

    # 轮询配置
    # 0 表示只执行一次；大于 0 表示持续运行，单位为秒。
    poll_interval_seconds: int = field(
        default_factory=lambda: get_int_env(
            "POLL_INTERVAL_SECONDS",
            0,
            minimum=0,
        )
    )

    # 每次从 Bilibili 获取的最新视频数量
    fetch_page_size: int = field(
        default_factory=lambda: get_int_env(
            "FETCH_PAGE_SIZE",
            10,
            minimum=1,
        )
    )

    def validate(self) -> None:
        missing: List[str] = []

        if not self.feishu_app_id:
            missing.append("FEISHU_APP_ID")

        if not self.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")

        if not self.feishu_receive_id:
            missing.append("FEISHU_RECEIVE_ID")

        if not self.target_uid:
            missing.append("TARGET_UID")

        if not self.target_keyword:
            missing.append("TARGET_KEYWORD")

        if missing:
            raise ValueError(
                "缺少必要的环境变量配置："
                + ", ".join(missing)
            )

        if self.backoff_factor < 0:
            raise ValueError("BACKOFF_FACTOR 不能小于 0。")


# ==============================================================================
# 数据模型
# ==============================================================================

@dataclass
class VideoEntity:
    """Bilibili 视频实体。"""

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

        return "  ".join(
            f"`#{tag}`"
            for tag in self.tags[:6]
        )

    @property
    def description_preview(self) -> str:
        clean = self.description.strip().replace("\r\n", "\n")

        if not clean:
            return "*作者未填写简介*"

        lines = clean.splitlines()
        preview = " ".join(
            line.strip()
            for line in lines
            if line.strip()
        )

        if len(preview) > 140:
            return f"{preview[:140]}..."

        return preview


# ==============================================================================
# HTTP 客户端
# ==============================================================================

class HttpClient:
    """
    HTTP 客户端。

    只对 GET、HEAD、OPTIONS 等幂等请求启用自动重试，
    不对飞书发送消息的 POST 请求自动重试，以降低重复推送风险。
    """

    def __init__(
        self,
        timeout: int = 15,
        max_retries: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self.timeout = timeout
        self.session = requests.Session()

        retry_strategy = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            redirect=max_retries,
            status=max_retries,
            backoff_factor=backoff,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD", "OPTIONS"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
        )

        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def get(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> requests.Response:
        return self.session.get(
            url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )

    def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> requests.Response:
        return self.session.post(
            url,
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )

    @staticmethod
    def parse_json(response: requests.Response) -> Optional[Dict[str, Any]]:
        """安全解析 JSON 响应。"""
        try:
            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                logger.error("接口返回的 JSON 不是对象：%r", payload)
                return None

            return payload
        except requests.RequestException as exc:
            logger.error(
                "HTTP 请求失败，status=%s，error=%s",
                response.status_code,
                exc,
            )
        except ValueError as exc:
            logger.error("接口返回内容不是有效 JSON：%s", exc)

        return None


# ==============================================================================
# 飞书客户端
# ==============================================================================

class FeishuClient:
    """飞书开放平台客户端。"""

    AUTH_URL = (
        "https://open.feishu.cn/"
        "open-apis/auth/v3/tenant_access_token/internal"
    )
    UPLOAD_IMAGE_URL = (
        "https://open.feishu.cn/open-apis/im/v1/images"
    )
    SEND_MESSAGE_URL = (
        "https://open.feishu.cn/open-apis/im/v1/messages"
    )

    def __init__(
        self,
        config: ServiceConfig,
        http_client: HttpClient,
    ) -> None:
        self.cfg = config
        self.http = http_client
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def get_access_token(self) -> Optional[str]:
        """获取并缓存 tenant_access_token。"""
        if (
            self._token
            and time.time() < self._token_expires_at - 60
        ):
            return self._token

        payload = {
            "app_id": self.cfg.feishu_app_id,
            "app_secret": self.cfg.feishu_app_secret,
        }

        try:
            response = self.http.post(
                self.AUTH_URL,
                json=payload,
            )
            result = self.http.parse_json(response)

            if not result:
                return None

            if result.get("code") == 0:
                token = result.get("tenant_access_token")
                expire_in = result.get("expire", 7200)

                if not token:
                    logger.error(
                        "飞书 Token 接口成功，但没有返回 tenant_access_token。"
                    )
                    return None

                self._token = str(token)
                self._token_expires_at = (
                    time.time() + int(expire_in)
                )
                return self._token

            logger.error("获取飞书 Token 失败：%s", result)

        except requests.RequestException as exc:
            logger.error("获取飞书 Token 时发生网络异常：%s", exc)
        except (TypeError, ValueError) as exc:
            logger.error("解析飞书 Token 配置时失败：%s", exc)

        return None

    def upload_image(
        self,
        image_url: str,
        referer_headers: Dict[str, str],
    ) -> Optional[str]:
        """
        下载 Bilibili 封面并上传到飞书。

        图片上传失败不会阻止后续发送文字卡片。
        """
        token = self.get_access_token()

        if not token or not image_url:
            return None

        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        elif image_url.startswith("http://"):
            image_url = image_url.replace(
                "http://",
                "https://",
                1,
            )

        try:
            image_response = self.http.get(
                image_url,
                headers=referer_headers,
            )
            image_response.raise_for_status()

            headers = {
                "Authorization": f"Bearer {token}",
            }
            files = {
                "image": (
                    "cover.jpg",
                    image_response.content,
                    "image/jpeg",
                ),
            }
            data = {
                "image_type": "message",
            }

            upload_response = self.http.post(
                self.UPLOAD_IMAGE_URL,
                headers=headers,
                data=data,
                files=files,
            )
            upload_result = self.http.parse_json(upload_response)

            if (
                upload_result
                and upload_result.get("code") == 0
                and isinstance(upload_result.get("data"), dict)
            ):
                image_key = upload_result["data"].get("image_key")

                if image_key:
                    return str(image_key)

            logger.error(
                "飞书图片上传失败：%s",
                upload_result,
            )

        except requests.RequestException as exc:
            logger.error("图片下载或上传网络异常：%s", exc)
        except Exception as exc:
            logger.error("图片处理流程异常：%s", exc)

        return None

    def _build_modern_card(
        self,
        video: VideoEntity,
        match_reason: str,
        image_key: Optional[str],
    ) -> Dict[str, Any]:
        """构造飞书互动卡片。"""
        now_time = datetime.datetime.now().strftime("%H:%M")
        elements: List[Dict[str, Any]] = []

        if image_key:
            elements.append(
                {
                    "tag": "img",
                    "img_key": image_key,
                    "alt": {
                        "tag": "plain_text",
                        "content": video.title,
                    },
                    "mode": "fit_horizontal",
                    "preview": True,
                }
            )

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"## [{video.title}]({video.url})\n"
                        "<font color='green'>●</font> "
                        f"**规则命中:** "
                        f"<font color='carmine'>"
                        f"**{match_reason}**"
                        f"</font>"
                    ),
                },
            }
        )

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**UP主:** {video.author}  |  "
                        f"**BV号:** `{video.bvid}`\n"
                        f"**标签:** {video.formatted_tags}"
                    ),
                },
            }
        )

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"> {video.description_preview}",
                },
            }
        )

        elements.append({"tag": "hr"})

        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "立即前往 B 站观看",
                        },
                        "type": "primary",
                        "url": video.url,
                    },
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "UP主主页",
                        },
                        "type": "default",
                        "url": (
                            "https://space.bilibili.com/"
                            f"{self.cfg.target_uid}"
                        ),
                    },
                ],
            }
        )

        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            "Bilibili 动态监测 • "
                            f"捕获时间 {now_time} • "
                            f"UID: {self.cfg.target_uid}"
                        ),
                    },
                ],
            }
        )

        return {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": (
                        f"{video.author} 投稿了新视频"
                    ),
                },
                "subtitle": {
                    "tag": "plain_text",
                    "content": (
                        f"命中监控词「{self.cfg.target_keyword}」"
                    ),
                },
                "template": "carmine",
            },
            "elements": elements,
        }

    def send_interactive_card(
        self,
        video: VideoEntity,
        match_reason: str,
        image_key: Optional[str],
    ) -> bool:
        """发送飞书卡片，返回是否成功。"""
        token = self.get_access_token()

        if not token:
            logger.error("发送中止：没有有效的飞书 Token。")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        params = {
            "receive_id_type": self.cfg.feishu_receive_id_type,
        }

        card_data = self._build_modern_card(
            video,
            match_reason,
            image_key,
        )

        payload = {
            "receive_id": self.cfg.feishu_receive_id,
            "msg_type": "interactive",
            "content": json.dumps(
                card_data,
                ensure_ascii=False,
            ),
        }

        try:
            response = self.http.post(
                self.SEND_MESSAGE_URL,
                params=params,
                headers=headers,
                json=payload,
            )
            result = self.http.parse_json(response)

            if result and result.get("code") == 0:
                logger.info(
                    "飞书卡片发送成功，BVID：%s",
                    video.bvid,
                )
                return True

            logger.error(
                "飞书卡片发送失败：%s",
                result,
            )

        except requests.RequestException as exc:
            logger.error(
                "飞书卡片发送网络异常：%s",
                exc,
            )
        except Exception as exc:
            logger.error(
                "飞书卡片发送流程异常：%s",
                exc,
            )

        return False


# ==============================================================================
# Bilibili 客户端
# ==============================================================================

class BilibiliService:
    """Bilibili 视频和标签接口客户端。"""

    COMMON_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self, http_client: HttpClient) -> None:
        self.http = http_client

    def fetch_latest_videos(
        self,
        mid: str,
        page_size: int = 10,
    ) -> Optional[List[VideoEntity]]:
        """
        获取指定 UP 主的最新视频。

        返回：
        - List：请求成功，列表可能为空；
        - None：请求失败或响应格式异常。
        """
        url = (
            "https://api.bilibili.com/x/v2/medialist/resource/list"
            f"?type=1&biz_id={mid}&ps={page_size}"
        )

        try:
            response = self.http.get(
                url,
                headers=self.COMMON_HEADERS,
            )
            result = self.http.parse_json(response)

            if not result:
                return None

            if (
                result.get("code") != 0
                or not isinstance(result.get("data"), dict)
            ):
                logger.warning(
                    "Bilibili 视频接口返回异常：%s",
                    result,
                )
                return None

            media_list = result["data"].get("media_list", [])

            if not isinstance(media_list, list):
                logger.warning(
                    "Bilibili 视频接口 media_list 格式异常：%r",
                    media_list,
                )
                return None

            videos: List[VideoEntity] = []

            for item in media_list:
                if not isinstance(item, dict):
                    continue

                bvid = item.get("bv_id") or item.get("bvid")

                if not bvid:
                    continue

                upper = item.get("upper")
                if not isinstance(upper, dict):
                    upper = {}

                videos.append(
                    VideoEntity(
                        bvid=str(bvid),
                        title=str(item.get("title", "未知标题")),
                        description=str(
                            item.get("intro", "")
                            or "暂无简介"
                        ),
                        cover_url=str(item.get("cover", "")),
                        author=str(
                            upper.get("name", "UP主")
                        ),
                    )
                )

            return videos

        except requests.RequestException as exc:
            logger.error(
                "获取 Bilibili 视频列表时发生网络异常：%s",
                exc,
            )
        except Exception as exc:
            logger.error(
                "解析 Bilibili 视频列表时发生异常：%s",
                exc,
            )

        return None

    def fetch_video_tags(
        self,
        bvid: str,
    ) -> Optional[List[str]]:
        """
        获取视频标签。

        返回 None 表示请求失败；
        返回空列表表示请求成功但没有标签。
        """
        url = (
            "https://api.bilibili.com/x/tag/archive/tags"
            f"?bvid={bvid}"
        )

        try:
            response = self.http.get(
                url,
                headers=self.COMMON_HEADERS,
            )
            result = self.http.parse_json(response)

            if not result:
                return None

            if result.get("code") != 0:
                logger.warning(
                    "获取视频标签失败，BVID=%s，响应=%s",
                    bvid,
                    result,
                )
                return None

            data = result.get("data") or []

            if not isinstance(data, list):
                logger.warning(
                    "视频标签接口返回格式异常，BVID=%s，data=%r",
                    bvid,
                    data,
                )
                return None

            tags: List[str] = []

            for item in data:
                if not isinstance(item, dict):
                    continue

                tag_name = item.get("tag_name")

                if tag_name:
                    tags.append(str(tag_name))

            return tags

        except requests.RequestException as exc:
            logger.warning(
                "获取视频标签时发生网络异常，BVID=%s，error=%s",
                bvid,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "解析视频标签时发生异常，BVID=%s，error=%s",
                bvid,
                exc,
            )

        return None


# ==============================================================================
# 历史记录
# ==============================================================================

class HistoryStore:
    """保存已经处理过的 BV 号。"""

    def __init__(
        self,
        file_path: Path,
        max_limit: int = 20,
    ) -> None:
        self.file_path = file_path
        self.max_limit = max_limit

    def load(self) -> List[str]:
        if not self.file_path.exists():
            return []

        try:
            content = self.file_path.read_text(
                encoding="utf-8",
            )

            result: List[str] = []
            seen = set()

            for line in content.splitlines():
                bvid = line.strip()

                if not bvid or bvid in seen:
                    continue

                seen.add(bvid)
                result.append(bvid)

            return result[:self.max_limit]

        except OSError as exc:
            logger.error(
                "读取历史记录失败：%s",
                exc,
            )
            return []

    def save(self, bvid_list: List[str]) -> bool:
        """原子保存历史记录。"""
        cleaned: List[str] = []
        seen = set()

        for bvid in bvid_list:
            bvid = str(bvid).strip()

            if not bvid or bvid in seen:
                continue

            seen.add(bvid)
            cleaned.append(bvid)

            if len(cleaned) >= self.max_limit:
                break

        try:
            self.file_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            # 先写入同目录临时文件，再替换正式文件。
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.file_path.name}.",
                suffix=".tmp",
                dir=str(self.file_path.parent),
                text=True,
            )

            try:
                with os.fdopen(
                    fd,
                    "w",
                    encoding="utf-8",
                ) as temp_file:
                    if cleaned:
                        temp_file.write(
                            "\n".join(cleaned)
                            + "\n"
                        )
                    else:
                        temp_file.write("")

                    temp_file.flush()
                    os.fsync(temp_file.fileno())

                os.replace(temp_name, self.file_path)

            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise

            logger.info(
                "历史记录更新成功，共 %s 条。",
                len(cleaned),
            )
            return True

        except OSError as exc:
            logger.error(
                "保存历史记录失败：%s",
                exc,
            )
            return False


# ==============================================================================
# 监控业务流程
# ==============================================================================

class VideoMonitorEngine:
    """视频监控业务流程。"""

    def __init__(self, config: ServiceConfig) -> None:
        self.cfg = config

        self.http_client = HttpClient(
            timeout=config.request_timeout,
            max_retries=config.max_retries,
            backoff=config.backoff_factor,
        )

        self.bili_service = BilibiliService(
            self.http_client,
        )

        self.feishu_client = FeishuClient(
            config,
            self.http_client,
        )

        self.history_store = HistoryStore(
            config.record_file_path,
            config.max_history_count,
        )

    def match_rules(
        self,
        video: VideoEntity,
    ) -> Tuple[bool, str]:
        """
        使用普通文本包含关系匹配关键词。

        例如 TARGET_KEYWORD=洛天依 时：
        标题为“洛天依新歌”可以命中；
        标签为“洛天依原创”也可以命中。
        """
        keyword = self.cfg.target_keyword.casefold()

        if keyword in video.title.casefold():
            return (
                True,
                f"标题包含「{self.cfg.target_keyword}」",
            )

        for tag in video.tags:
            if keyword in tag.casefold():
                return (
                    True,
                    f"标签包含「{tag}」",
                )

        return False, "标题和标签均未包含目标关键词"

    def process_video(self, video: VideoEntity) -> bool:
        """
        处理单个视频。

        返回 True：
        - 视频不匹配，但已经检查完成；
        - 视频匹配，并且飞书发送成功。

        返回 False：
        - 获取标签失败；
        - 命中后飞书发送失败。
        """
        tags = self.bili_service.fetch_video_tags(
            video.bvid,
        )

        # 标签接口失败时不要写历史，下一轮继续重试。
        if tags is None:
            logger.warning(
                "视频标签获取失败，暂不记录历史，BVID=%s",
                video.bvid,
            )
            return False

        video.tags = tags

        is_matched, reason = self.match_rules(video)

        if not is_matched:
            logger.info(
                "视频未命中关键词，跳过推送，BVID=%s，标题=%s",
                video.bvid,
                video.title,
            )
            return True

        logger.info(
            "发现目标视频，BVID=%s，命中原因=%s",
            video.bvid,
            reason,
        )

        # 封面失败时仍然发送不带图片的卡片。
        image_key = self.feishu_client.upload_image(
            video.cover_url,
            self.bili_service.COMMON_HEADERS,
        )

        send_success = self.feishu_client.send_interactive_card(
            video,
            reason,
            image_key,
        )

        if not send_success:
            logger.warning(
                "飞书推送失败，暂不记录历史，BVID=%s",
                video.bvid,
            )
            return False

        return True

    def run_once(self) -> None:
        """执行一次扫描。"""
        logger.info(
            "开始扫描 UP 主：%s，关键词：%s",
            self.cfg.target_uid,
            self.cfg.target_keyword,
        )

        history = self.history_store.load()

        videos = self.bili_service.fetch_latest_videos(
            self.cfg.target_uid,
            self.cfg.fetch_page_size,
        )

        if videos is None:
            logger.warning(
                "获取视频列表失败，本轮不修改历史记录。"
            )
            return

        if not videos:
            logger.info("当前没有可处理的视频。")
            return

        # 第一次运行时只检查最新视频，避免首次启动一次性推送旧视频。
        if not history:
            logger.info(
                "检测到首次运行，只处理最新视频，"
                "不会批量推送历史视频。"
            )

            latest = videos[0]
            success = self.process_video(latest)

            if success:
                self.history_store.save(
                    [video.bvid for video in videos]
                )
            else:
                logger.warning(
                    "首次运行处理失败，本次不写入历史，"
                    "下次启动会继续重试。"
                )

            return

        new_bvids: List[str] = []

        # 接口通常按最新到最旧返回，因此反转后按时间顺序处理。
        for video in reversed(videos):
            if video.bvid in history:
                continue

            logger.info(
                "发现新视频：%s（%s）",
                video.title,
                video.bvid,
            )

            success = self.process_video(video)

            # 无论是否命中关键词，只要检查流程成功，就记录历史。
            # 发送失败或标签获取失败则不记录，下一轮继续重试。
            if success:
                history.insert(0, video.bvid)
                new_bvids.append(video.bvid)
            else:
                logger.warning(
                    "视频本轮处理失败，保留为未处理状态，BVID=%s",
                    video.bvid,
                )

        if new_bvids:
            saved = self.history_store.save(history)

            if saved:
                logger.info(
                    "本轮完成，成功处理 %s 个新视频。",
                    len(new_bvids),
                )
            else:
                logger.error(
                    "本轮处理完成，但历史记录保存失败。"
                    "下次运行可能会重复处理这些视频。"
                )
        else:
            logger.info("没有发现新视频。")

    def run_forever(self) -> None:
        """持续轮询模式。"""
        interval = self.cfg.poll_interval_seconds

        if interval <= 0:
            self.run_once()
            return

        logger.info(
            "持续轮询已开启，间隔 %s 秒。",
            interval,
        )

        while True:
            started_at = time.time()

            try:
                self.run_once()
            except Exception:
                # 防止单轮未预期异常导致常驻进程直接退出。
                logger.exception(
                    "本轮扫描发生未捕获异常。"
                )

            elapsed = time.time() - started_at
            sleep_seconds = max(1, interval - int(elapsed))

            logger.info(
                "本轮扫描结束，%s 秒后开始下一轮。",
                sleep_seconds,
            )

            try:
                time.sleep(sleep_seconds)
            except KeyboardInterrupt:
                logger.info("收到退出信号，监控程序结束。")
                return


# ==============================================================================
# 程序入口
# ==============================================================================

def main() -> None:
    config = ServiceConfig()

    try:
        config.validate()
    except ValueError as exc:
        logger.critical(
            "配置校验失败：%s",
            exc,
        )
        return

    engine = VideoMonitorEngine(config)

    try:
        engine.run_forever()
    except KeyboardInterrupt:
        logger.info("程序已退出。")


if __name__ == "__main__":
    main()