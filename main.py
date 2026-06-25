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

_QQNT_BLOCKED = ("1200", "Timeout", "NodeIKernelMsgService")


def _is_blocked(err: Exception) -> bool:
    return any(kw in str(err) for kw in _QQNT_BLOCKED)


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
                enable_waiting_message=False, ignored_plugins=set(), custom_font_path="",
                rendering=RenderingConfig(10, 30, 2, 1500, 16383, 16000, 144),
                appearance=AppearanceConfig("default", {"default": ThemePreset("default", [], {})}),
                qzone_share=QzoneShareConfig(True, "https://h5.qzone.qq.com/ugc/share/?res_uin=2562925383&cellid=4723c398fea8b26971e70500", "Bot使用说明", "点击查看详细功能贴", "", "text")
            )

        raw_path = self.plugin_config.custom_font_path
        self.user_font_dir = Path(raw_path) if raw_path and raw_path.strip() else self.data_dir / "fonts"
        try: self.user_font_dir.mkdir(parents=True, exist_ok=True)
        except: pass

        self.builtin_font_dir = self.plugin_dir / "resources" / InternalCFG.NAME_FONT_DIR
        self.font_dirs = [self.builtin_font_dir, self.user_font_dir]
        self.font_manager = FontManager(self.font_dirs)
        self.layout = TypstLayout(self.plugin_config)
        self.hint = HelpHint()
        self.msg = MsgRecall()
        self.renderer = TypstRenderer(star=self, data_dir=self.data_dir, template_path=self.template_path, font_dirs=self.font_dirs, config=self.plugin_config)
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
        except: pass

        qz = getattr(self.plugin_config, "qzone_share", None)
        logger.info(f"[HelpTypst V11] {self._plugin_id} qzone={getattr(qz, 'enable', False)}/{getattr(qz, 'mode', '?')}")

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
        except: pass

    async def _perform_cleanup(self):
        try:
            for f in self.data_dir.glob("temp_*"):
                try:
                    if f.exists(): f.unlink()
                except OSError: pass
        except Exception as e:
            logger.warning(f"清理失败: {e}")

    def _dedup(self, event: AstrMessageEvent, key: str, ttl: float = 2.5) -> bool:
        try: uid = f"{event.get_sender_id()}:{key}:{event.get_group_id() or 'p'}"
        except: uid = key
        now = time.time()
        last = self._last_send.get(uid, 0)
        if now - last < ttl:
            logger.debug(f"[HelpTypst] 去重拦截 {uid} {now-last:.2f}s")
            return False
        self._last_send[uid] = now
        return True

    async def _try_ark(self, event, card: dict) -> bool:
        """尝试发送 Ark 卡片，返回是否成功"""
        try:
            await event.send(MessageChain([Json(data=card)]))
            return True
        except Exception:
            pass
        try:
            bot = getattr(event, "bot", None)
            if bot:
                is_grp = bool(event.get_group_id())
                tgt = event.get_group_id() or event.get_sender_id()
                if tgt and str(tgt).isdigit():
                    tgt = int(tgt)
                    msg = [{"type": "json", "data": {"data": js.dumps(card, ensure_ascii=False, separators=(',', ':'))}}]
                    if is_grp: await bot.call_action("send_group_msg", group_id=tgt, message=msg)
                    else: await bot.call_action("send_private_msg", user_id=tgt, message=msg)
                    return True
        except Exception:
            pass
        return False

    async def _send_qzone_link(self, event: AstrMessageEvent) -> bool:
        """V11 策略：先试 Ark，失败/被拦截则文本兜底，确保 100% 可达"""
        try: qz = self.plugin_config.qzone_share
        except Exception as e:
            logger.warning(f"[QZone] 读配置失败: {e}")
            return False
        if not getattr(qz, "enable", False): return False
        url = (getattr(qz, "url", "") or "").strip()
        if not url.startswith("http"): return False

        mode = (getattr(qz, "mode", "text") or "text").lower()
        title = (getattr(qz, "title", "") or "Bot使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        preview = (getattr(qz, "image", "") or "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png").strip()

        # 尝试 Ark（仅当 mode=json/ark/auto）
        ark_sent = False
        if mode in ("json", "ark", "auto"):
            try:
                ctime = int(time.time())
                card = {
                    "app": "com.tencent.structmsg",
                    "config": {"autosize": True, "forward": True, "type": "normal", "ctime": ctime, "token": ""},
                    "desc": "QQ空间", "extra": {"app_type": 1, "appid": 100358126},
                    "meta": {"news": {"title": title, "desc": content, "jumpUrl": url, "preview": preview, "tag": "QQ空间"}},
                    "prompt": f"[分享] {title}", "ver": "0.0.0.1", "view": "news"
                }
                card["config"]["token"] = self._gen_token(card)
                ark_sent = await self._try_ark(event, card)
            except Exception as e:
                logger.debug(f"[QZone] Ark 异常: {e}")

        # 文本兜底：无论 Ark 成功与否都发，确保 URL 可达
        try:
            txt = f"📖 {title}\n{content}\n👉 {url}"
            await event.send(MessageChain([Plain(txt)]))
            return True
        except Exception as e:
            logger.error(f"[QZone] 文本发送失败: {e}", exc_info=True)
            return ark_sent

    def _gen_token(self, card_dict: dict) -> str:
        """生成 Ark 卡片签名 token（MD5 伪签名，QQNT 可能不认但聊胜于无）"""
        raw = js.dumps(card_dict, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.md5(f"qzoneshare{raw}{int(time.time())}".encode()).hexdigest()

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
            self.layout.dump_layout_json(plugins=plugins, save_path=save_path, title=display_title, mode=mode, prefixes=self.prefixes, font_list=final_font_list)
            return len(plugins)

        result, error = await self.renderer.render(data_pipeline, mode, query)
        if wait_msg_id: await self.msg.recall(event, wait_msg_id)

        if result:
            try:
                if with_qzone_share:
                    await self._send_qzone_link(event)
                    await asyncio.sleep(0.2)
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

    # ============ 指令：V11 精简版 ============
    async def _run_menu(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.cmd_analyzer, "AstrBot 指令菜单", "command", query, with_qzone_share=True):
            yield r

    # 核心指令（合并重复装饰器）
    @filter.command("helps")
    @filter.command("helps_菜单")
    @filter.command("helps_帮助")
    async def helps_menu(self, event: AstrMessageEvent, query: str = ""):
        if not self._dedup(event, "helps_menu", 2.5): return
        logger.info(f"[helps] {event.get_sender_id()} q={query!r}")
        async for r in self._run_menu(event, query):
            yield r

    @filter.command("helps_事件")
    async def helps_events(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.evt_analyzer, "AstrBot 事件监听", "event", query, with_qzone_share=False):
            yield r

    @filter.command("helps_过滤器")
    async def helps_filters(self, event: AstrMessageEvent, query: str = ""):
        async for r in self._handle_request(event, self.flt_analyzer, "AstrBot 过滤器分析", "filter", query, with_qzone_share=False):
            yield r

    @filter.command("qztest")
    async def qztest(self, event: AstrMessageEvent):
        if not self._dedup(event, "qztest", 3.0): return
        logger.info("[qztest] 诊断开始")
        qz = getattr(self.plugin_config, "qzone_share", None)
        if not qz or not getattr(qz, "enable", False):
            yield event.plain_result("❌ QZone 未启用\n配置 qzone_share.enable = true")
            return
        url = (getattr(qz, "url", "") or "").strip()
        mode = (getattr(qz, "mode", "text") or "text").lower()
        title = (getattr(qz, "title", "") or "Bot 使用说明").strip()
        content = (getattr(qz, "content", "") or "点击查看详细功能贴").strip()
        ctime = int(time.time())
        log = [
            "🧪 qztest 诊断报告", "━━━━━━━━━━━━━━━",
            f"模式: {mode}", f"URL: {url[:60]}...", f"标题: {title}", f"内容: {content[:30]}...", f"时间戳: {ctime}"
        ]
        card = {
            "app": "com.tencent.structmsg", "config": {"autosize": True, "forward": True, "type": "normal", "ctime": ctime, "token": ""},
            "desc": "QQ 空间", "extra": {"app_type": 1, "appid": 100358126},
            "meta": {"news": {"title": title, "desc": content, "jumpUrl": url, "preview": "https://qzonestyle.gtimg.cn/aoi/sola/shared/img/qzone_logo.png", "tag": "QQ 空间"}},
            "prompt": f"[分享] {title}", "ver": "0.0.0.1", "view": "news"
        }
        card["config"]["token"] = self._gen_token(card)
        log.append(f"Token: {card['config']['token'][:16]}...")
        log.append("┅┅┅┅┅┅┅┅┅┅┅┅┅┅┅")
        sent = "失败"
        try:
            await event.send(MessageChain([Json(data=card)]))
            log.append("✅ Json 组件: 调用成功"); sent = "Json"
        except Exception as e:
            err = str(e)
            if any(k in err for k in _QQNT_BLOCKED): log.append(f"❌ Json: QQNT 拦截 → {err[:80]}")
            else: log.append(f"❌ Json: {err[:80]}")
            try:
                bot = getattr(event, "bot", None)
                if bot:
                    is_grp = bool(event.get_group_id())
                    tgt = event.get_group_id() or event.get_sender_id()
                    if tgt and str(tgt).isdigit():
                        tgt = int(tgt)
                        msg = [{"type": "json", "data": {"data": js.dumps(card, ensure_ascii=False, separators=(',', ':'))}}]
                        if is_grp: await bot.call_action("send_group_msg", group_id=tgt, message=msg)
                        else: await bot.call_action("send_private_msg", user_id=tgt, message=msg)
                        log.append("✅ NapCat API: 调用成功"); sent = "NapCat"
            except Exception as e2:
                err2 = str(e2)
                if any(k in err2 for k in _QQNT_BLOCKED): log.append(f"❌ NapCat: QQNT 拦截 → {err2[:80]}")
                else: log.append(f"❌ NapCat: {err2[:80]}")
        log.extend(["━━━━━━━━━━━━━━━", f"通道: {sent}"])
        if sent == "失败":
            log.extend(["", "📌 QQNT 3.2+ 拦截未签名 Ark 卡片", "   非插件 bug，NapCat 层面限制", "", "🔧 建议:", "   1. 等 NapCat 修复 issue #1700", "   2. mode=text 稳定可用", "   3. 邪修路线: 接入真实签名服务器"])
        else:
            log.extend(["", "✅ 已发出，若显示'版本过低'说明缺合法签名"])
        yield event.plain_result("\n".join(log))

    # ---------- fallback ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _fallback(self, event: AstrMessageEvent):
        try:
            txt_raw = (event.message_str or "").strip()
            if not txt_raw: return
            txt = txt_raw.lower()
            # 去前缀
            for p in self.prefixes + ["/", "!", "！", ".", "。", "#", "！", " "]:
                if p and txt.startswith(p.lower()):
                    txt = txt[len(p):].lstrip()
                    break
            if not txt: return
            first = txt.split()[0]
            query = txt[len(first):].strip()

            # 菜单触发（包含 helps 裸词）
            menu_triggers = {"helps", "helps_菜单", "helps_帮助"}
            if first in menu_triggers:
                if not self._dedup(event, "helps_menu", 2.5):
                    event.stop_event(); return
                event.stop_event()
                logger.debug(f"[fallback] {event.message_str!r} -> menu")
                async for r in self._run_menu(event, query):
                    yield r
                return
        except Exception as e:
            logger.debug(f"fallback 异常: {e}", exc_info=True)
