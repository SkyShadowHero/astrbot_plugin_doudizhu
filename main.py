"""AstrBot 斗地主插件。

移植自 mirai-console 插件 ``dada.douDiZhu``（https://github.com/kono-dada/doudizhu）。
原来的实现用协程阻塞等待玩家输入，在 AstrBot 里改为"消息驱动"的状态机：
每条消息推进一次状态，需要发言时通过 ``Context.send_message`` 主动发出。

玩法：
1. 群内发送「创建游戏」；
2. 群友发送「上桌」（3 人满员）；
3. 任意玩家发送「开始游戏」，bot 会私聊每人自己的 17 张牌；
4. 依次发送「抢地主」/「不抢」；
5. 出牌阶段发送「出34567」这样的消息出牌，或发送「要不起」过牌；
6. 一把结束后自动结算 积分，可继续「创建游戏」再来一局。

所有指令都不占用 "/" 唤醒前缀，直接发中文即可，避免和别的机器人 / QQ 官方指令冲突。
积分相关：「乞讨」领每日低保，「斗地主余额」查看积分与胜率；
主人可以用「送钱@某人」（固定 500 积分）给玩家发钱。
兼容旧写法：/d beg、/d me、/d help，可用配置项 enable_slash_commands 整体关闭。
"""

from __future__ import annotations

import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.custom_filter import CustomFilter

from .core.game import GameConfig, Sender
from .core.manager import GAME_WORDS, PLAY_PREFIX, GameManager

#: 帮助口令
HELP_WORD = "斗地主帮助"

#: 群内可以裸发（不需要唤醒前缀）的游戏指令
WORD_PATTERN = re.compile(r"^(?:" + "|".join((*GAME_WORDS, HELP_WORD)) + r")$")

#: 出牌消息：「出」+ 3-9、10、J/Q/K/A、2、鬼（小王）、王（大王），例如 出34567
#: 「出」后面允许夹空格，如「出 3 4 5 6 7」
PLAY_PATTERN = re.compile(
    rf"^{PLAY_PREFIX}\s*[0-9jqkatJQKAT鬼王][\s0-9jqkatJQKAT鬼王]{{0,38}}$",
)

#: 主人送钱：送钱@某人（固定 500 积分）
GIFT_WORD = "送钱"
GIFT_PATTERN = re.compile(rf"^{GIFT_WORD}(?:\s.*|\d+)?$")
GIFT_AMOUNT = 500

HELP_TEXT = (
    "斗地主玩法：\n"
    "1. 群内发送「创建游戏」\n"
    "2. 群友发送「上桌」，满 3 人\n"
    "3. 发送「开始游戏」，机器人会私聊发牌\n"
    "4. 依次发送「抢地主」或「不抢」\n"
    "5. 出牌发送「出34567」这样的消息，过牌发送「要不起」\n"
    "6. 出完所有牌即获胜，自动结算积分\n"
    "其他：「下桌」「当前玩家」「我的牌」「结束游戏」（仅管理员）\n"
    "积分：「乞讨」领每日低保，「斗地主余额」查看积分与胜率\n"
    "主人：送钱@某人（固定 500 积分）"
)

SLASH_HINT = (
    "\n也可用斜杠指令：/d beg 领低保、/d me 查积分、/d help 看玩法"
    "\n（斜杠指令可在插件配置里整体关闭）"
)

_manager: GameManager | None = None
"""插件级的对局管理器，供 CustomFilter 访问。"""


def get_manager() -> GameManager | None:
    return _manager


