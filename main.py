import asyncio
import os
import re
import tempfile
import time
import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Json, File
from astrbot.api.star import Context, Star, register

# 常量
TEMP_FILE_PREFIX = "astrbot_meting_"
CHUNK_SIZE = 8192
SESSION_EXPIRY = 60  # 搜索列表缓存 60 秒

@register("astrbot_plugin_meting", "chuyegzs", "基于 MetingAPI 的音频点歌插件", "1.0.5")
class MetingPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self._sessions = {}  # 结构: {session_id: {"results": [...], "timestamp": 123456}}
        self._http_session = None
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())  # 启动定时清理任务

    async def _get_session(self):
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _session_cleanup_loop(self):
        """后台循环：每隔一分钟清理一次内存中过期的歌曲列表"""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                expired_keys = [
                    k for k, v in self._sessions.items()
                    if now - v["timestamp"] > SESSION_EXPIRY
                ]
                for k in expired_keys:
                    del self._sessions[k]
                if expired_keys:
                    logger.debug(f"已清理 {len(expired_keys)} 个过期的搜索会话")
            except Exception as e:
                logger.error(f"会话清理任务异常: {e}")

    def _get_config(self, key, default=None):
        return self.config.get(key, default) if self.config else default

    # --- 安全校验 ---
    async def _validate_url(self, url: str):
        if not url or not url.startswith(("http://", "https://")):
            return False, "无效协议"
        black_list = ["127.0.0.1", "localhost", "192.168."]
        if any(b in url for b in black_list):
            return False, "受限地址"
        return True, ""

    # --- 核心下载逻辑 ---
    async def _download_song(self, url: str) -> str | None:
        session = await self._get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    return None

                size_limit = self._get_config("max_file_size", 50) * 1024 * 1024
                if int(resp.headers.get('Content-Length', 0)) > size_limit:
                    return None
                suffix = ".mp3"
                ctype = resp.headers.get('Content-Type', '').lower()
                if 'flac' in ctype:
                    suffix = ".flac"
                elif 'm4a' in ctype or 'mp4' in ctype:
                    suffix = ".m4a"
                fd, path = tempfile.mkstemp(suffix=suffix, prefix=TEMP_FILE_PREFIX)
                with os.fdopen(fd, 'wb') as f:
                    while True:
                        chunk = await resp.content.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                return path
        except Exception as e:
            logger.error(f"下载失败: {e}")
            return None

    # --- API请求核心方法 ---
    async def _fetch_api(self, type, query, server):
        api_url = self._get_config("api_config", {}).get("api_url", "")
        if api_url == "custom":
            api_url = self._get_config("api_config", {}).get("custom_api_url", "")

        api_type = self._get_config("api_config", {}).get("api_type", 1)
        endpoint = f"{api_url.rstrip('/')}/api" if api_type == 1 else api_url

        params = {"server": server, "type": type}
        if type == "song":
            params["id"] = query
        else:
            params["keywords"] = query

        session = await self._get_session()
        try:
            async with session.get(endpoint, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.error(f"API 请求失败: {e}")
        return None

    # --- 发送逻辑 ---
    async def _play_song_logic(self, event: AstrMessageEvent, song: dict):
        song_url = song.get("url")
        if not song_url:
            yield event.plain_result("无法获取歌曲地址")
            return
        try:
            yield event.plain_result(f"正在准备音频: {song.get('title')}...")
            temp_file = await self._download_song(song_url)
            if temp_file:
                yield event.chain_result([File(temp_file)])
                await asyncio.sleep(15)
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            else:
                yield event.plain_result("文件下载失败")
        except Exception as e:
            logger.error(f"播放逻辑出错: {e}")

    @filter.command("点歌")
    async def search_song(self, event: AstrMessageEvent, name: str):
        source = self._get_config("default_source", "netease")
        results = await self._fetch_api("search", name, source)
        if not results:
            yield event.plain_result("❌ 未找到相关歌曲")
            return
        mode = self._get_config("selection_mode", "manual")

        if mode == "direct":
            async for res in self._play_song_logic(event, results[0]):
                yield res
        else:
            # 存储搜索结果并记录当前时间戳
            session_id = event.unified_msg_origin
            self._sessions[session_id] = {
                "results": results,
                "timestamp": time.time()
            }

            resp = f"🔍 搜索结果 (有效时间 {SESSION_EXPIRY}s)：\n"
            for i, s in enumerate(results[:self._get_config("search_result_count", 10)]):
                resp += f"{i+1}. {s.get('title')} - {s.get('author')}\n"
            yield event.plain_result(resp.strip())

    @filter.on_event(AstrMessageEvent)
    async def handle_selection(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        if session_id not in self._sessions:
            return

        msg = event.get_message_str().strip()
        if not msg.isdigit():
            return

        # 校验是否过期
        session_data = self._sessions.get(session_id)
        if time.time() - session_data["timestamp"] > SESSION_EXPIRY:
            del self._sessions[session_id]
            yield event.plain_result("⌛ 会话已过期，请重新搜歌")
            return
        idx = int(msg) - 1
        results = session_data["results"]

        if 0 <= idx < len(results):
            self._sessions.pop(session_id)  # 选中后立即销毁
            async for res in self._play_song_logic(event, results[idx]):
                yield res

    # --- 自动解析 URL ---
    @filter.on_event(AstrMessageEvent)
    async def handle_url_parse(self, event: AstrMessageEvent):
        if not self._get_config("auto_parse_url", True):
            return

        msg = event.get_message_str().strip()
        patterns = {
            "netease": r"music\.163\.com/.*song\?id=(\d+)",
            "tencent": r"y\.qq\.com/.*songDetail/([a-zA-Z0-9]+)",
            "kugou": r"kugou\.com/.*hash=([a-zA-Z0-9]+)",
            "kuwo": r"kuwo\.cn/play_detail/(\d+)"
        }
        for source, pattern in patterns.items():
            match = re.search(pattern, msg)
            if match:
                song_id = match.group(1)
                yield event.plain_result(f"🔗 链接解析中...")
                song_info = await self._fetch_api("song", song_id, source)
                if song_info:
                    song_data = song_info[0] if isinstance(song_info, list) else song_info
                    async for res in self._play_song_logic(event, song_data):
                        yield res
                return

    def _cleanup_temp_files(self):
        """强制清理临时文件"""
        temp_dir = tempfile.gettempdir()
        for f in os.listdir(temp_dir):
            if f.startswith(TEMP_FILE_PREFIX):
                try:
                    os.remove(os.path.join(temp_dir, f))
                except:
                    pass

    async def terminate(self):
        """插件终止时销毁任务和资源"""
        if self._cleanup_task:
            self._cleanup_task.cancel()  # 取消后台任务
        if self._http_session:
            await self._http_session.close()
        self._sessions.clear()
        self._cleanup_temp_files()
