import asyncio
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Image, Share, Plain, Json

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
                qzone_share=QzoneShareConfig(True, "", "Bot使用说明", "", "", "text")
            )

        raw_path = self.plugin_config.custom_font_path
        if raw_path and raw_path.strip():
            self.user_font_dir = Path(raw_path)
        else:
            self.user_font_dir = self.data_dir / "fonts"
        try:
            self.user_font_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if not raw_path:
                logger.warning(f"[HelpTypst] 无法创建默认字体目录: {e}")

        self.builtin_font_dir = self.plugin_dir / "resources" / InternalCFG.NAME_FONT_DIR
        self.font_dirs = [self.builtin_font_dir, self.user_font_dir]

        self.font_manager = FontManager(self.font_dirs)
        self.layout = TypstLayout(self.plugin_config)
        self.hint = HelpHint()
        self.msg = MsgRecall()

        self.renderer = TypstRenderer(
            star=self,
            data_dir=self.data_dir,
            template_path=self.template_path,
            font_dirs=self.font_dirs,
            config=self.plugin_config,
        )

        self.cmd_analyzer = CommandAnalyzer(context, self.plugin_config)
        self.evt_analyzer = EventAnalyzer(context, self.plugin_config)
        self.flt_analyzer = FilterAnalyzer(context, self.plugin_config)

        self.prefixes: list[str] = []
        self._plugin_id = "astrbot_plugin_help_typs87"
        try:
            import yaml
            meta_path = self.plugin_dir / "metadata.yaml"
            if meta_path.exists():
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = yaml.safe_load(f)
                    self._plugin_id = meta.get("name", self._plugin_id)
        except Exception:
            pass
        qz = getattr(self.plugin_config, "qzone_share", None)
        logger.info(f"[HelpTypst] plugin_id={self._plugin_id} qzone_enable={getattr(qz,'enable',False)} mode={getattr(qz,'mode','?')} url={(getattr(qz,'url','') or '')[:80]}")

    async def initialize(self):
        self._init_prefixes(self.context)
        await asyncio.to_thread(self._refresh_resources)
        logger.info(f"[HelpTypst] 初始化完成 prefixes={self.prefixes}")

    def _refresh_resources(self):
        try:
            self.font_manager.scan_fonts()
            self.font_manager.update_json_schema(self.schema_path)
            self.font_manager.prune_invalid_config_items(self.config)
        except Exception as e:
            logger.warning(f"[HelpTypst] 资源重载失败: {e}")

    async def terminate(self):
        await self._perform_cleanup()
        try:
            self._refresh_resources()
        except Exception:
            pass

    async def _perform_cleanup(self):
        try:
            temp_files = list(self.data_dir.glob("temp_*"))
            for f in temp_files:
                try:
                    if f.exists():
                        f.unlink()
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"[HelpTypst] 清理失败: {e}")

    @filter.command_group("typst")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def typst(self):
        pass

    @typst.command("font")
    async def cmd_scan_fonts(self, event: AstrMessageEvent):
        await asyncio.to_thread(self._refresh_resources)
        count = len(self.font_manager.available_families)
        try:
            pm = getattr(self.context, "_star_manager", None)
            if pm:
                plugin_name = self._plugin_id
                yield event.plain_result(f"✅ 扫描完成 ({count} fonts)。正在重载...")
                asyncio.create_task(self._safe_reload(pm, plugin_name))
            else:
                yield event.plain_result(f"✅ 扫描完成 ({count} fonts)。请手动重载插件")
        except Exception as e:
            yield event.plain_result(f"❌ 自动重载失败: {e}")

    async def _safe_reload(self, pm, plugin_name):
        await asyncio.sleep(InternalCFG.DELAY_SEND)
        try:
            logger.info(f"[HelpTypst] 自我重载: {plugin_name}")
            await pm.reload(plugin_name)
        except Exception as e:
            logger.error(f"[HelpTypst] 自我重载异常: {e}")

    # ========== QZone ==========
    def _is_qq_platform(self, event: AstrMessageEvent) -> bool:
        try:
            plat = (event.get_platform_name() or "").lower()
            if any(k in plat for k in ("aiocqhttp", "qq", "onebot", "napcat", "qofficial", "qzone", "go-cqhttp")):
                return True
        except Exception:
            pass
        try:
            umo = (event.unified_msg_origin or "").lower()
            if any(k in umo for k in ("aiocqhttp", "qq", "onebot", "napcat")):
                return True
        except Exception:
            pass
        return False

    async def _send_qzone_card(self, event: AstrMessageEvent) -> bool:
        """尝试发送QQ空间卡片，成功True，失败False。绝不抛异常。"""
        try:
            qz = self.plugin_config.qzone_share
        except Exception as e:
            logger.warning(f"[HelpTypst] qzone配置读取失败: {e}")
            return False
        if not getattr(qz, "enable", False):
            return False
        url = (getattr(qz, "url", "") or "").strip()
        if not url.startswith("http"):
            logger.warning(f"[HelpTypst] QZone url无效: {url}")
            return False

        mode = getattr(qz, "mode", "text").lower()
        title = (getattr(qz, "title", "") or "Bot使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        image = (getattr(qz, "image", "") or "").strip()

        # 1. share 模式
        if mode == "share":
            try:
                from astrbot.api.message_components import MessageChain
                chain = MessageChain([Share(url=url, title=title, content=content, image=image if image else None)])
                logger.info(f"[HelpTypst] 尝试 Share 发送: {url[:60]}...")
                await event.send(chain)
                logger.info("[HelpTypst] Share 发送成功")
                return True
            except Exception as e:
                logger.warning(f"[HelpTypst] Share 发送失败，降级 text: {e}")
                # fallthrough

        # 2. json 卡片模式（OneBot json）
        if mode == "json":
            try:
                # 构造一个通用的链接卡片 json（兼容 NapCat）
                import json as js
                card = {
                    "app": "com.tencent.structmsg",
                    "config": {"autosize": True, "forward": True, "type": "normal"},
                    "desc": "Bot",
                    "extra": {"app_type": 1, "appid": 8, "msg_seq": 1},
                    "meta": {
                        "news": {
                            "action": "",
                            "android_pkg_name": "",
                            "app_type": 1,
                            "appid": 8,
                            "desc": content,
                            "jumpUrl": url,
                            "preview": image or "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png",
                            "source_icon": "",
                            "source_url": "",
                            "tag": "QQ空间",
                            "title": title
                        }
                    },
                    "prompt": f"[分享] {title}",
                    "ver": "0.0.0.1",
                    "view": "news"
                }
                from astrbot.api.message_components import MessageChain
                await event.send(MessageChain([Json(data=card)]))
                logger.info("[HelpTypst] JSON卡片发送成功")
                return True
            except Exception as e:
                logger.warning(f"[HelpTypst] JSON卡片发送失败，降级 text: {e}")

        # 3. text 降级（默认最稳）
        try:
            from astrbot.api.message_components import MessageChain
            txt = f"📖 {title}\n{content}\n{url}" if content else f"📖 {title}\n{url}"
            await event.send(MessageChain([Plain(txt)]))
            logger.info("[HelpTypst] Text URL 发送成功")
            return True
        except Exception as e:
            logger.error(f"[HelpTypst] Text URL 也失败: {e}")
            return False

    # ========== 请求处理 ==========
    async def _handle_request(
        self,
        event: AstrMessageEvent,
        analyzer,
        title: str,
        mode: str,
        query: str | None,
        *,
        with_qzone_share: bool = False,
    ):
        logger.info(f"[HelpTypst] _handle_request mode={mode} query={query!r} qzone={with_qzone_share} sender={event.get_sender_id()} platform={event.get_platform_name()}")
        wait_msg_id = None
        if self.plugin_config.enable_waiting_message:
            hint_text = self.hint.msg_searching(query) if query else self.hint.msg_rendering(mode)
            wait_msg_id = await self.msg.send_wait(event, hint_text)

        def data_pipeline(save_path: Path) -> int:
            plugins = analyzer.get_plugins(query)
            if not plugins:
                return 0
            display_title = f'搜索结果: "{query}"' if query else title
            user_fonts = self.plugin_config.appearance.get_active_font_order()
            final_font_list = self.font_manager.get_render_font_list(user_fonts)
            self.layout.dump_layout_json(
                plugins=plugins,
                save_path=save_path,
                title=display_title,
                mode=mode,
                prefixes=self.prefixes,
                font_list=final_font_list,
            )
            return len(plugins)

        result, error = await self.renderer.render(data_pipeline, mode, query)
        if wait_msg_id:
            await self.msg.recall(event, wait_msg_id)

        if result:
            try:
                # 先尝试发 QZone 卡片（独立发送，失败不影响图片）
                if with_qzone_share:
                    try:
                        await self._send_qzone_card(event)
                    except Exception as e:
                        logger.warning(f"[HelpTypst] QZone外层异常: {e}")

                # 再发图片（必定发送，独立 chain）
                images = [Image.fromFileSystem(p) for p in result.images]
                if images:
                    logger.info(f"[HelpTypst] 发送图片 {len(images)} 张")
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
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.warning(f"[HelpTypst] 临时文件清理失败 {p}: {e}")

    def _init_prefixes(self, context: Context):
        try:
            global_config = context.get_config()
            raw = global_config.get("wake_prefix", ["/"])
            self.prefixes = [raw] if isinstance(raw, str) else list(raw)
        except Exception as e:
            logger.warning(f"[HelpTypst] 获取唤醒词失败，使用默认值 '/': {e}")
            self.prefixes = ["/"]

    # ============ 指令 ============
    # 一个个单独注册，避免多装饰器叠加失效
    @filter.command("helps")
    async def show_menu_helps(self, event: AstrMessageEvent, query: str = ""):
        logger.info(f"[HelpTypst] helps触发 sender={event.get_sender_id()} query={query!r}")
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    @filter.command("help")
    async def show_menu_help(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    @filter.command("菜单")
    async def show_menu_cd(self, event: AstrMessageEvent):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", "", with_qzone_share=True):
            yield r

    @filter.command("帮助")
    async def show_menu_help_cn(self, event: AstrMessageEvent):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", "", with_qzone_share=True):
            yield r

    @filter.command("cd")
    async def show_menu_cd_en(self, event: AstrMessageEvent):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", "", with_qzone_share=True):
            yield r

    # QZone 测试 - 分开注册
    @filter.command("qztest")
    async def qztest_cmd(self, event: AstrMessageEvent):
        logger.info("[HelpTypst] /qztest")
        ok = await self._send_qzone_card(event)
        if ok:
            yield event.plain_result("✅ QZone 卡片已尝试发送，见上条")
        else:
            qz = getattr(self.plugin_config, "qzone_share", None)
            url = getattr(qz, "url", "") if qz else ""
            mode = getattr(qz, "mode", "") if qz else ""
            yield event.plain_result(f"❌ 发送失败或未启用\nmode={mode}\nurl={url}")

    @filter.command("qz")
    async def qz_cmd(self, event: AstrMessageEvent):
        async for r in self.qztest_cmd(event):
            yield r

    @filter.command("空间")
    async def kongjian_cmd(self, event: AstrMessageEvent):
        async for r in self.qztest_cmd(event):
            yield r

    # events / filters
    @filter.command("events")
    async def show_events(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.evt_analyzer, "AstrBot 事件监听", "event", query, with_qzone_share=False):
            yield r

    @filter.command("filters")
    async def show_filters(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.flt_analyzer, "AstrBot 过滤器分析", "filter", query, with_qzone_share=False):
            yield r

    # ============ 兜底监听 ============
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _fallback_listener(self, event: AstrMessageEvent):
        try:
            txt_raw = (event.message_str or "").strip()
            if not txt_raw:
                return
            txt = txt_raw.lower()
            # 去前缀
            for p in self.prefixes + ["/", "!", "！", ".", "。", "#", " "]:
                if p and txt.startswith(p.lower()):
                    txt = txt[len(p):].lstrip()
                    break
            triggers = {
                "helps", "help", "菜单", "帮助", "cd", "menu",
                "qz", "qztest", "空间", "qzone"
            }
            # 支持 helps xxx 搜索
            first = txt.split()[0] if txt.split() else ""
            if first in triggers or txt in triggers:
                logger.info(f"[HelpTypst] fallback命中: {event.message_str!r} -> {first}")
                event.stop_event()
                # qz 类走卡片测试
                if first in ("qz", "qztest", "空间", "qzone"):
                    async for r in self.qztest_cmd(event):
                        yield r
                    return
                # 其余走菜单
                query = txt[len(first):].strip() if len(txt) > len(first) else ""
                async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
                    yield r
        except Exception as e:
            logger.debug(f"[HelpTypst] fallback异常: {e}")
