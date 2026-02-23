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
from astrbot.core.pipeline.respond import plain_result, chain_result

# 常量
TEMP_FILE_PREFIX = "astrbot_meting_"
CHUNK_SIZE = 8192
SESSION_EXPIRY = 60  # 搜索列表缓存 60 秒

# 严格遵循AstrBot插件注册规范
@register(
    name="astrbot_plugin_meting",
    author="chuyegzs",
    description="基于MetingAPI的音频点歌插件（AstrBot适配版）",
    version="1.0.6"
)
class MetingPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}  # 兼容无配置场景
        self._sessions = {}  # 结构: {session_id: {"results": [...], "timestamp": 123456}}
        self._http_session = None
        self._cleanup_task = None
        # 初始化时启动后台任务（遵循AstrBot惰性初始化）

    # AstrBot插件标准初始化方法
    async def initialize(self):
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
        logger.info("Meting点歌插件初始化完成（AstrBot适配版）")

    # 惰性获取HTTP Session，适配AstrBot异步环境
    async def _get_session(self):
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "AstrBot/MetingPlugin/1.4.0",
                    "Referer": "https://astrbot.app/"
                }
            )
        return self._http_session

    # 后台会话清理任务（增加取消异常捕获，适配AstrBot资源回收）
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
            except asyncio.CancelledError:
                logger.debug("会话清理任务已被AstrBot框架取消")
                break
            except Exception as e:
                logger.error(f"会话清理任务异常: {e}", exc_info=True)

    # 配置获取封装，兼容AstrBot配置加载逻辑
    def _get_config(self, key, default=None):
        return self.config.get(key, default)

    # --- 安全校验 ---
    async def _validate_url(self, url: str):
        if not url or not url.startswith(("http://", "https://")):
            return False, "无效协议"
        black_list = ["127.0.0.1", "localhost", "192.168.", "10.", "172."]
        if any(b in url for b in black_list):
            return False, "受限内网地址"
        return True, ""

    # --- 核心下载逻辑 ---
    async def _download_song(self, url: str) -> str | None:
        # 先执行安全校验
        is_valid, _ = await self._validate_url(url)
        if not is_valid:
            return None
        try:
            session = await self._get_session()
            async with session.get(
                url, 
                timeout=aiohttp.ClientTimeout(total=300),
                allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"下载失败，状态码: {resp.status}")
                    return None

                # 大小限制（默认50MB）
                size_limit = self._get_config("max_file_size", 50) * 1024 * 1024
                content_len = int(resp.headers.get('Content-Length', 0))
                if content_len > size_limit or content_len == 0:
                    logger.warning(f"文件大小超出限制/未知，大小: {content_len}")
                    return None
                
                # 自动匹配音频后缀
                suffix = ".mp3"
                ctype = resp.headers.get('Content-Type', '').lower()
                if 'flac' in ctype:
                    suffix = ".flac"
                elif 'm4a' in ctype or 'mp4' in ctype or 'x-m4a' in ctype:
                    suffix = ".m4a"
                elif 'wav' in ctype:
                    suffix = ".wav"

                # 创建临时文件（适配跨平台）
                fd, path = tempfile.mkstemp(suffix=suffix, prefix=TEMP_FILE_PREFIX)
                with os.fdopen(fd, 'wb') as f:
                    while True:
                        chunk = await resp.content.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                return path
        except Exception as e:
            logger.error(f"下载失败: {e}", exc_info=True)
            return None

    # --- API请求核心方法（适配MetingAPI多类型）---
    async def _fetch_api(self, type: str, query: str, server: str):
        api_url = self._get_config("api_config", {}).get("api_url", "https://musicapi.chuyel.top/meting/")
        if api_url == "custom":
            api_url = self._get_config("api_config", {}).get("custom_api_url", "https://musicapi.chuyel.top/meting/")
        if not api_url:
            logger.error("MetingAPI地址未配置")
            return None

        api_type = self._get_config("api_config", {}).get("api_type", 1)
        endpoint = f"{api_url.rstrip('/')}/api" if api_type == 1 else api_url

        # 标准MetingAPI参数
        params = {
            "server": server,
            "type": type,
            "r": str(int(time.time() * 1000))  # 防缓存
        }
        if type == "song":
            params["id"] = query
        else:
            params["keywords"] = query

        try:
            session = await self._get_session()
            async with session.get(
                endpoint, 
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"MetingAPI请求失败，状态码: {resp.status}")
        except Exception as e:
            logger.error(f"MetingAPI请求异常: {e}", exc_info=True)
        return None

    # --- 核心播放逻辑（适配AstrBot消息返回规范）---
    async def _play_song_logic(self, event: AstrMessageEvent, song: dict):
        song_url = song.get("url") or song.get("download_url")
        song_title = song.get("title") or song.get("name") or "未知歌曲"
        song_author = song.get("author") or song.get("artist") or "未知歌手"
        
        if not song_url:
            yield plain_result(f"❌ 无法获取【{song_title} - {song_author}】的播放地址")
            return
        
        # 安全校验
        is_valid, reason = await self._validate_url(song_url)
        if not is_valid:
            yield plain_result(f"❌ 歌曲地址校验失败: {reason}")
            return

        try:
            yield plain_result(f"📥 正在准备音频: {song_title} - {song_author}")
            temp_file = await self._download_song(song_url)
            if temp_file and os.path.exists(temp_file):
                # 适配AstrBot File组件规范
                yield chain_result([File(temp_file, name=f"{song_title}-{song_author}{os.path.splitext(temp_file)[1]}")])
                # 延时清理（等待AstrBot完成文件上传）
                await asyncio.sleep(20)
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            else:
                yield plain_result(f"❌ 【{song_title} - {song_author}】文件下载失败（可能超出大小限制）")
        except Exception as e:
            logger.error(f"播放逻辑出错: {e}", exc_info=True)
            yield plain_result(f"❌ 音频处理失败，请稍后重试")

    # --- 点歌指令（AstrBot标准指令注册，支持参数解析）---
    @filter.command("点歌", help="点歌 歌曲名 - 搜索并点播歌曲", aliases=["搜歌"])
    async def search_song(self, event: AstrMessageEvent):
        # 适配AstrBot消息解析逻辑，提取指令后参数
        msg = event.get_message_str().strip()
        song_name = msg[2:].strip() if msg.startswith(("点歌", "搜歌")) else msg
        if not song_name:
            yield plain_result("💡 请输入歌曲名，例：点歌 七里香")
            return

        # 获取配置的默认音源
        source = self._get_config("default_source", "netease")
        source_name = {
            "netease": "网易云",
            "tencent": "QQ音乐",
            "kugou": "酷狗",
            "kuwo": "酷我"
        }.get(source, source)
        
        yield plain_result(f"🔍 正在{source_name}搜索: {song_name}")
        results = await self._fetch_api("search", song_name, source)
        
        if not results or not isinstance(results, list):
            yield plain_result(f"❌ 未找到【{song_name}】相关歌曲")
            return

        # 直接播放/手动选择模式
        mode = self._get_config("selection_mode", "manual")
        if mode == "direct":
            async for res in self._play_song_logic(event, results[0]):
                yield res
        else:
            # 存储会话（使用AstrBot标准session_id）
            session_id = event.unified_msg_origin
            self._sessions[session_id] = {
                "results": results,
                "timestamp": time.time()
            }
            # 构造搜索结果列表
            resp = f"🔍 搜索结果（{source_name}，有效时间{SESSION_EXPIRY}s）：\n"
            max_show = self._get_config("search_result_count", 10)
            for i, s in enumerate(results[:max_show]):
                title = s.get("title") or s.get("name") or "未知"
                author = s.get("author") or s.get("artist") or "未知"
                resp += f"{i+1}. {title} - {author}\n"
            resp += "💡 直接输入数字序号点播歌曲"
            yield plain_result(resp.strip())

    # --- 序号选择处理（AstrBot标准事件监听）---
    @filter.on_event(priority=99)  # 低优先级，避免拦截其他指令
    async def handle_selection(self, event: AstrMessageEvent):
        msg = event.get_message_str().strip()
        # 仅处理纯数字消息
        if not msg.isdigit():
            return
        # 获取会话ID
        session_id = event.unified_msg_origin
        if session_id not in self._sessions:
            return

        # 校验会话是否过期
        session_data = self._sessions[session_id]
        if time.time() - session_data["timestamp"] > SESSION_EXPIRY:
            del self._sessions[session_id]
            yield plain_result("⌛ 搜索会话已过期，请重新点歌")
            return

        # 解析序号并播放
        idx = int(msg) - 1
        results = session_data["results"]
        if 0 <= idx < len(results):
            del self._sessions[session_id]  # 播放后销毁会话
            async for res in self._play_song_logic(event, results[idx]):
                yield res

    # --- 自动解析歌曲URL（AstrBot标准事件监听）---
    @filter.on_event(priority=98)
    async def handle_url_parse(self, event: AstrMessageEvent):
        # 开关控制
        if not self._get_config("auto_parse_url", True):
            return
        msg = event.get_message_str().strip()
        # 四大平台URL正则（适配主流链接格式）
        patterns = {
            "netease": r"music\.163\.com/.*?song\?id=(\d+)",
            "tencent": r"(y\.qq\.com|i\.y\.qq\.com)/.*?songDetail/([a-zA-Z0-9]+)|y\.qq\.com.*?songid=(\d+)",
            "kugou": r"kugou\.com/.*?hash=([a-zA-Z0-9]+)|t\d+\.kugou\.com/.*?id=([a-zA-Z0-9]+)",
            "kuwo": r"kuwo\.cn/.*?play_detail/(\d+)"
        }
        for source, pattern in patterns.items():
            match = re.search(pattern, msg, re.IGNORECASE)
            if match:
                # 提取非空的歌曲ID
                song_id = [g for g in match.groups() if g][0]
                yield plain_result(f"🔗 检测到{source_name(source)}歌曲链接，解析中...")
                # 获取歌曲详情
                song_info = await self._fetch_api("song", song_id, source)
                if song_info and isinstance(song_info, list) and song_info:
                    async for res in self._play_song_logic(event, song_info[0]):
                        yield res
                else:
                    yield plain_result(f"❌ 歌曲链接解析失败，无法获取歌曲信息")
                return  # 匹配一个平台后立即返回，避免重复解析

    # --- 辅助方法：音源名转换 ---
    def source_name(self, source: str) -> str:
        return {
            "netease": "网易云",
            "tencent": "QQ音乐",
            "kugou": "酷狗",
            "kuwo": "酷我"
        }.get(source, "第三方")

    # --- 临时文件清理 ---
    def _cleanup_temp_files(self):
        """强制清理插件产生的临时文件"""
        try:
            temp_dir = tempfile.gettempdir()
            count = 0
            for f in os.listdir(temp_dir):
                if f.startswith(TEMP_FILE_PREFIX):
                    try:
                        os.remove(os.path.join(temp_dir, f))
                        count += 1
                    except:
                        pass
            if count > 0:
                logger.debug(f"清理了{count}个过期临时音频文件")
        except Exception as e:
            logger.error(f"清理临时文件失败: {e}", exc_info=True)

    # --- AstrBot插件标准销毁方法 ---
    async def terminate(self):
        """插件被销毁时，释放所有资源（AstrBot框架调用）"""
        # 取消后台任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except:
                pass
        # 关闭HTTP会话
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        # 清空内存
        self._sessions.clear()
        # 清理临时文件
        self._cleanup_temp_files()
        logger.info("Meting点歌插件已释放所有资源，完成销毁")
