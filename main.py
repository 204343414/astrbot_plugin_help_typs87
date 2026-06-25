import asyncio
import time
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Image, Plain, Json

from .domain import InternalCFG, TypstPluginConfig
from .utils import FontManager, HelpHint, MsgRecall, TypstLayout
from .core import CommandAnalyzer, EventAnalyzer, FilterAnalyzer, TypstRenderer

class HelpTypst(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.plugin_dir = Path(__file__).parent
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.template_path = self.plugin_dir / "templates" / InternalCFG.NAME_TEMPLATE
        self.schema_path = self.plugin_dir / "_conf_schema.json"

        self.config = config
        try:
            self.plugin_config = TypstPluginConfig.load(config)
        except Exception as e:
            logger.error(f"[HelpTypst] 配置加载失败: {e}", exc_info=True)
            from .domain.config import QzoneShareConfig, RenderingConfig, AppearanceConfig, ThemePreset
            self.plugin_config = TypstPluginConfig(
                enable_waiting_message=False,
                ignored_plugins=set(),
                custom_font_path="",
                rendering=RenderingConfig(10,30,2,1500,16383,16000,144),
                appearance=AppearanceConfig("default", {"default": ThemePreset("default", [], {})}),
                qzone_share=QzoneShareConfig(True,
                    "https://h5.qzone.qq.com/ugc/share/?res_uin=2562925383&cellid=4723c398fea8b26971e70500",
                    "Bot使用说明", "点击查看详细功能贴", "", "json")
            )

        raw_path = self.plugin_config.custom_font_path
        self.user_font_dir = Path(raw_path) if raw_path and raw_path.strip() else self.data_dir / "fonts"
        try:
            self.user_font_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        self.builtin_font_dir = self.plugin_dir / "resources" / InternalCFG.NAME_FONT_DIR
        self.font_dirs = [self.builtin_font_dir, self.user_font_dir]

        self.font_manager = FontManager(self.font_dirs)
        self.layout = TypstLayout(self.plugin_config)
        self.hint = HelpHint()
        self.msg = MsgRecall()

        self.renderer = TypstRenderer(
            star=self, data_dir=self.data_dir, template_path=self.template_path,
            font_dirs=self.font_dirs, config=self.plugin_config,
        )

        self.cmd_analyzer = CommandAnalyzer(context, self.plugin_config)
        self.evt_analyzer = EventAnalyzer(context, self.plugin_config)
        self.flt_analyzer = FilterAnalyzer(context, self.plugin_config)

        self.prefixes: list[str] = []
        self._last_send = {}

        self._plugin_id = "astrbot_plugin_help_typs87"
        try:
            import yaml
            meta = self.plugin_dir / "metadata.yaml"
            if meta.exists():
                with open(meta, "r", encoding="utf-8") as f:
                    d = yaml.safe_load(f)
                    self._plugin_id = d.get("name", self._plugin_id)
        except Exception:
            pass

        qz = getattr(self.plugin_config, "qzone_share", None)
        logger.info(f"[HelpTypst V5] id={self._plugin_id} qzone={getattr(qz,'enable',False)}/{getattr(qz,'mode','?')}")

    async def initialize(self):
        self._init_prefixes(self.context)
        await asyncio.to_thread(self._refresh_resources)
        logger.info(f"[HelpTypst] init prefixes={self.prefixes}")

    def _refresh_resources(self):
        try:
            self.font_manager.scan_fonts()
            self.font_manager.update_json_schema(self.schema_path)
            self.font_manager.prune_invalid_config_items(self.config)
        except Exception as e:
            logger.warning(f"资源重载失败: {e}")

    async def terminate(self):
        await self._perform_cleanup()
        try: self._refresh_resources()
        except Exception: pass

    async def _perform_cleanup(self):
        try:
            for f in self.data_dir.glob("temp_*"):
                try:
                    if f.exists(): f.unlink()
                except OSError: pass
        except Exception as e:
            logger.warning(f"清理失败: {e}")

    # ---------- 工具 ----------
    def _dedup(self, event: AstrMessageEvent, key: str, ttl: float = 1.8) -> bool:
        try:
            uid = f"{event.get_sender_id()}:{key}:{event.get_group_id() or 'p'}"
        except Exception:
            uid = key
        now = time.time()
        last = self._last_send.get(uid, 0)
        if now - last < ttl:
            logger.info(f"[HelpTypst] 去重 {uid} {now-last:.2f}s")
            return False
        self._last_send[uid] = now
        return True

    def _parse_qzone_url(self, url: str) -> dict:
        """从QQ空间分享链接提取 res_uin / cellid 等，用于 Ark 卡片更逼真"""
        try:
            p = urlparse(url)
            qs = parse_qs(p.query)
            def g(k, d=""):
                v = qs.get(k, [])
                return unquote(v[0]) if v else d
            return {
                "res_uin": g("res_uin"),
                "cellid": g("cellid"),
                "appid": g("appid", "311"),
                "g": g("g", "84"),
                "url": url
            }
        except Exception:
            return {"url": url}

    async def _send_qzone_card(self, event: AstrMessageEvent) -> bool:
        try:
            qz = self.plugin_config.qzone_share
        except Exception as e:
            logger.warning(f"qzone配置读取失败: {e}")
            return False
        if not getattr(qz, "enable", False):
            return False
        url = (getattr(qz, "url", "") or "").strip()
        if not url.startswith("http"):
            logger.warning(f"QZone url无效: {url}")
            return False

        mode = (getattr(qz, "mode", "json") or "json").lower()
        title = (getattr(qz, "title", "") or "Bot使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        image = (getattr(qz, "image", "") or "").strip()

        from astrbot.api.message_components import MessageChain

        info = self._parse_qzone_url(url)
        preview_img = image or "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png"

        # 1) JSON Ark - NapCat 支持 json 发
        if mode in ("json", "auto", "ark"):
            try:
                # 构造一个类似 QQ 空间说说的 Ark 卡片
                # 参考 NapCat OneBot json 段: {"type":"json","data":{"data":"{...}"}}
                card_obj = {
                    "app": "com.tencent.structmsg",
                    "config": {"autosize": True, "forward": True, "type": "normal"},
                    "desc": "QQ空间",
                    "extra": {"app_type": 1, "appid": int(info.get("appid", "311")) if str(info.get("appid", "311")).isdigit() else 8},
                    "meta": {
                        "news": {
                            "action": "",
                            "android_pkg_name": "",
                            "app_type": 1,
                            "appid": int(info.get("appid", "311")) if str(info.get("appid", "311")).isdigit() else 8,
                            "desc": content,
                            "jumpUrl": url,
                            "preview": preview_img,
                            "source_icon": "https://qzonestyle.gtimg.cn/qzone/vas/opensns/res/img/favicon.ico",
                            "source_url": url,
                            "tag": "QQ空间",
                            "title": title
                        }
                    },
                    "prompt": f"[分享] {title}",
                    "ver": "0.0.0.1",
                    "view": "news"
                }
                import json as js
                # AstrBot Json 组件: Json(data=dict)
                await event.send(MessageChain([Json(data=card_obj)]))
                logger.info("[HelpTypst] JSON Ark 卡片发送成功")
                return True
            except Exception as e:
                logger.warning(f"JSON Ark 失败: {e}", exc_info=True)

        # 2) Share - 理论上 NapCat 不支持，发了会 1200 错误，这里保留但默认不走
        if mode == "share":
            try:
                from astrbot.api.message_components import Share
                kw = {"url": url, "title": title}
                if content: kw["content"] = content
                if image: kw["image"] = image
                await event.send(MessageChain([Plain(""),]))  # 占位避免空
                # 用原始 OneBot 适配器直接发 share，避免 AstrBot 过滤？
                # 降级 text
                raise RuntimeError("share disabled by NapCat")
            except Exception as e:
                logger.warning(f"Share 失败: {e}")

        # 3) 纯文本 - 最稳
        try:
            txt = f"📖 {title}\n{content}\n{url}" if content else f"📖 {title}\n{url}"
            await event.send(MessageChain([Plain(txt)]))
            logger.info("[HelpTypst] Text URL 发送成功")
            return True
        except Exception as e:
            logger.error(f"Text 也失败: {e}")
            return False

    # ---------- 渲染 ----------
    async def _handle_request(self, event: AstrMessageEvent, analyzer, title: str, mode: str, query: str | None, *, with_qzone_share: bool = False):
        logger.info(f"[HelpTypst] handle {mode} q={query!r} qzone={with_qzone_share}")
        wait_msg_id = None
        if self.plugin_config.enable_waiting_message:
            hint_text = self.hint.msg_searching(query) if query else self.hint.msg_rendering(mode)
            wait_msg_id = await self.msg.send_wait(event, hint_text)

        def data_pipeline(save_path: Path) -> int:
            plugins = analyzer.get_plugins(query)
            if not plugins: return 0
            display_title = f'搜索结果: "{query}"' if query else title
            user_fonts = self.plugin_config.appearance.get_active_font_order()
            final_font_list = self.font_manager.get_render_font_list(user_fonts)
            self.layout.dump_layout_json(
                plugins=plugins, save_path=save_path, title=display_title,
                mode=mode, prefixes=self.prefixes, font_list=final_font_list,
            )
            return len(plugins)

        result, error = await self.renderer.render(data_pipeline, mode, query)
        if wait_msg_id:
            await self.msg.recall(event, wait_msg_id)

        if result:
            try:
                # QZone 先独立发
                if with_qzone_share:
                    try:
                        await self._send_qzone_card(event)
                        await asyncio.sleep(0.35)
                    except Exception as e:
                        logger.warning(f"QZone外层异常: {e}")
                # 图片必发
                images = [Image.fromFileSystem(p) for p in result.images]
                if images:
                    logger.info(f"[HelpTypst] 发送图片 {len(images)}")
                    yield event.chain_result(images)
            finally:
                if result.temp_files:
                    asyncio.create_task(self._cleanup_task(result.temp_files))
        else:
            if error == "empty":
                yield event.plain_result(self.hint.msg_empty_result(mode, query))
            else:
                yield event.plain_result(error or "渲染失败")

    async def _cleanup_task(self, files: list[Path]):
        await asyncio.sleep(InternalCFG.DELAY_SEND)
        for p in files:
            try:
                if p.exists(): p.unlink()
            except Exception as e:
                logger.warning(f"清理失败 {p}: {e}")

    def _init_prefixes(self, context: Context):
        try:
            gc = context.get_config()
            raw = gc.get("wake_prefix", ["/"])
            self.prefixes = [raw] if isinstance(raw, str) else list(raw)
        except Exception as e:
            logger.warning(f"获取唤醒词失败: {e}")
            self.prefixes = ["/"]

    # ============ 新指令：中文 + 短英文 ============
    # /帮助菜单 /bot菜单 /菜单帮助
    @filter.command("帮助菜单")
    async def help_menu_cn(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.5): return
        logger.info(f"[帮助菜单] {event.get_sender_id()} q={query!r}")
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    @filter.command("bot菜单")
    async def bot_menu(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.5): return
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    @filter.command("菜单帮助")
    async def menu_help(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.5): return
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    # 英文短别名
    @filter.command("hmenu")
    async def hmenu(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.5): return
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    @filter.command("bhelp")
    async def bhelp(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.5): return
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    # QZone 测试
    @filter.command("qztest")
    async def qztest(self, event: AstrMessageEvent):
        if not self._dedup(event, "qz", 1.0): return
        ok = await self._send_qzone_card(event)
        qz = getattr(self.plugin_config, "qzone_share", None)
        mode = getattr(qz, "mode", "?") if qz else "?"
        url = getattr(qz, "url", "") if qz else ""
        yield event.plain_result(f"{'✅已发送' if ok else '❌失败'}\nmode={mode}\n{url[:120]}")

    @filter.command("空间测试")
    async def kongjian_test(self, event: AstrMessageEvent):
        async for r in self.qztest(event):
            yield r

    # events / filters 新名
    @filter.command("事件列表")
    async def events_cn(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.evt_analyzer, "AstrBot 事件监听", "event", query, with_qzone_share=False):
            yield r

    @filter.command("过滤器列表")
    async def filters_cn(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.flt_analyzer, "AstrBot 过滤器分析", "filter", query, with_qzone_share=False):
            yield r

    # 兼容旧英文短名（避免彻底断）： events_ty / filters_ty
    @filter.command("events_ty")
    async def events_ty(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.evt_analyzer, "AstrBot 事件监听", "event", query, with_qzone_share=False):
            yield r

    @filter.command("filters_ty")
    async def filters_ty(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.flt_analyzer, "AstrBot 过滤器分析", "filter", query, with_qzone_share=False):
            yield r

    # ---------- 兜底 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _fallback(self, event: AstrMessageEvent):
        try:
            txt_raw = (event.message_str or "").strip()
            if not txt_raw:
                return
            txt = txt_raw.lower()
            # 去前缀
            for p in self.prefixes + ["/", "!", "！", ".", "。", "#"]:
                if p and txt.startswith(p.lower()):
                    txt = txt[len(p):].lstrip()
                    break
            first = txt.split()[0] if txt.split() else ""
            mapping = {
                "帮助菜单": "hm", "bot菜单": "hm", "菜单帮助": "hm",
                "hmenu": "hm", "bhelp": "hm",
                # 旧的也容错一下，但去重会拦
                "helps": "hm", "help": "hm", "菜单": "hm", "帮助": "hm", "cd": "hm",
                "qztest": "qz", "qz": "qz", "空间测试": "qz", "空间": "qz",
            }
            if first in mapping:
                if not self._dedup(event, mapping[first], 1.5):
                    return
                event.stop_event()
                logger.info(f"[fallback] {event.message_str!r} -> {first}")
                if mapping[first] == "qz":
                    async for r in self.qztest(event):
                        yield r
                else:
                    query = txt[len(first):].strip()
                    async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
                        yield r
        except Exception as e:
            logger.debug(f"fallback异常: {e}")
