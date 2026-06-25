import asyncio
import time
import json as js
import hashlib
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
        self._last_send: dict[str, float] = {}

        self._plugin_id = "astrbot_plugin_help_typs87"
        try:
            import yaml
            meta = self.plugin_dir / "metadata.yaml"
            if meta.exists():
                with open(meta, "r", encoding="utf-8") as f:
                    d = yaml.safe_load(f) or {}
                    self._plugin_id = d.get("name", self._plugin_id)
        except Exception:
            pass

        qz = getattr(self.plugin_config, "qzone_share", None)
        logger.info(f"[HelpTypst V7] {self._plugin_id} qzone={getattr(qz,'enable',False)}/{getattr(qz,'mode','?')}")

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

    # ---------- dedup ----------
    def _dedup(self, event: AstrMessageEvent, key: str, ttl: float = 2.2) -> bool:
        try:
            uid = f"{event.get_sender_id()}:{key}:{event.get_group_id() or 'p'}"
        except Exception:
            uid = key
        now = time.time()
        last = self._last_send.get(uid, 0)
        if now - last < ttl:
            logger.info(f"[HelpTypst] 去重拦截 {uid} {now-last:.2f}s")
            return False
        self._last_send[uid] = now
        return True

    # ---------- QZone Ark signing ----------
    def _gen_token(self, card_dict: dict) -> str:
        """Generate a simple MD5 token for Ark card signing. QQNT requires this to render cards."""
        raw = js.dumps(card_dict, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        # Simple hash-based token - not cryptographic, just satisfies QQNT validation
        token_input = f"qzoneshare{raw}{int(time.time())}"
        return hashlib.md5(token_input.encode('utf-8')).hexdigest()

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
        """发QQ空间Ark卡，失败绝不抛异常，保证后续图片能发"""
        try:
            qz = self.plugin_config.qzone_share
        except Exception as e:
            logger.warning(f"[QZone] 读配置失败: {e}")
            return False
        if not getattr(qz, "enable", False):
            return False
        url = (getattr(qz, "url", "") or "").strip()
        if not url.startswith("http"):
            logger.warning(f"[QZone] url无效: {url}")
            return False

        mode = (getattr(qz, "mode", "json") or "json").lower()
        title = (getattr(qz, "title", "") or "Bot使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        image = (getattr(qz, "image", "") or "").strip()
        preview_img = image or "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png"

        info = self._parse_qzone(url)
        appid = info.get("appid", "311")
        try:
            appid_int = int(appid) if str(appid).isdigit() else 311
        except Exception:
            appid_int = 311

        # 5套 Ark 卡，逐一尝试
        cards = []

        ctime = int(time.time())

        # A) structmsg news - 标准分享卡（加 ctime + token 签名）
        card_a = {
            "app": "com.tencent.structmsg",
            "config": {"autosize": True, "forward": True, "type": "normal", "ctime": ctime, "token": ""},
            "desc": "QQ空间",
            "extra": {"app_type": 1, "appid": 100358126},
            "meta": {"news": {
                "action": "", "android_pkg_name": "", "app_type": 1, "appid": 100358126,
                "bizsrc": "qzone.share",
                "desc": content,
                "jumpUrl": url,
                "preview": preview_img,
                "source_icon": "https://qzonestyle.gtimg.cn/qzone/vas/opensns/res/img/favicon.ico",
                "source_url": url,
                "tag": "QQ空间",
                "title": title,
                "ctime": ctime
            }},
            "prompt": f"[分享] {title}",
            "ver": "0.0.0.1",
            "view": "news"
        }
        card_a["config"]["token"] = self._gen_token(card_a)
        cards.append(("structmsg_news", card_a))

        # B) com.tencent.qzone.share 原生空间卡片
        card_b = {
            "app": "com.tencent.qzone.share",
            "config": {"autosize": True, "forward": True, "type": "normal", "ctime": ctime, "token": ""},
            "desc": "QQ空间",
            "extra": {"app_type": 1, "appid": 100358126, "uin": 2562925383},
            "meta": {"news": {
                "action": "", "android_pkg_name": "", "app_type": 1, "appid": 100358126,
                "bizsrc": "qzone",
                "desc": content,
                "jumpUrl": url,
                "preview": preview_img,
                "source_icon": "https://qzonestyle.gtimg.cn/qzone/vas/opensns/res/img/favicon.ico",
                "source_url": url,
                "tag": "说说",
                "title": title,
                "ctime": ctime
            }},
            "prompt": f"[QQ空间] {title}",
            "ver": "1.0.0.1",
            "view": "news"
        }
        card_b["config"]["token"] = self._gen_token(card_b)
        cards.append(("qzone_share", card_b))

        # C) mqqapi qzone 跳转（notification 视图）
        card_c = {
            "app": "com.tencent.miniapp_01",
            "desc": "",
            "view": "notification",
            "ver": "0.0.0.1",
            "prompt": f"[QQ空间] {title}",
            "meta": {"notification": {
                "appInfo": {
                    "appName": "QQ空间",
                    "appType": 4,
                    "appid": 100358126,
                    "iconUrl": "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png"
                },
                "data": [
                    {"title": "说说", "value": title},
                    {"title": "详情", "value": content}
                ],
                "emphasis_keyword": title,
                "title": "说说",
                "sourceUrl": url,
                "iconUrl": "https://qzonestyle.gtimg.cn/qzone/vas/opensns/res/img/favicon.ico"
            }},
            "config": {"type": "normal", "ctime": ctime, "forward": True, "token": "", "autosize": True}
        }
        card_c["config"]["token"] = self._gen_token(card_c)
        cards.append(("mqq_qzone", card_c))

        # D) music 卡伪装（NapCat 对 music 卡支持较好）
        card_d = {
            "app": "com.tencent.music.lua",
            "bizsrc": "qqconnect.sdkshare_music",
            "desc": "QQ空间",
            "view": "music",
            "ver": "0.0.0.1",
            "prompt": f"[分享] {title}",
            "meta": {"music": {
                "action": "",
                "android_pkg_name": "",
                "app_type": 1,
                "appid": 100495085,
                "desc": content,
                "jumpUrl": url,
                "musicUrl": url,
                "preview": preview_img,
                "sourceMsgId": "0",
                "source_icon": "",
                "source_url": "",
                "tag": "QQ空间",
                "title": title,
                "uin": 2562925383,
                "ctime": ctime
            }},
            "config": {"type": "normal", "forward": True, "autosize": True, "token": "", "ctime": ctime}
        }
        card_d["config"]["token"] = self._gen_token(card_d)
        cards.append(("music_fake", card_d))

        # E) 最简 news（带签名）
        card_e = {
            "app": "com.tencent.structmsg",
            "view": "news",
            "ver": "0.0.0.1",
            "prompt": title,
            "meta": {"news": {"jumpUrl": url, "title": title, "desc": content, "preview": preview_img, "tag": "QQ空间"}},
            "desc": "QQ空间",
            "config": {"autosize": True, "forward": True, "ctime": ctime, "token": "", "type": "normal"}
        }
        card_e["config"]["token"] = self._gen_token(card_e)
        cards.append(("mini", card_e))

        if mode in ("json", "ark", "auto"):
            for name, card in cards:
                # 方式1: AstrBot Json 组件
                try:
                    logger.info(f"[QZone] 尝试 Ark {name} via Json组件")
                    await event.send(MessageChain([Json(data=card)]))
                    logger.info(f"[QZone] Ark {name} 成功")
                    return True
                except Exception as e:
                    logger.warning(f"[QZone] Ark {name} Json组件失败: {e}")
                # 方式2: NapCat 原生 API
                try:
                    bot = getattr(event, "bot", None)
                    if bot:
                        is_group = bool(event.get_group_id())
                        target = event.get_group_id() or event.get_sender_id()
                        if target and str(target).isdigit():
                            target = int(target)
                            msg = [{"type": "json", "data": {"data": js.dumps(card, ensure_ascii=False, separators=(',', ':'))}}]
                            if is_group:
                                await bot.call_action("send_group_msg", group_id=target, message=msg)
                            else:
                                await bot.call_action("send_private_msg", user_id=target, message=msg)
                            logger.info(f"[QZone] Ark {name} NapCat API 成功")
                            return True
                except Exception as e2:
                    logger.warning(f"[QZone] Ark {name} NapCatAPI失败: {e2}")
                    continue

        # 文本降级
        try:
            txt = f"📖 {title}\n{content}\n{url}" if content else f"📖 {title}\n{url}"
            await event.send(MessageChain([Plain(txt)]))
            logger.info("[QZone] text降级成功")
            return True
        except Exception as e:
            logger.error(f"[QZone] text也失败: {e}", exc_info=True)
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
                    ok = await self._send_qzone_card(event)
                    logger.info(f"[HelpTypst] QZone结果={ok}")
                    await asyncio.sleep(0.35)
                images = [Image.fromFileSystem(p) for p in result.images]
                if images:
                    logger.info(f"[HelpTypst] 发图 {len(images)} 张")
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
            logger.warning(f"唤醒词失败: {e}")
            self.prefixes = ["/"]

    # ============ 指令：全部 helps_xxx ============
    async def _run_menu(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    # helps_菜单系
    @filter.command("helps_菜单")
    async def helps_menu(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "helps_menu", 2.0): return
        logger.info(f"[helps_菜单] {event.get_sender_id()} q={query!r}")
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_帮助")
    async def helps_help(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "helps_menu", 2.0): return
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_bot")
    async def helps_bot(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "helps_menu", 2.0): return
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_说明")
    async def helps_sm(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "helps_menu", 2.0): return
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_指令")
    async def helps_cmd(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "helps_menu", 2.0): return
        async for r in self._run_menu(event, query):
            yield r

    # QZone测试 - 带详细日志返回
    @filter.command("qztest")
    async def qztest(self, event: AstrMessageEvent):
        """测试QQ空间Ark卡片发送，返回详细调试日志"""
        if not self._dedup(event, "qztest", 3.0): return
        logger.info("[qztest] 开始测试 Ark 卡片发送")
        
        qz = getattr(self.plugin_config, "qzone_share", None)
        if not qz or not getattr(qz, "enable", False):
            yield event.plain_result("❌ QZone 卡片未启用\n请在配置中设置 qzone_share.enable = true")
            return
        
        url = (getattr(qz, "url", "") or "").strip()
        mode = (getattr(qz, "mode", "json") or "json").lower()
        title = (getattr(qz, "title", "") or "Bot使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        
        log_lines = [f"🧪 qztest Ark 诊断报告", f"━━━━━━━━━━━━━━━", 
                     f"模式: {mode}", f"URL: {url[:80]}...", 
                     f"标题: {title}", f"内容: {content[:40]}..."]
        
        # 测试每张卡片
        ctime = int(time.time())
        test_card = {
            "app": "com.tencent.structmsg",
            "config": {"autosize": True, "forward": True, "type": "normal", "ctime": ctime},
            "desc": "QQ空间", "extra": {"app_type": 1, "appid": 100358126},
            "meta": {"news": {
                "action": "", "android_pkg_name": "", "app_type": 1, "appid": 100358126,
                "bizsrc": "qzone.share", "desc": content, "jumpUrl": url,
                "preview": "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png",
                "source_icon": "https://qzonestyle.gtimg.cn/qzone/vas/opensns/res/img/favicon.ico",
                "source_url": url, "tag": "QQ空间", "title": title, "ctime": ctime
            }},
            "prompt": f"[分享] {title}", "ver": "0.0.0.1", "view": "news"
        }
        test_card["config"]["token"] = self._gen_token(test_card)
        
        log_lines.append(f"签名Token: {test_card['config']['token'][:16]}...")
        log_lines.append(f"┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅")
        
        # 尝试发送测试卡片
        try:
            await event.send(MessageChain([Json(data=test_card)]))
            log_lines.append("✅ Json组件发送: 成功")
            sent_via = "Json组件"
        except Exception as e:
            err_msg = str(e)[:100]
            log_lines.append(f"❌ Json组件发送: 失败 → {err_msg}")
            
            # 尝试 NapCat 原生 API
            try:
                bot = getattr(event, "bot", None)
                if bot:
                    is_group = bool(event.get_group_id())
                    target = event.get_group_id() or event.get_sender_id()
                    if target and str(target).isdigit():
                        target = int(target)
                        msg = [{"type": "json", "data": {"data": js.dumps(test_card, ensure_ascii=False, separators=(',', ':'))}}]
                        if is_group:
                            await bot.call_action("send_group_msg", group_id=target, message=msg)
                        else:
                            await bot.call_action("send_private_msg", user_id=target, message=msg)
                        log_lines.append("✅ NapCat API 发送: 成功")
                        sent_via = "NapCat API"
            except Exception as e2:
                log_lines.append(f"❌ NapCat API 发送: 失败 → {str(e2)[:80]}")
                sent_via = "全部失败"
        
        log_lines.append(f"━━━━━━━━━━━━━━━")
        log_lines.append(f"发送通道: {sent_via}")
        log_lines.append(f"时间戳: {ctime}")
        log_lines.append(f"提示: 如果客户端显示'版本过低'，请检查 NapCat/QQNT 版本")
        
        yield event.plain_result("\n".join(log_lines))

    @filter.command("helps_qz")
    async def helps_qz(self, event: AstrMessageEvent):
        if not self._dedup(event, "helps_qz", 2.5): return
        logger.info("[helps_qz] 触发")
        ok = await self._send_qzone_card(event)
        qz = getattr(self.plugin_config, "qzone_share", None)
        mode = getattr(qz, "mode", "?") if qz else "?"
        url = getattr(qz, "url", "") if qz else ""
        yield event.plain_result(f"{'✅ Ark已发' if ok else '❌已降级text'}\nmode={mode}\n{url[:120]}")

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

    # ---------- fallback 拦截 helps_xxx 指令 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _fallback(self, event: AstrMessageEvent):
        try:
            txt_raw = (event.message_str or "").strip()
            if not txt_raw:
                return
            txt = txt_raw.lower()
            # 去前缀
            for p in self.prefixes + ["/", "!", "！", ".", "。", "#", "！", " "]:
                if p and txt.startswith(p.lower()):
                    txt = txt[len(p):].lstrip()
                    break
            if not txt:
                return
            first = txt.split()[0]
            query = txt[len(first):].strip()

            # 只监听 helps_xxx 前缀的指令，不再监听裸词避免冲突
            menu_triggers = {
                "helps_菜单", "helps_帮助", "helps_bot", "helps_说明", "helps_指令",
                "hmenu", "bhelp",
            }
            qz_triggers = {
                "helps_qz", "helps_空间",
            }

            if first in menu_triggers:
                # 去重 TTL 提升到 2.5s 防止双发
                if not self._dedup(event, "helps_menu", 2.5):
                    event.stop_event()
                    return
                event.stop_event()
                logger.debug(f"[fallback] {event.message_str!r} -> menu")
                async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
                    yield r
                return

            if first in qz_triggers:
                if not self._dedup(event, "helps_qz", 2.5):
                    event.stop_event()
                    return
                event.stop_event()
                logger.debug(f"[fallback] {event.message_str!r} -> qz")
                async for r in self.helps_qz(event):
                    yield r
                return
        except Exception as e:
            logger.debug(f"fallback异常: {e}", exc_info=True)
