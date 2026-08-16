# -*- coding: utf-8 -*-
"""艾珀莉亚的Pixiv图片小助手

通过 pixiv.re 镜像图床与 pixivision 官方杂志获取 Pixiv 插画：
  1. 定时向白名单群聊推送随机热门插画（可配置数量/间隔/R-18/AIGC 等）；
  2. /pixiv 作品ID —— 获取指定插画（支持多页）；
  3. /pixiv 特辑 —— 获取 pixivision 最新主题特辑插画。

作者: 艾珀莉亚 (FionaFaust)
"""

import asyncio
import random
import re
import tempfile
import time
from pathlib import Path

import httpx

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PIXIV_RE = "https://pixiv.re"
PIXIVISION = "https://www.pixivision.net/zh"
MAX_PUSH_COUNT = 25


class PixivHelperPlugin(Star):
    """艾珀莉亚的Pixiv图片小助手"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._pool: list = []          # 热门作品 ID 池
        self._last_pool_time = 0.0     # 池更新时间
        self._bot_id = "10001"         # 机器人自身 ID（合并转发用）
        self._platform_id = ""         # 平台实例 ID（如 default），用于构造 umo
        self._group_umos: dict = {}    # 群ID -> 真实 unified_msg_origin 缓存
        self._downloaded: list = []    # 已下载缓存文件列表 [(路径, 时间)]
        self._tasks: list = []         # 后台任务引用（terminate 时取消）
        # 启动定时推送
        if self.config.get("enable_push", True):
            self._tasks.append(asyncio.create_task(self._push_loop()))
        # 启动抽选池定时刷新（每天 0 点与 12 点 UTC+8）
        self._tasks.append(asyncio.create_task(self._pool_refresh_loop()))
        # 启动缓存清理
        if self.config.get("enable_cache_cleanup", True):
            self._tasks.append(asyncio.create_task(self._cleanup_cache_loop()))

    # ==================== 网络工具 ====================

    async def _http_get(self, url: str, binary: bool = False, timeout: float = 25.0):
        """GET 请求，返回文本或二进制"""
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False) as c:
                r = await c.get(url, headers={"User-Agent": UA})
                if r.status_code == 200:
                    return r.content if binary else r.text
        except Exception as e:
            logger.error(f"请求失败 {url}: {e}")
        return None

    # ==================== Pixivision 特辑 ====================

    async def fetch_special_ids(self, article_limit: int = 40) -> list:
        """获取 pixivision 特辑中的作品 ID 列表（默认爬取 40 个特辑，目标 300+ 作品）"""
        ids = []
        try:
            html = await self._http_get(f"{PIXIVISION}/")
            if not html:
                return ids
            # 提取特辑链接
            articles = re.findall(r'href="(/zh/a/(\d+))"', html)
            seen = set()
            for _, aid in articles[:article_limit]:
                if aid in seen:
                    continue
                seen.add(aid)
                ids.extend(await self._parse_article_ids(aid))
                # 已足够则提前结束
                if len(ids) >= 300:
                    break
        except Exception as e:
            logger.error(f"获取特辑列表失败: {e}")
        return sorted(set(ids), key=int)

    async def _parse_article_ids(self, article_id: str) -> list:
        """解析特辑页面中的作品 ID"""
        try:
            html = await self._http_get(f"{PIXIVISION}/a/{article_id}")
            if not html:
                return []
            ids = re.findall(r"artworks/(\d+)", html)
            ids += re.findall(r'"illustId"\s*:\s*"(\d+)"', html)
            return sorted(set(ids), key=int)
        except Exception as e:
            logger.error(f"解析特辑 {article_id} 失败: {e}")
            return []

    async def _refresh_pool(self, force: bool = False):
        """刷新热门作品池（pixivision 特辑作品，目标 300+）"""
        # 30 分钟内不重复刷新（除非强制）
        if not force and self._pool and (time.time() - self._last_pool_time) < 1800:
            return
        try:
            ids = await self.fetch_special_ids()
            if ids:
                self._pool = list(ids)
                self._last_pool_time = time.time()
                logger.info(f"作品池已刷新: {len(self._pool)} 个作品")
        except Exception as e:
            logger.error(f"刷新作品池失败: {e}")

    async def _pool_refresh_loop(self):
        """每天 0 点与 12 点（UTC+8）清理抽选池并重新爬取"""
        while True:
            try:
                from datetime import datetime, timedelta, timezone
                now = datetime.now(timezone.utc) + timedelta(hours=8)  # UTC+8
                target_times = [0, 12]
                # 找到下一个目标时间（UTC+8 的 0 点或 12 点）
                next_run = None
                for h in target_times:
                    cand = now.replace(hour=h, minute=0, second=0, microsecond=0)
                    if cand <= now:
                        cand = cand + timedelta(days=1)
                    if next_run is None or cand < next_run:
                        next_run = cand
                # 转回 UTC 计算等待秒数
                now_utc = datetime.now(timezone.utc)
                delay = (next_run - now_utc).total_seconds()
                await asyncio.sleep(max(1, delay))
                # 清理旧池并强制重新爬取
                self._pool = []
                self._last_pool_time = 0
                await self._refresh_pool(force=True)
                logger.info(f"[UTC+8 {next_run.strftime('%H:%M')}] 抽选池已清理并重新爬取")
            except Exception as e:
                logger.error(f"抽选池定时刷新异常: {e}")
                await asyncio.sleep(3600)

    # ==================== 图片下载 ====================

    async def _download_image(self, illust_id: str, page: int = 1, quality: str = "jpg") -> str | None:
        """下载 Pixiv 图片到本地临时文件，返回路径"""
        try:
            page_suffix = "" if page <= 1 else f"-{page}"
            url = f"{PIXIV_RE}/{illust_id}{page_suffix}.{quality}"
            data = await self._http_get(url, binary=True)
            if not data:
                return None
            fd = tempfile.NamedTemporaryFile(suffix=f".{quality}", delete=False, prefix=f"pixiv_{illust_id}_")
            fd.write(data)
            fd.close()
            self._downloaded.append((fd.name, time.time()))
            return fd.name
        except Exception as e:
            logger.error(f"下载图片失败 {illust_id} p{page}: {e}")
            return None

    # ==================== 缓存清理 ====================

    async def _cleanup_cache_loop(self):
        """定时清理下载的临时图片缓存"""
        while True:
            try:
                interval = max(60, int(self.config.get("cache_cleanup_interval", 3600) or 3600))
                await asyncio.sleep(interval)
                self._cleanup_cache()
            except Exception as e:
                logger.error(f"缓存清理任务异常: {e}")
                await asyncio.sleep(3600)

    def _cleanup_cache(self):
        """删除已下载的临时图片文件"""
        try:
            removed = 0
            for path, _ in self._downloaded:
                try:
                    if Path(path).exists():
                        Path(path).unlink()
                        removed += 1
                except Exception:
                    pass
            self._downloaded = []
            if removed:
                logger.info(f"图片缓存已清理: {removed} 个文件")
        except Exception as e:
            logger.error(f"清理图片缓存失败: {e}")

    # ==================== 发送工具 ====================

    def _whitelist(self) -> list:
        return [str(g).strip() for g in (self.config.get("whitelist_groups", []) or [])]

    def _get_count(self) -> int:
        try:
            n = int(self.config.get("push_count", 15) or 15)
        except Exception:
            n = 15
        return max(1, min(MAX_PUSH_COUNT, n))

    def _max_forward_nodes(self) -> int:
        """合并转发最大节点数（超过则普通发送，避免 NapCat retcode 1200）"""
        try:
            n = int(self.config.get("forward_max_nodes", 5) or 5)
        except Exception:
            n = 5
        return max(1, n)

    async def _send_forward(self, event: AstrMessageEvent, items: list):
        """发送图片（群聊用合并转发主动发送并捕获失败回退；私聊普通发送）"""
        # 缓存机器人自身 ID
        try:
            sid = event.get_self_id()
            if sid:
                self._bot_id = str(sid)
        except Exception:
            pass
        # 仅群聊支持合并转发
        is_group = False
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            pass
        use_fwd = self.config.get("use_forward", True) and is_group
        # 节点数超过上限时改用普通发送（避免 NapCat 限制）
        if use_fwd and len(items) > self._max_forward_nodes():
            use_fwd = False

        if not use_fwd:
            # 私聊/关闭合并转发：普通逐条发送
            for it in items:
                if self.config.get("show_info", True):
                    yield event.plain_result(f"Pixiv ID: {it.get('id', '')}\n标题: {it.get('title', '')}\n作者: {it.get('author', '')}")
                if it.get("image") and Path(it["image"]).exists():
                    yield event.image_result(it["image"])
            return

        # 群聊：主动发送合并转发（捕获 NapCat 失败并回退）
        adapter = self._get_adapter()
        if not adapter:
            for it in items:
                if self.config.get("show_info", True):
                    yield event.plain_result(f"Pixiv ID: {it.get('id', '')}\n标题: {it.get('title', '')}\n作者: {it.get('author', '')}")
                if it.get("image") and Path(it["image"]).exists():
                    yield event.image_result(it["image"])
            return
        bot_uin = str(self.config.get("bot_qq", "") or "").strip() or str(getattr(adapter, "client_self_id", "") or "10000")
        try:
            nodes = []
            for it in items:
                content = []
                if self.config.get("show_info", True):
                    info = f"Pixiv ID: {it.get('id', '')}"
                    if it.get("title"):
                        info += f"\n标题: {it['title']}"
                    if it.get("author"):
                        info += f"\n作者: {it['author']}"
                    content.append(Comp.Plain(info))
                if it.get("image") and Path(it["image"]).exists():
                    content.append(Comp.Image(file=it["image"]))
                nodes.append(Comp.Node(uin=bot_uin, name="Pixiv 小助手", content=content))
            await AiocqhttpMessageEvent.send_message(
                bot=adapter.bot,
                message_chain=MessageChain([Comp.Nodes(nodes)]),
                is_group=True,
                session_id=str(event.get_group_id()),
            )
            return  # 合并转发已主动发送成功
        except Exception as e:
            logger.error(f"合并转发发送失败，回退普通发送: {e}")
        # 回退普通发送
        for it in items:
            if self.config.get("show_info", True):
                yield event.plain_result(f"Pixiv ID: {it.get('id', '')}\n标题: {it.get('title', '')}\n作者: {it.get('author', '')}")
            if it.get("image") and Path(it["image"]).exists():
                yield event.image_result(it["image"])

    def _get_adapter(self) -> AiocqhttpAdapter | None:
        """获取 aiocqhttp 适配器实例"""
        try:
            for p in self.context.platform_manager.get_insts():
                if isinstance(p, AiocqhttpAdapter):
                    return p
        except Exception as e:
            logger.error(f"获取 aiocqhttp 适配器失败: {e}")
        return None

    async def _send_forward_to_group(self, group_id: str, items: list):
        """向指定群发送合并转发（AIpaper 方式：直接走 adapter.bot，最可靠）"""
        adapter = self._get_adapter()
        if not adapter:
            logger.error("未找到 aiocqhttp 适配器，无法推送")
            return
        # 机器人 uin：优先配置 bot_qq，其次 platform client_self_id
        bot_uin = str(self.config.get("bot_qq", "") or "").strip()
        if not bot_uin:
            bot_uin = str(getattr(adapter, "client_self_id", "") or "10000")

        def build_content(it):
            content = []
            if self.config.get("show_info", True):
                info = f"Pixiv ID: {it.get('id', '')}"
                if it.get("title"):
                    info += f"\n标题: {it['title']}"
                if it.get("author"):
                    info += f"\n作者: {it['author']}"
                content.append(Comp.Plain(info))
            if it.get("image") and Path(it["image"]).exists():
                content.append(Comp.Image(file=it["image"]))
            return content

        if self.config.get("push_use_forward", True):
            # 节点数超过上限时改用普通发送（避免 NapCat 限制）
            if len(items) > self._max_forward_nodes():
                pass
            else:
                try:
                    nodes = [
                        Comp.Node(uin=bot_uin, name="Pixiv 小助手", content=build_content(it))
                        for it in items
                    ]
                    forward = Comp.Nodes(nodes)
                    chain = MessageChain([forward])
                    await AiocqhttpMessageEvent.send_message(
                        bot=adapter.bot,
                        message_chain=chain,
                        is_group=True,
                        session_id=str(group_id),
                    )
                    return
                except Exception as e:
                    logger.error(f"合并转发失败，回退普通发送: {e}")

        # 普通逐条发送
        for it in items:
            try:
                chain = MessageChain(chain=build_content(it))
                await AiocqhttpMessageEvent.send_message(
                    bot=adapter.bot,
                    message_chain=chain,
                    is_group=True,
                    session_id=str(group_id),
                )
            except Exception as e:
                logger.error(f"普通发送失败: {e}")

    # ==================== 定时推送 ====================

    async def _push_loop(self):
        while True:
            try:
                await self._refresh_pool()
                interval = max(60, int(self.config.get("push_interval", 3600) or 3600))
                await asyncio.sleep(interval)
                await self._push_once()
            except Exception as e:
                logger.error(f"定时推送任务异常: {e}")
                await asyncio.sleep(60)

    def _get_real_platform_id(self) -> str:
        """从平台管理器获取 aiocqhttp 适配器的真实 platform_id（如 Kite003-Ver2.0.1）"""
        try:
            for p in self.context.platform_manager.platform_insts:
                meta = p.meta()
                if meta and meta.name == "aiocqhttp":
                    return meta.id or ""
        except Exception as e:
            logger.error(f"获取平台 ID 失败: {e}")
        # 兜底：缓存值
        return self._platform_id or ""

    async def _ensure_bot_id(self) -> str:
        """确保机器人自身 QQ 号正确：优先使用配置项 bot_qq，其次运行时获取"""
        # 1) 配置项（最可靠）
        cfg_qq = str(self.config.get("bot_qq", "") or "").strip()
        if cfg_qq:
            self._bot_id = cfg_qq
            return cfg_qq
        # 2) 已缓存的有效值
        if self._bot_id and self._bot_id != "10001":
            return self._bot_id
        # 3) 从平台获取
        try:
            for p in self.context.platform_manager.platform_insts:
                meta = p.meta()
                if meta and meta.name == "aiocqhttp":
                    client = p.get_client()
                    info = await client.api.call_action("get_login_info")
                    uid = str(info.get("user_id") or "")
                    if uid:
                        self._bot_id = uid
                        return uid
        except Exception as e:
            logger.error(f"获取机器人 QQ 号失败: {e}")
        return self._bot_id or "10001"

    async def _push_once(self):
        """执行一次推送"""
        await self._refresh_pool()
        whitelist = self._whitelist()
        if not whitelist:
            logger.warning("推送白名单为空，跳过本次推送")
            return
        if not self._pool:
            logger.warning("作品池为空，跳过本次推送")
            return
        count = self._get_count()
        selected = random.sample(self._pool, min(count, len(self._pool)))
        # 下载图片
        items = []
        for pid in selected:
            path = await self._download_image(pid)
            if path:
                items.append({"id": pid, "title": f"作品 {pid}", "author": "Pixiv", "image": path})
            if len(items) >= count:
                break
        if not items:
            return
        for gid in whitelist:
            try:
                await self._send_forward_to_group(gid, items)
            except Exception as e:
                logger.error(f"推送到群 {gid} 失败: {e}")
        logger.info(f"已向 {len(whitelist)} 个群推送 {len(items)} 张 Pixiv 图片")

    # ==================== 指令 ====================

    @filter.command("pixiv")
    async def pixiv(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """Pixiv 图片查询

        用法:
          /pixiv 帮助 —— 帮助
          /pixiv 作品ID [页码] —— 获取指定插画（支持多页）
          /pixiv 特辑 —— 获取 pixivision 最新主题特辑插画
        """
        a1 = arg1.strip()
        # 缓存机器人 ID 与群真实 umo（供定时推送使用）
        try:
            sid = event.get_self_id()
            if sid:
                self._bot_id = str(sid)
        except Exception:
            pass
        try:
            gid = event.get_group_id()
            if gid:
                self._group_umos[str(gid)] = event.unified_msg_origin
                # 缓存真实平台 ID（用于构造正确的 umo）
                try:
                    self._platform_id = event.get_platform_id() or ""
                except Exception:
                    pass
                logger.info(f"[诊断] 群 {gid} 真实 umo = {event.unified_msg_origin}, platform_id = {self._platform_id}")
        except Exception:
            pass
        if not a1 or a1 == "帮助":
            yield event.plain_result(
                "🖼️ Pixiv 图片小助手\n"
                "用法:\n"
                "  /pixiv 作品ID [页码] —— 获取指定插画（支持多页）\n"
                "  /pixiv 特辑 —— 获取 pixivision 最新主题特辑插画\n"
                "定时推送: 按配置自动向白名单群发送热门 Pixiv 图片"
            )
            return

        if a1 == "特辑":
            async for r in self._cmd_special(event):
                yield r
            return

        # 作品 ID（不带页码自动获取全部页，带页码只获取指定页）
        if a1.isdigit():
            page = 0
            if arg2.strip().isdigit():
                page = int(arg2.strip())
            async for r in self._cmd_illust(event, a1, page):
                yield r
            return

        yield event.plain_result(f"❌ 无法识别: {a1}\n发送 /pixiv 帮助 查看用法")

    async def _cmd_illust(self, event: AstrMessageEvent, illust_id: str, page: int = 0):
        """按作品 ID 获取插画：/pixiv {id} {num} 指定页；{num} 留空自动预览前 3 页"""
        if page > 0:
            # 指定单页
            yield event.plain_result(f"🖼️ 正在获取 Pixiv 作品 {illust_id} 第 {page} 页...")
            path = await self._download_image(illust_id, page)
            if not path:
                yield event.plain_result(f"❌ 无法获取作品 {illust_id} 第 {page} 页（可能不存在或已被删除）")
                return
            items = [{"id": illust_id, "title": f"第 {page} 页", "author": "Pixiv", "image": path}]
            async for r in self._send_forward(event, items):
                yield r
            return

        # 不带页码：自动预览前 3 页
        yield event.plain_result(f"🖼️ 正在预览作品 {illust_id} 的前 3 页...")
        items = []
        for p in range(1, 4):
            path = await self._download_image(illust_id, p)
            if not path:
                break
            items.append({"id": illust_id, "title": f"第 {p} 页", "author": "Pixiv", "image": path})
        if not items:
            yield event.plain_result(f"❌ 无法获取作品 {illust_id}（可能不存在或已被删除）")
            return
        async for r in self._send_forward(event, items):
            yield r

    async def _cmd_special(self, event: AstrMessageEvent):
        """获取 pixivision 最新主题特辑插画"""
        yield event.plain_result("🖼️ 正在获取 pixivision 最新特辑...")
        ids = await self.fetch_special_ids()
        if not ids:
            yield event.plain_result("❌ 获取特辑失败（网络或解析错误）")
            return
        count = self._get_count()
        selected = random.sample(ids, min(count, len(ids)))
        items = []
        for pid in selected:
            path = await self._download_image(pid)
            if path:
                items.append({"id": pid, "title": "特辑作品", "author": "Pixivision", "image": path})
            if len(items) >= min(count, 10):  # 特辑最多 10 张避免过长
                break
        if not items:
            yield event.plain_result("❌ 特辑图片下载失败")
            return
        async for r in self._send_forward(event, items):
            yield r

    @filter.command("rm")
    async def rm(self, event: AstrMessageEvent):
        """清除抽选池并重新获取一次"""
        yield event.plain_result("🧹 正在清除抽选池并重新获取...")
        self._pool = []
        self._last_pool_time = 0
        await self._refresh_pool(force=True)
        if self._pool:
            yield event.plain_result(f"✅ 抽选池已更新: {len(self._pool)} 个作品")
        else:
            yield event.plain_result("❌ 重新获取失败，抽选池为空")

    @filter.command("今日涩图")
    async def today_hentai(self, event: AstrMessageEvent):
        """今日涩图：Lolicon API 获取 3 张 R18 图（regular 规格，降低发送超时）"""
        yield event.plain_result("🔞 正在获取今日涩图...")
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as c:
                resp = await c.post(
                    "https://api.lolicon.app/setu/v2",
                    json={"r18": 1, "num": 3, "size": ["regular"]},
                    headers={"User-Agent": UA},
                )
                data = resp.json()
                results = data.get("data", []) if data else []
        except Exception as e:
            logger.error(f"Lolicon 请求失败: {e}")
            yield event.plain_result("❌ 获取失败，请检查日志")
            return

        if not results:
            yield event.plain_result("❌ 没有获取到涩图")
            return

        items = []
        for item in results:
            urls = item.get("urls") or {}
            # 优先 regular（较小），回退 original
            url = urls.get("regular") or urls.get("original", "")
            if not url:
                continue
            try:
                async with httpx.AsyncClient(timeout=30, verify=False) as c:
                    img = await c.get(url, headers={"User-Agent": UA})
                    if img.status_code == 200 and img.content:
                        fd = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, prefix="lolicon_")
                        fd.write(img.content)
                        fd.close()
                        self._downloaded.append((fd.name, time.time()))
                        items.append({
                            "id": str(item.get("pid", "")),
                            "title": item.get("title", ""),
                            "author": item.get("author", ""),
                            "image": fd.name,
                        })
            except Exception as e:
                logger.error(f"下载涩图失败: {e}")

        if not items:
            yield event.plain_result("❌ 图片下载失败")
            return

        yield event.plain_result(f"🔞 今日涩图 {len(items)} 张，正在发送...")
        async for r in self._send_forward(event, items):
            yield r

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件被卸载/停用时取消后台任务，防止旧任务残留"""
        for t in self._tasks:
            try:
                t.cancel()
            except Exception:
                pass
        self._tasks = []
        logger.info("艾珀莉亚的Pixiv图片小助手已卸载")
