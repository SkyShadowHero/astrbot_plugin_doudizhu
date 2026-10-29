"""对局管理器：按群维护牌局、玩家积分与持久化。

它是 AstrBot 适配层（``main.py``）和纯逻辑层（``game.py`` / ``cards.py``）之间的桥梁：
- 输入都是字符串 / 基础类型，输出都通过 ``Sender`` 发出，方便测试；
- 玩家 积分 数据由 AstrBot 的 KV 存储持久化（见 main.py 里的 PlayerStore 实现）。
"""

from __future__ import annotations

import asyncio
from typing import Protocol

try:  # pragma: no cover - 正常运行一定走这里（AstrBot 环境）
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 只有脱离 AstrBot 跑单元测试时才走到
    class _NullLogger:
        """没有 AstrBot 环境时的空日志器，保证 core 可以独立测试。"""

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    logger = _NullLogger()

from .game import Game, GameConfig, Phase, PlayerData, Sender, today_index

#: 群聊里可以直接说的游戏指令（不需要 "/" 前缀，移植自原插件的写法）
GAME_WORDS: tuple[str, ...] = (
    "创建游戏",
    "上桌",
    "下桌",
    "开始游戏",
    "结束游戏",
    "抢地主",
    "我抢",
    "抢",
    "不抢",
    "要不起",
    "不要",
    "过",
    "当前玩家",
    "我的牌",
    "乞讨",
    "斗地主余额",
)

PASS_WORDS = frozenset({"要不起", "不要", "过"})
GRAB_WORDS = frozenset({"抢地主", "我抢", "抢"})
BEG_WORDS = frozenset({"乞讨"})
"""和 ``/d beg`` 等价的群内口令。"""
ME_WORDS = frozenset({"斗地主余额"})
"""和 ``/d me`` 等价的群内口令。"""
GROUP_ONLY_WORDS = frozenset(
    {"创建游戏", "上桌", "下桌", "开始游戏", "结束游戏", "当前玩家"},
)

PLAY_PREFIX = "出"
"""出牌消息的前缀，例如「出34567」。"""


def strip_play_prefix(text: str) -> str:
    """去掉出牌消息开头的「出」和多余空白；没有前缀时原样返回。"""
    text = (text or "").strip()
    if text.startswith(PLAY_PREFIX):
        return text[len(PLAY_PREFIX) :].strip()
    return text


class PlayerStore(Protocol):
    """积分 数据持久化接口。"""

    async def load(self) -> dict[str, dict]:
        """读取 {用户 id: 数据字典}。"""

    async def save(self, data: dict[str, dict]) -> None:
        """整体写入。"""


