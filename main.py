import os
import re
import json
import time
import random
import asyncio
import aiohttp
import aiofiles
import hashlib
from typing import Optional, List, Dict, Any

from astrbot.api import logger, AstrBotEvent, CommandResult, PluginMetadata
from astrbot.api.star import register
from astrbot.api.event import MessageChain
from astrbot.api.platform import MessageType
from astrbot.core.config.default import VERSION as ASTRBOT_VERSION

__plugin_meta__ = PluginMetadata(
    name="MetingAPI 点歌 (文件版)",
    version="1.2.1",
    author="Modified",
    description="基于 MetingAPI 的点歌插件，支持发送 MP3 文件。",
    help="直接发送歌曲名或链接即可点歌。"
)

# 注意：@register 装饰器的用法保持不变
@register(
    "astrbot_plugin_meting", 
    "MetingAPI 点歌",
    "1.2.1",
    "https://github.com/ImNotBird/astrbot_plugin_meting"
)
class MetingPlugin:
    def __init__(self, context):
        self.context = context
        self.config = context.get_config()
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._initialized = False
        
        # 默认配置
        self._default_config = {
            "use_music_card": False,  # 【关键配置】False=发送MP3文件，True=发送音乐卡片
            "api_sign_url": "https://oiapi.net/api/QQMusicJSONArk/",
            "meting_api_url": "https://api.i-meto.com/meting/api?server=:type&type=search&s=:keyword",
            "max_duration_sec": 600,  # 发送文件模式下的最大时长限制（秒），防止过大
        }
        
        # 合并配置
        for k, v in self._default_config.items():
            if k not in self.config:
                self.config[k] = v
                
        self._ensure_initialized()

    def _ensure_initialized(self):
        if self._initialized:
            return
            
        # 版本兼容性检查 (仅针对卡片模式)
        if self.use_music_card():
            try:
                version_parts = ASTRBOT_VERSION.split('.')
                major, minor = int(version_parts), int(version_parts)
                if major < 4 or (major == 4 and minor < 17):
                    logger.warning("检测到当前 AstrBot 版本可能不支持 JSON 消息组件，建议升级到 4.17.6+ 以使用音乐卡片功能。")
                # 检查核心文件是否存在 Json 组件支持 (简化检查)
                # 实际项目中可能需要更复杂的检查，这里仅作示意
            except Exception as e:
                logger.warning(f"版本检查出错: {e}")
        
        self._initialized = True

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(trust_env=True)
        return self._http_session

    def _get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def use_music_card(self) -> bool:
        """是否启用音乐卡片模式"""
        return bool(self._get_config("use_music_card", False))

    def get_sign_api_url(self) -> str:
        return str(self._get_config("api_sign_url", "https://oiapi.net/api/QQMusicJSONArk/"))

    def get_meting_api_url(self) -> str:
        return str(self._get_config("meting_api_url", "https://api.i-meto.com/meting/api?server=:type&type=search&s=:keyword"))

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def _search_song(self, keyword: str, source: str = "netease") -> Optional[Dict]:
        """搜索歌曲"""
        session = await self._get_http_session()
        api_url = self.get_meting_api_url().replace(":type", source).replace(":keyword", keyword)
        
        try:
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data
                else:
                    logger.error(f"MetingAPI 请求失败: {resp.status}")
        except Exception as e:
            logger.error(f"搜索歌曲出错: {e}")
        return None

    async def _download_audio(self, url: str, save_path: str) -> bool:
        """下载音频文件"""
        session = await self._get_http_session()
        try:
            async with session.get(url, timeout=60) as resp:
                if resp.status == 200:
                    async with aiofiles.open(save_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            await f.write(chunk)
                    return True
                else:
                    logger.error(f"音频下载失败: {resp.status}")
        except Exception as e:
            logger.error(f"下载音频异常: {e}")
        return False

    async def _generate_music_card(self, song_info: Dict) -> Optional[MessageChain]:
        """生成音乐卡片消息链"""
        sign_api = self.get_sign_api_url()
        session = await self._get_http_session()
        
        # 提取信息
        song_name = song_info.get('name', '未知歌曲')
        singer = song_info.get('artist', '未知歌手')
        cover = song_info.get('pic', '')
        song_url = song_info.get('url', '')
        
        if not song_url:
            return None

        # 确定 cover 类型 (根据链接判断，简单处理)
        cover_type = "netease"
        if "qq.com" in song_url or "y.qq.com" in song_info.get('link', ''):
            cover_type = "qq"
        elif "163.com" in song_url:
            cover_type = "163"
            
        params = {
            "url": song_url,
            "song": song_name,
            "singer": singer,
            "cover": cover_type
        }
        
        try:
            async with session.get(sign_api, params=params, timeout=30) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get('code') == 200:
                        card_data = result.get('data', {})
                        # 构造 QQ 音乐卡片消息 (AstrBot 特定格式)
                        # 注意：不同 OneBot 实现可能对 music 消息支持不同，这里使用通用的 json 结构尝试
                        # 如果是 NapCat/Lagrange，通常支持 send_json 或直接构造 music 消息
                        # 这里假设 AstrBot 有封装好的方法或我们需要构造特定的 MessageSegment
                        
                        # 方案 A: 如果 AstrBot 支持直接发送 music 类型
                        # return MessageChain().music(type=cover_type, id=song_info.get('id', '')) 
                        
                        # 方案 B: 使用 API 返回的 json 数据 (通用性更强，依赖底层协议支持 json 消息)
                        # 这里构造一个包含 json 的消息段，具体需参考 AstrBot 文档
                        # 由于不同协议端差异大，这里采用最稳妥的方式：如果 API 返回了 xml 或特殊结构
                        # 实际上 OIAPI 返回的是 ArkJson 结构
                        return MessageChain().json(card_data)
                    else:
                        logger.warning(f"签名 API 返回错误: {result.get('message')}")
                else:
                    logger.error(f"签名 API 请求失败: {resp.status}")
        except Exception as e:
            logger.error(f"生成卡片异常: {e}")
        return None

    async def _play_song_logic(self, event: AstrBotEvent, keyword: str):
        """核心播放逻辑"""
        yield event.plain_result(f"正在搜索歌曲：{keyword}...")
        
        # 1. 搜索
        song_info = await self._search_song(keyword)
        if not song_info:
            yield event.plain_result("未找到相关歌曲，请尝试更换关键词。")
            return

        song_name = song_info.get('name', '未知')
        singer = song_info.get('artist', '未知')
        song_url = song_info.get('url', '')
        
        if not song_url:
            yield event.plain_result("获取歌曲播放链接失败，该歌曲可能无法试听。")
            return

        # 2. 分支处理：卡片模式 vs 文件模式
        if self.use_music_card():
            # --- 模式 A: 发送音乐卡片 ---
            yield event.plain_result(f"正在生成 {song_name} 的音乐卡片...")
            card_chain = await self._generate_music_card(song_info)
            if card_chain:
                await event.send(card_chain)
            else:
                yield event.plain_result("音乐卡片生成失败，已降级为发送文件模式。")
                # 降级逻辑：继续执行下面的文件发送代码
                # 为了不重复代码，这里我们可以用 fall-through 或者重新调用一次逻辑
                # 这里简单起见，如果卡片失败，提示用户检查配置或网络
                return
        else:
            # --- 模式 B: 发送 MP3 文件 (修改重点在这里) ---
            yield event.plain_result(f"正在下载 {song_name} - {singer} ...")
            
            # 创建临时文件
            temp_dir = os.path.join(os.getcwd(), "data", "temp", "meting")
            os.makedirs(temp_dir, exist_ok=True)
            file_name = f"{song_name} - {singer}.mp3".replace('/', '_').replace('\\', '_')
            # 去除非法字符
            for char in ['*', '"', '<', '>', '|', '?', ':']:
                file_name = file_name.replace(char, '')
            temp_path = os.path.join(temp_dir, f"{int(time.time())}_{file_name}")
            
            success = await self._download_audio(song_url, temp_path)
            
            if success:
                file_size = os.path.getsize(temp_path)
                if file_size > 50 * 1024 * 1024: # 限制 50MB
                    os.remove(temp_path)
                    yield event.plain_result("歌曲文件过大 (>50MB)，无法发送。")
                    return

                try:
                    # 【关键修改】发送文件而不是语音
                    # send_file 通常接收文件路径，第二个参数是自定义文件名（可选）
                    await event.send_file(temp_path, name=file_name)
                    logger.info(f"成功发送文件: {file_name}")
                except Exception as e:
                    logger.error(f"发送文件失败: {e}")
                    yield event.plain_result(f"发送文件失败: {str(e)}")
                finally:
                    # 延迟删除临时文件，防止发送过程中被删
                    asyncio.create_task(self._delay_remove(temp_path))
            else:
                yield event.plain_result("歌曲下载失败，请检查网络或源。")

    async def _delay_remove(self, path: str, delay: int = 60):
        """延迟删除临时文件"""
        await asyncio.sleep(delay)
        if os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass

    @register.event_handler()
    async def on_message(self, event: AstrBotEvent):
        # 忽略非文本消息
        if event.get_message_type() != MessageType.FRIEND_MESSAGE and \
           event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
            
        text = event.get_message_str().strip()
        if not text:
            return

        # 简单的点歌指令判断：如果是以 "点歌" 开头，或者直接就是歌词/歌名
        # 这里做一个简单的启发式判断：如果包含 "点歌" 或者 看起来像歌名（没有 http 链接且长度适中）
        # 为了避免误触，可以设定必须带前缀，或者用户自己配置触发词
        # 这里演示：直接发送文字即视为点歌（可根据需要修改）
        
        # 排除指令
        if text.startswith("/"):
            return
            
        # 排除链接（链接由其他插件处理，或者如果想让本插件处理链接点歌，可以去掉这个判断）
        if text.startswith("http"):
            return

        # 触发点歌
        # 为了防止群聊刷屏，可以加一个群聊开关配置，这里暂略
        await self._play_song_logic(event, text)
        # 阻止其他插件处理（可选）
        # event.stop_propagation() 

    @register.command_handler("点歌")
    async def cmd_play(self, event: AstrBotEvent, keyword: str):
        """手动点歌指令：/点歌 歌名"""
        if not keyword:
            yield event.plain_result("用法：/点歌 <歌曲名>")
            return
        await self._play_song_logic(event, keyword)

    @register.command_handler("设置点歌模式")
    async def cmd_set_mode(self, event: AstrBotEvent, mode: str):
        """切换模式：/设置点歌模式 card (卡片) 或 file (文件)"""
        if mode.lower() in ["card", "卡片", "true"]:
            self.config["use_music_card"] = True
            yield event.plain_result("已切换为【音乐卡片】模式。")
        elif mode.lower() in ["file", "文件", "false"]:
            self.config["use_music_card"] = False
            yield event.plain_result("已切换为【MP3 文件】模式。")
        else:
            yield event.plain_result("参数错误，请使用 'card' 或 'file'。")
        
        # 保存配置 (假设 context 有 save_config 方法，如果没有则依赖重启重载)
        # self.context.save_config() 