class InGameFilter(CustomFilter):
    """只在"当前会话有进行中的对局"时放行 handler。

    这样群里普通的数字 / “不要” 之类消息不会被斗地主插件吃掉，
    也不会打扰到 LLM 聊天。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        manager = get_manager()
        if manager is None:
            return False
        group_id = event.get_group_id()
        if group_id:
            return manager.find_game(event.get_platform_id(), group_id) is not None
        return manager.find_game_by_player(str(event.get_sender_id())) is not None


class KVPlayerStore:
    """用 AstrBot 插件级 KV 存储保存 积分 数据。"""

    KEY = "player_data"

    def __init__(self, plugin: Star) -> None:
        self.plugin = plugin

    async def load(self) -> dict[str, dict]:
        data = await self.plugin.get_kv_data(self.KEY, {})
        return data if isinstance(data, dict) else {}

    async def save(self, data: dict[str, dict]) -> None:
        await self.plugin.put_kv_data(self.KEY, data)


class AstrBotSender:
    """把 ``core.game.Sender`` 的发言请求翻译成 AstrBot 主动消息。"""

    def __init__(
        self,
        plugin: Star,
        platform_id: str,
        group_umo: str | None = None,
        dm_event: AstrMessageEvent | None = None,
    ) -> None:
        self.plugin = plugin
        self.platform_id = platform_id
        self.group_umo = group_umo
        #: 当前消息本身来自私聊时带上事件，回复直接走这个会话（跨平台最稳）
        self.dm_event = dm_event

    async def group(self, text: str, at: str | None = None) -> None:
        if not self.group_umo:
            logger.warning("斗地主：缺少群会话 umo，丢弃消息：%s", text)
            return
        chain = MessageChain()
        if at:
            chain.chain.append(Comp.At(qq=at))
            chain.chain.append(Comp.Plain(" "))
        chain.message(text)
        try:
            await self.plugin.context.send_message(self.group_umo, chain)
        except Exception as exc:
            logger.error("斗地主：群消息发送失败: %s", exc)
            if at:
                # 有的平台不支持 At 消息段，退化成纯文本再试一次
                try:
                    await self.plugin.context.send_message(
                        self.group_umo,
                        MessageChain().message(text),
                    )
                except Exception as exc2:
                    logger.error("斗地主：群消息（纯文本）发送失败: %s", exc2)

    async def private(self, uid: str, text: str) -> bool:
        # 这条消息就来自该玩家的私聊：直接回到当前会话。
        # QQ/Telegram 可以靠 FriendMessage:{uid} 猜出会话，但 Discord 等平台的
        # 私聊会话 id 不等于用户 id，猜出来的 umo 发不出去。
        if self.dm_event is not None and str(uid) == str(self.dm_event.get_sender_id()):
            try:
                await self.dm_event.send(MessageChain().message(text))
                return True
            except Exception as exc:
                logger.warning("斗地主：私聊 %s 发送失败: %s", uid, exc)
                return False

        umo = f"{self.platform_id}:FriendMessage:{uid}"
        try:
            await self.plugin.context.send_message(umo, MessageChain().message(text))
            return True
        except Exception as exc:
            logger.warning("斗地主：私聊 %s 发送失败: %s", uid, exc)
            return False


class DoudizhuPlugin(Star):
    """斗地主插件主体。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        global _manager
        self.config = config if config is not None else {}
        _manager = GameManager(self._build_game_config(self.config), KVPlayerStore(self))

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        manager = get_manager()
        if manager is not None:
            await manager.ensure_loaded()

    async def terminate(self) -> None:
        global _manager
        if _manager is not None:
            await _manager.terminate()
            _manager = None

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_list(value) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [part for part in re.split(r"[,，\s]+", value) if part]
        return []

    def _build_game_config(self, raw: dict) -> GameConfig:
        return GameConfig(
            basic_bet=max(1, self._as_int(raw.get("basic_bet"), 200)),
            initial_coins=max(0, self._as_int(raw.get("initial_coins"), 500)),
            daily_allowance=max(0, self._as_int(raw.get("daily_allowance"), 500)),
            turn_timeout=max(0, self._as_int(raw.get("turn_timeout"), 120)),
            admin_ids=self._as_list(raw.get("admin_ids")),
            allowed_groups=self._as_list(raw.get("allowed_groups")),
        )

    def _slash_enabled(self) -> bool:
        """斜杠指令（/d beg）总开关，默认开启。"""
        value = self.config.get("enable_slash_commands", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开", "启用"}
        return bool(value)

    def _help_text(self) -> str:
        return HELP_TEXT + (SLASH_HINT if self._slash_enabled() else "")

    # ------------------------------------------------------------------ 内部工具
    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        name = (event.get_sender_name() or "").strip()
        if not name:
            name = str(event.get_sender_id())
        return name[:16]

    def _sender_for(self, event: AstrMessageEvent) -> Sender:
        if event.get_group_id():
            return AstrBotSender(self, event.get_platform_id(), event.unified_msg_origin)
        # 私聊：带上事件本身，回复直接回到当前私聊会话。
        # 这样 Discord 这类"私聊会话 id ≠ 用户 id"的平台也能用「我的牌」/私聊出牌。
        return AstrBotSender(self, event.get_platform_id(), None, dm_event=event)

    # ------------------------------------------------------------------ 游戏指令
    @filter.regex(WORD_PATTERN)
    async def on_game_word(self, event: AstrMessageEvent):
        """群聊里的「创建游戏 / 上桌 / 抢地主 / 要不起 / 斗地主帮助」等指令。"""
        word = event.message_str.strip()
        if word == HELP_WORD:
            await event.send(event.plain_result(self._help_text()))
            event.stop_event()
            return

        manager = get_manager()
        if manager is None:
            return
        uid = str(event.get_sender_id())
        name = self._sender_name(event)
        sender = self._sender_for(event)
        try:
            if event.get_group_id():
                handled = await manager.handle_word(
                    platform_id=event.get_platform_id(),
                    group_id=event.get_group_id(),
                    uid=uid,
                    name=name,
                    word=word,
                    sender=sender,
                    is_admin=event.is_admin(),
                )
            else:
                handled = await manager.handle_private(
                    platform_id=event.get_platform_id(),
                    uid=uid,
                    text=word,
                    sender=sender,
                    name=name,
                )
        except Exception as exc:
            logger.error("斗地主：处理「%s」失败: %s", word, exc, exc_info=True)
            await event.send(event.plain_result("斗地主插件出了点问题，请稍后再试～"))
            event.stop_event()
            return
        if handled:
            event.stop_event()

    @filter.regex(PLAY_PATTERN)
    @filter.custom_filter(InGameFilter)
    async def on_play_cards(self, event: AstrMessageEvent):
        """出牌：发送「出34567」这样的消息，群聊（无需唤醒前缀）和私聊都支持。"""
        manager = get_manager()
        if manager is None:
            return
        text = event.message_str.strip()
        uid = str(event.get_sender_id())
        sender = self._sender_for(event)
        try:
            if event.is_private_chat():
                handled = await manager.handle_private(
                    platform_id=event.get_platform_id(),
                    uid=uid,
                    text=text,
                    sender=sender,
                    name=self._sender_name(event),
                )
            else:
                handled = await manager.handle_cards(
                    platform_id=event.get_platform_id(),
                    group_id=event.get_group_id(),
                    uid=uid,
                    text=text,
                    sender=sender,
                )
        except Exception as exc:
            logger.error("斗地主：处理出牌「%s」失败: %s", text, exc, exc_info=True)
            await event.send(event.plain_result("斗地主插件出了点问题，请稍后再试～"))
            event.stop_event()
            return
        if handled:
            event.stop_event()

    # ------------------------------------------------------------------ 主人送钱
    @filter.regex(GIFT_PATTERN)
    async def on_gift(self, event: AstrMessageEvent):
        """送钱@某人：主人给指定玩家加 500 积分。"""
        manager = get_manager()
        if manager is None:
            return

        uid = str(event.get_sender_id())
        if not manager.is_admin(uid, event.is_admin()):
            await event.send(event.plain_result("只有主人可以送钱哦"))
            event.stop_event()
            return

        target = self._find_at(event)
        if not target:
            await event.send(
                event.plain_result(
                    f"用法：{GIFT_WORD}@某人\n例如：{GIFT_WORD}@张三（固定送 {GIFT_AMOUNT} 积分）",
                ),
            )
            event.stop_event()
            return

        balance = await manager.gift(target_uid=target, amount=GIFT_AMOUNT)
        if event.get_group_id():
            await self._sender_for(event).group(
                f"💰 主人送了你 {GIFT_AMOUNT} 积分，现在一共有 {balance} 积分",
                at=target,
            )
        else:
            await event.send(
                event.plain_result(
                    f"已给 {target} 送 {GIFT_AMOUNT} 积分，他现在有 {balance} 积分",
                ),
            )
        event.stop_event()

    @staticmethod
    def _find_at(event: AstrMessageEvent) -> str:
        """取消息里第一个被 @ 的 QQ（跳过 @全体 和 @bot 自己）。"""
        self_id = str(event.get_self_id() or "")
        for comp in event.get_messages():
            if not isinstance(comp, Comp.At):
                continue
            qq = str(getattr(comp, "qq", "") or "")
            if qq.isdigit() and qq != self_id:
                return qq
        return ""

    # ------------------------------------------------------------------ 斜杠指令（统一开关）
    @filter.command("d")
    async def slash_command(self, event: AstrMessageEvent, args: GreedyStr):
        """斜杠指令：/d beg、/d me、/d help（可在插件配置里整体关闭）。

        关掉之后本插件不再提供任何 "/" 入口，中文口令照常工作。
        """
        if not self._slash_enabled():
            # 静默消费，避免 /d 之类的消息再被交给 LLM 或其它插件
            event.stop_event()
            return

        manager = get_manager()
        if manager is None:
            return

        sub = str(args).strip().lower()
        uid = str(event.get_sender_id())
        name = self._sender_name(event)

        if sub in {"", "help", "帮助"}:
            yield event.plain_result(self._help_text())
            return
        if sub == "beg":
            yield event.plain_result(await manager.beg(uid, name))
            return
        if sub == "me":
            yield event.plain_result(await manager.me(uid, name))
            return

        yield event.plain_result(
            "未知子指令，可用：/d beg 领低保、/d me 查积分、/d help 看玩法",
        )
