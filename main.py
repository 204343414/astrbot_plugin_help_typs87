import asyncio
import time
import json as js
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
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
                with open(meta, "r", encoding="utf-8") as mf:
                    d = yaml.safe_load(mf)
                    self._plugin_id = d.get("name", self._plugin_id)
        except Exception:
            pass

        qz = getattr(self.plugin_config, "qzone_share", None)
        logger.info(f"[HelpTypst V6] id={self._plugin_id} qzone={getattr(qz,'enable',False)}/{getattr(qz,'mode','?')}")

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
    def _dedup(self, event: AstrMessageEvent, key: str, ttl: float = 2.0) -> bool:
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

    def _is_qq(self, event: AstrMessageEvent) -> bool:
        try:
            p = (event.get_platform_name() or "").lower()
            if any(k in p for k in ("aiocqhttp", "qq", "onebot", "napcat", "go-cqhttp", "qzone", "llonebot")):
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

    def _parse_qzone(self, url: str) -> dict:
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
                "url": url
            }
        except Exception:
            return {"url": url}

    async def _send_qzone_card(self, event: AstrMessageEvent) -> bool:
        """发送QQ空间Ark卡片，依次尝试：Json组件 -> NapCat原生API -> 文本降级"""
        try:
            qz = self.plugin_config.qzone_share
        except Exception as e:
            logger.warning(f"qzone配置读取失败: {e}")
            return False
        if not getattr(qz, "enable", False):
            logger.info("[QZone] disabled in config")
            return False
        url = (getattr(qz, "url", "") or "").strip()
        if not url.startswith("http"):
            logger.warning(f"QZone url无效: {url}")
            return False

        mode = (getattr(qz, "mode", "json") or "json").lower()
        title = (getattr(qz, "title", "") or "Bot使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        image = (getattr(qz, "image", "") or "").strip()

        info = self._parse_qzone(url)
        preview_img = image or "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png"
        appid_str = info.get("appid", "311")
        try:
            appid_int = int(appid_str) if str(appid_str).isdigit() else 311
        except Exception:
            appid_int = 311

        # 构造 Ark JSON - 3种样式兜底
        ark_variants = []

        # 1) 标准 news 卡
        ark_variants.append({
            "app": "com.tencent.structmsg",
            "config": {"autosize": True, "forward": True, "type": "normal"},
            "desc": "QQ空间",
            "extra": {"app_type": 1, "appid": appid_int},
            "meta": {
                "news": {
                    "action": "",
                    "android_pkg_name": "",
                    "app_type": 1,
                    "appid": appid_int,
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
        })

        # 2) 简化版 - 去掉 extra
        ark_variants.append({
            "app": "com.tencent.structmsg",
            "desc": "QQ空间",
            "view": "news",
            "ver": "0.0.0.1",
            "prompt": f"[QQ空间] {title}",
            "meta": {
                "news": {
                    "title": title,
                    "desc": content,
                    "preview": preview_img,
                    "jumpUrl": url,
                    "tag": "QQ空间",
                    "source_url": "",
                    "source_icon": ""
                }
            },
            "config": {"type": "normal", "forward": True, "autosize": True}
        })

        # 3) mqqapi 跳转（更像原生说说）
        ark_variants.append({
            "app": "com.tencent.miniapp_01",
            "desc": "",
            "view": "notification",
            "ver": "0.0.0.1",
            "prompt": "[QQ空间] " + title,
            "meta": {
                "notification": {
                    "appInfo": {
                        "appName": "QQ空间",
                        "appType": 4,
                        "appid": 311,
                        "iconUrl": "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png"
                    },
                    "data": [
                        {"title": title, "value": content}
                    ],
                    "emphasis_keyword": title,
                    "title": "说说"
                }
            },
            "config": {"type": "normal", "forward": True, "autosize": True, "ctime": int(time.time())}
        })

        # 尝试 JSON 组件发送
        if mode in ("json", "auto", "ark"):
            for i, card in enumerate(ark_variants, 1):
                try:
                    logger.info(f"[QZone] 尝试 Ark v{i}")
                    await event.send(MessageChain([Json(data=card)]))
                    logger.info(f"[QZone] Ark v{i} 发送成功")
                    return True
                except Exception as e:
                    logger.warning(f"[QZone] Ark v{i} 失败: {e}")
                    continue

            # NapCat 原生 API 兜底
            try:
                # 尝试直接调 OneBot API send_group_msg / send_private_msg 带 json 段
                client = getattr(event, "bot", None)
                if not client:
                    # 从 message_obj 拿
                    raw = getattr(event.message_obj, "raw_message", None)
                    if raw and hasattr(raw, "get"):
                        client = getattr(event, "bot", None)
                # 如果能拿到 aiocqhttp bot 实例
                bot = getattr(event, "_bot", None) or getattr(event, "bot", None)
                # AstrBot 的 AiocqhttpMessageEvent 有 bot 属性
                if hasattr(event, "bot"):
                    bot = event.bot
                if bot:
                    is_group = bool(event.get_group_id())
                    target_id = int(event.get_group_id() or event.get_sender_id() or 0)
                    if target_id:
                        import json as js
                        card_str = js.dumps(ark_variants[0], ensure_ascii=False, separators=(',', ':'))
                        msg_seg = [{"type": "json", "data": {"data": card_str}}]
                        if is_group:
                            await bot.call_action("send_group_msg", group_id=target_id, message=msg_seg)
                        else:
                            await bot.call_action("send_private_msg", user_id=target_id, message=msg_seg)
                        logger.info("[QZone] NapCat原生API发送成功")
                        return True
            except Exception as e:
                logger.warning(f"[QZone] NapCat原生API失败: {e}")

        # 最终文本降级
        try:
            txt = f"📖 {title}\n{content}\n{url}" if content else f"📖 {title}\n{url}"
            await event.send(MessageChain([Plain(txt)]))
            logger.info("[QZone] 文本降级发送成功")
            return True
        except Exception as e:
            logger.error(f"[QZone] 文本也失败: {e}", exc_info=True)
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
                if with_qzone_share:
                    try:
                        ok = await self._send_qzone_card(event)
                        logger.info(f"[HelpTypst] QZone卡片结果: {ok}")
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        logger.warning(f"QZone外层异常: {e}", exc_info=True)
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

    # ============ 指令：全部 helps_xxx ============
    async def _run_menu(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    # 主指令组
    @filter.command("helps_菜单")
    async def helps_menu(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.8): return
        logger.info(f"[helps_菜单] {event.get_sender_id()} q={query!r}")
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_帮助")
    async def helps_help(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.8): return
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_bot")
    async def helps_bot(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.8): return
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_说明")
    async def helps_sm(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.8): return
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_指令")
    async def helps_cmd(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "hm", 1.8): return
        async for r in self._run_menu(event, query):
            yield r

    # QZone 测试 - 也用 helps_ 前缀避冲突
    @filter.command("helps_qz")
    async def helps_qz(self, event: AstrMessageEvent):
        if not self._dedup(event, "qz", 1.2): return
        logger.info("[helps_qz] 测试")
        ok = await self._send_qzone_card(event)
        qz = getattr(self.plugin_config, "qzone_share", None)
        mode = getattr(qz, "mode", "?") if qz else "?"
        url = getattr(qz, "url", "") if qz else ""
        # 单独回一条结果，不和卡片混发，避免二次触发
        yield event.plain_result(f"{'✅ Ark卡片已发' if ok else '❌失败，已降级'}\nmode={mode}\n{url[:100]}")

    @filter.command("helps_空间")
    async def helps_kj(self, event: AstrMessageEvent):
        async for r in self.helps_qz(event):
            yield r

    # events / filters
    @filter.command("helps_事件")
    async def helps_events(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.evt_analyzer, "AstrBot 事件监听", "event", query, with_qzone_share=False):
            yield r

    @filter.command("helps_过滤器")
    async def helps_filters(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.flt_analyzer, "AstrBot 过滤器分析", "filter", query, with_qzone_share=False):
            yield r

    # ---------- 兜底：拦截旧指令名，防止进LLM ----------
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
            if not txt:
                return
            first = txt.split()[0]
            query = txt[len(first):].strip()

            # 新指令映射
            new_menu_cmds = {
                "helps_菜单", "helps_帮助", "helps_bot", "helps_说明", "helps_指令",
                "hmenu", "bhelp",
                # 中文裸词（兼容）
                "帮助菜单", "bot菜单", "菜单帮助"
            }
            new_qz_cmds = {"helps_qz", "helps_空间", "qztest", "空间测试"}
            # 旧指令拦截
            old_menu_cmds = {"helps", "help", "菜单", "帮助", "cd", "menu"}
            old_qz_cmds = {"qz", "qztest", "空间", "qzone"}

            if first in new_menu_cmds or first in old_menu_cmds:
                if not self._dedup(event, "hm", 1.5):
                    event.stop_event()
                    return
                event.stop_event()
                logger.info(f"[fallback] {event.message_str!r} -> menu query={query!r}")
                async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
                    yield r
                return

            if first in new_qz_cmds or first in old_qq_cmds if (old_qq_cmds:={"qz","qzone","空间"}) else False:
                pass  # 下面统一处理

            if first in new_qz_cmds or first in {"qz", "qztest", "空间", "qzone", "空间测试"}:
                if not self._dedup(event, "qz", 1.0):
                    event.stop_event()
                    return
                event.stop_event()
                logger.info(f"[fallback] {event.message_str!r} -> qztest")
                async for r in self.helps_qz(event):
                    yield r
                return
        except Exception as e:
            logger.debug(f"fallback异常: {e}", exc_info=True)