class GameManager:
    """所有群的牌局与所有玩家的 积分 都在这里。"""

    def __init__(self, config: GameConfig, store: PlayerStore) -> None:
        self.config = config
        self.store = store
        self.players: dict[str, PlayerData] = {}
        self.games: dict[str, Game] = {}
        self._loaded = False
        self._save_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 数据
    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = await self.store.load()
        except Exception as exc:  # 读取失败不能影响开新局
            logger.error("读取斗地主玩家数据失败: %s", exc)
            return
        for uid, item in (raw or {}).items():
            if isinstance(item, dict):
                self.players[str(uid)] = PlayerData.from_dict(item)

    def player_data(self, uid: str) -> PlayerData:
        data = self.players.get(uid)
        if data is None:
            data = PlayerData(coins=self.config.initial_coins)
            self.players[uid] = data
        return data

    async def persist(self) -> None:
        snapshot = {uid: data.to_dict() for uid, data in self.players.items()}
        async with self._save_lock:
            try:
                await self.store.save(snapshot)
            except Exception as exc:
                logger.error("保存斗地主玩家数据失败: %s", exc)

    # ------------------------------------------------------------------ 牌局
    @staticmethod
    def game_key(platform_id: str, group_id: str) -> str:
        return f"{platform_id}:{group_id}"

    def find_game(self, platform_id: str, group_id: str) -> Game | None:
        return self.games.get(self.game_key(platform_id, group_id))

    def find_game_by_player(self, uid: str) -> Game | None:
        for game in self.games.values():
            if game.find(uid) is not None:
                return game
        return None

    def _drop(self, key: str) -> None:
        game = self.games.pop(key, None)
        if game is not None:
            game.close()

    async def terminate(self) -> None:
        for game in list(self.games.values()):
            game.close()
        self.games.clear()

    # ------------------------------------------------------------------ 入口
    async def handle_word(
        self,
        *,
        platform_id: str,
        group_id: str,
        uid: str,
        name: str,
        word: str,
        sender: Sender,
        is_admin: bool = False,
    ) -> bool:
        """处理群聊里的游戏指令；返回是否消费了这条消息。"""
        await self.ensure_loaded()

        if word == "创建游戏":
            await self._create(platform_id, group_id, sender)
            return True

        # 「乞讨」和 /d beg 等价，不依赖是否有牌局
        if word in BEG_WORDS:
            await sender.group(await self.beg(uid, name or uid))
            return True

        # 「斗地主余额」和 /d me 等价，也不依赖是否有牌局
        if word in ME_WORDS:
            await sender.group(await self.me(uid, name or uid))
            return True

        game = self.find_game(platform_id, group_id)
        if game is None:
            if word == "上桌":
                await sender.group("本群还没有游戏，先发送「创建游戏」开一局吧")
                return True
            return False

        if word == "上桌":
            await game.join(uid, name)
        elif word == "下桌":
            await game.leave(uid)
        elif word == "开始游戏":
            await game.start()
        elif word in GRAB_WORDS:
            await game.bid(uid, True)
        elif word == "不抢":
            await game.bid(uid, False)
        elif word in PASS_WORDS:
            await game.do_pass(uid)
        elif word == "结束游戏":
            if not self.is_admin(uid, is_admin):
                await sender.group("只有管理员可以结束游戏哦")
            else:
                await game.force_end(name or uid)
        elif word == "当前玩家":
            await game.show_status()
        elif word == "我的牌":
            await game.show_hand(uid)
        else:
            return False
        return True

    async def handle_cards(
        self,
        *,
        platform_id: str,
        group_id: str,
        uid: str,
        text: str,
        sender: Sender,
    ) -> bool:
        """处理群聊里的出牌请求（如「出10JQKA」）。"""
        await self.ensure_loaded()
        game = self.find_game(platform_id, group_id)
        if game is None:
            return False
        return await game.play(uid, strip_play_prefix(text))

    async def handle_private(
        self,
        *,
        platform_id: str,
        uid: str,
        text: str,
        sender: Sender,
        name: str = "",
    ) -> bool:
        """处理私聊消息：私聊里可以看牌 / 出牌 / 不出，也可以乞讨。"""
        await self.ensure_loaded()
        game = self.find_game_by_player(uid)
        text = strip_play_prefix(text)

        if text in BEG_WORDS:
            await sender.private(uid, await self.beg(uid, name or uid))
            return True

        if text in ME_WORDS:
            await sender.private(uid, await self.me(uid, name or uid))
            return True

        if text == "我的牌":
            if game is None:
                await sender.private(uid, "你当前没有进行中的对局")
            else:
                await game.show_hand(uid, reply=sender)
            return True

        if text in GROUP_ONLY_WORDS:
            await sender.private(uid, "斗地主要在群聊里玩哦，把 bot 拉进群再试试吧")
            return True

        if game is None:
            return False
        if game.phase is not Phase.PLAYING:
            return False
        if text in PASS_WORDS:
            return await game.do_pass(uid, reply=sender)
        return await game.play(uid, text, reply=sender)

    # ------------------------------------------------------------------ 内部
    async def _create(self, platform_id: str, group_id: str, sender: Sender) -> None:
        key = self.game_key(platform_id, group_id)
        if self.config.allowed_groups and group_id not in self.config.allowed_groups:
            await sender.group(
                "本群还没有开启斗地主，请管理员在插件配置的 allowed_groups 里加上本群号",
            )
            return
        if key in self.games:
            await sender.group("已经有一个游戏了")
            return
        game = Game(
            platform_id=platform_id,
            group_id=group_id,
            sender=sender,
            player_data=self.players,
            config=self.config,
            on_change=self.persist,
            on_finish=lambda: self._drop(key),
        )
        self.games[key] = game
        await sender.group(
            f"创建成功（底分：{self.config.basic_bet}）！发送「上桌」即可参与游戏",
        )

    def is_admin(self, uid: str, event_is_admin: bool = False) -> bool:
        return event_is_admin or uid in {str(item) for item in self.config.admin_ids}

    # ------------------------------------------------------------------ 积分
    async def beg(self, uid: str, name: str) -> str:
        """每日低保。"""
        await self.ensure_loaded()
        data = self.player_data(uid)
        today = today_index()
        if data.last_allowance_day == today:
            return (
                f"{name} 你今天已经领过 {self.config.daily_allowance} 积分了，"
                "别得寸进尺了哦！"
            )
        data.coins += self.config.daily_allowance
        data.last_allowance_day = today
        await self.persist()
        return (
            f"又输光了吗……喏，这是 {self.config.daily_allowance} 积分，别再输了哦\n"
            f"{name} 现在有 {data.coins} 积分"
        )

    async def me(self, uid: str, name: str) -> str:
        await self.ensure_loaded()
        data = self.player_data(uid)
        return (
            f"{name} 现在有 {data.coins} 积分，"
            f"总共进行了 {data.games} 场游戏，获胜 {data.wins} 场，"
            f"胜率 {data.win_rate * 100:.1f}%"
        )

    async def gift(self, *, target_uid: str, amount: int) -> int:
        """主人送钱：给指定玩家加 积分，返回送完后的余额。"""
        await self.ensure_loaded()
        data = self.player_data(target_uid)
        data.coins += max(1, int(amount))
        await self.persist()
        return data.coins
