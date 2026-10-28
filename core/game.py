"""斗地主对局状态机。

移植自 mirai 插件 ``dada.douDiZhu.Game``：原始实现用协程 + 事件订阅等待玩家输入，
在 AstrBot 里改成"消息驱动"的状态机——每条消息进来时推进状态、把要发的话交给
``Sender`` 发出去。

本模块不依赖 AstrBot，方便脱离 bot 做单元测试。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Protocol

from .cards import (
    Card,
    Combo,
    detect,
    format_cards,
    hand_has,
    parse_cards,
    remove_cards,
    shuffled_deck,
)


class Sender(Protocol):
    """消息发送接口，由 AstrBot 适配层实现，测试里用假的实现。"""

    async def group(self, text: str, at: str | None = None) -> None:
        """在游戏所在群发言，``at`` 为要 @ 的 QQ 号。"""

    async def private(self, uid: str, text: str) -> bool:
        """私聊某个玩家，返回是否发送成功。"""


@dataclass
class PlayerData:
    """跨群持久化的玩家数据（积分 / 胜负场次 / 低保时间）。"""

    coins: int = 0
    wins: int = 0
    games: int = 0
    last_allowance_day: int = -1

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    def to_dict(self) -> dict:
        return {
            "coins": self.coins,
            "wins": self.wins,
            "games": self.games,
            "last_allowance_day": self.last_allowance_day,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> PlayerData:
        def as_int(key: str, default: int) -> int:
            try:
                return int(raw.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            coins=as_int("coins", 0),
            wins=as_int("wins", 0),
            games=as_int("games", 0),
            last_allowance_day=as_int("last_allowance_day", -1),
        )


@dataclass
class GameConfig:
    """插件配置。"""

    basic_bet: int = 200
    initial_coins: int = 500
    daily_allowance: int = 500
    turn_timeout: int = 120
    admin_ids: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)


class Phase(Enum):
    WAITING = "waiting"  # 等人上桌
    BIDDING = "bidding"  # 抢地主
    PLAYING = "playing"  # 出牌
    FINISHED = "finished"  # 已结算


@dataclass
class Player:
    uid: str
    name: str
    data: PlayerData
    hand: list[Card] = field(default_factory=list)

    @property
    def display(self) -> str:
        return self.name or self.uid


class Game:
    """一局斗地主。所有"回复"都通过 ``sender`` 发出。"""

    def __init__(
        self,
        platform_id: str,
        group_id: str,
        sender: Sender,
        player_data: dict[str, PlayerData],
        config: GameConfig,
        on_change: Callable[[], Awaitable[None]],
        on_finish: Callable[[], None],
    ) -> None:
        self.platform_id = platform_id
        self.group_id = group_id
        self.sender = sender
        self._player_data = player_data
        self.config = config
        self.on_change = on_change
        self.on_finish = on_finish

        self.table: list[Player] = []
        self.phase = Phase.WAITING
        self.bottom: list[Card] = []
        self.landlord: str | None = None
        self.bid_index = 0
        self.turn_index = 0
        self.last_play: tuple[str, Combo] | None = None
        self.pass_count = 0
        self.magnification = 1

        self._timer: asyncio.Task | None = None
        self._turn_token = 0

    # ------------------------------------------------------------------ 工具
    @property
    def key(self) -> str:
        return f"{self.platform_id}:{self.group_id}"

    @property
    def finished(self) -> bool:
        return self.phase is Phase.FINISHED

    def find(self, uid: str) -> Player | None:
        return next((player for player in self.table if player.uid == uid), None)

    @property
    def current(self) -> Player | None:
        if self.phase is Phase.BIDDING and self.table:
            return self.table[self.bid_index % len(self.table)]
        if self.phase is Phase.PLAYING and self.table:
            return self.table[self.turn_index % len(self.table)]
        return None

    def names(self) -> str:
        return "、".join(player.display for player in self.table)

    def data_for(self, uid: str) -> PlayerData:
        data = self._player_data.get(uid)
        if data is None:
            data = PlayerData(coins=self.config.initial_coins)
            self._player_data[uid] = data
        return data

    def close(self) -> None:
        self._cancel_timer()

    # ------------------------------------------------------------------ 准备
    async def join(self, uid: str, name: str) -> None:
        if self.phase is not Phase.WAITING:
            await self.sender.group("游戏已经开始了，等这一局结束再来吧")
            return
        if self.find(uid):
            await self.sender.group(f"{name} 你已经在桌上了哦")
            return
        if len(self.table) >= 3:
            await self.sender.group("人满了或你已经在游戏中了，无法加入")
            return
        data = self.data_for(uid)
        if data.coins < self.config.basic_bet:
            await self.sender.group(
                f"{name} 你的积分不够 {self.config.basic_bet} 哦，"
                "先发「乞讨」领个低保吧",
            )
            return
        self.table.append(Player(uid=uid, name=name, data=data))
        await self.sender.group(f"加入成功\n当前玩家：{self.names()}")

    async def leave(self, uid: str) -> None:
        player = self.find(uid)
        if player is None:
            await self.sender.group("你不在桌上哦")
            return
        if self.phase is not Phase.WAITING:
            await self.sender.group("游戏已经开始了，现在不能下桌")
            return
        self.table.remove(player)
        await self.sender.group(f"{player.display} 下桌成功\n当前玩家：{self.names() or '（空）'}")

    async def start(self) -> None:
        if self.phase is not Phase.WAITING:
            return
        if len(self.table) < 3:
            await self.sender.group(f"还没满人，无法开始\n当前玩家：{self.names()}")
            return
        await self._deal()

    # ------------------------------------------------------------------ 发牌
    async def _deal(self) -> None:
        deck = shuffled_deck()
        hands = [deck[0:17], deck[17:34], deck[34:51]]
        self.bottom = deck[51:54]
        for player, hand in zip(self.table, hands):
            player.hand = sorted(hand)

        self.phase = Phase.BIDDING
        self.bid_index = 0
        self.turn_index = 0
        self.landlord = None
        self.last_play = None
        self.pass_count = 0
        self.magnification = 1

        await self.sender.group("发牌完毕！每人 17 张，底牌 3 张。")
        for player in self.table:
            await self._send_hand(player, "你的手牌")
        await self._prompt_bid()
        self._arm_timer()

    async def _send_hand(self, player: Player, title: str) -> None:
        ok = await self.sender.private(
            player.uid,
            f"{title}（{len(player.hand)} 张）：\n{format_cards(player.hand)}",
        )
        if not ok:
            await self.sender.group(
                f"⚠️ 私聊 {player.display} 发送手牌失败，请先加 bot 好友后发送「我的牌」查看",
            )

    async def _inform(self, player: Player, text: str) -> None:
        """优先私聊告诉某个玩家；私聊失败就 @ 他发在群里。"""
        if not await self.sender.private(player.uid, text):
            await self.sender.group(text, at=player.uid)

    # ------------------------------------------------------------------ 抢地主
    async def _prompt_bid(self) -> None:
        player = self.current
        if player is None:
            return
        await self.sender.group(
            "轮到你抢地主了，是否要抢？（发送「抢地主」或「不抢」）",
            at=player.uid,
        )

    async def bid(self, uid: str, take: bool) -> bool:
        if self.phase is not Phase.BIDDING:
            return False
        player = self.current
        if player is None or player.uid != uid:
            if player is not None and self.find(uid) is not None:
                await self.sender.group(f"还没轮到你抢地主哦，现在是 {player.display}", at=uid)
                return True
            return False

        if take:
            self.landlord = uid
            player.hand = sorted(player.hand + self.bottom)
            self.phase = Phase.PLAYING
            self.turn_index = next(
                index for index, item in enumerate(self.table) if item.uid == uid
            )
            self.last_play = None
            self.pass_count = 0
            await self.sender.group(f"{player.display} 当上了地主！", at=player.uid)
            await self.sender.group(f"底牌是{format_cards(self.bottom)}")
            await self.sender.private(
                uid,
                f"你当上了地主，底牌是{format_cards(self.bottom)}\n"
                f"你现在有 {len(player.hand)} 张牌：\n{format_cards(player.hand)}",
            )
            await self._prompt_play()
            self._arm_timer()
            return True

        await self.sender.group(f"{player.display} 不抢")
        self.bid_index += 1
        if self.bid_index >= len(self.table):
            await self.sender.group("三家都不抢地主，重新发牌～")
            await self._deal()
            return True
        await self._prompt_bid()
        self._arm_timer()
        return True

    # ------------------------------------------------------------------ 出牌
    async def _prompt_play(self) -> None:
        player = self.current
        if player is None:
            return
        if self.last_play is None:
            hint = "自由出牌，发送「出+牌」，例如 出34567"
        else:
            combo = self.last_play[1]
            hint = (
                f"上家出了{combo.text()}（{combo.name()}），"
                "发送「出…」压过它，或发送「要不起」"
            )
        await self.sender.group(f"轮到你出牌了：{hint}", at=player.uid)

    async def play(self, uid: str, text: str) -> bool:
        """处理一次出牌请求；返回是否"消费"了这条消息。"""
        if self.phase is not Phase.PLAYING:
            return False
        player = self.current
        if player is None or player.uid != uid:
            target = self.find(uid)
            if target is not None:
                await self._inform(target, "还没轮到你出牌哦")
                return True
            return False

        cards = parse_cards(text)
        if not cards:
            await self._inform(player, "没看懂你想要出什么牌啦")
            return True

        combo = detect(cards)
        if not combo.valid:
            await self._inform(player, "没看懂你想要出什么牌啦")
            return True
        if not hand_has(player.hand, cards):
            await self._inform(player, "没在你的牌中找到你想出的牌哦")
            return True

        previous = self.last_play[1] if self.last_play else None
        if previous is not None and not combo.beats(previous):
            await self._inform(
                player,
                f"你出的牌貌似不符合规则哦：上家是{previous.text()}（{previous.name()}）",
            )
            return True

        remove_cards(player.hand, cards)
        self.last_play = (uid, combo)
        self.pass_count = 0

        if combo.is_bomb:
            self.magnification *= 2
            await self.sender.group(f"💣 {player.display} 出了{combo.text()}（{combo.name()}）")
            await self.sender.group(f"当前倍率：×{self.magnification}")
        else:
            await self.sender.group(f"{player.display} 出了{combo.text()}（{combo.name()}）")

        if not player.hand:
            await self._settle(player)
            return True

        if len(player.hand) <= 2:
            await self.sender.group(
                f"⚠️ {player.display} 只剩 {len(player.hand)} 张牌了，感觉要赢了！",
            )
        await self._send_hand(player, "你还剩")

        self._advance_turn()
        await self._prompt_play()
        self._arm_timer()
        return True

    async def do_pass(self, uid: str) -> bool:
        if self.phase is not Phase.PLAYING:
            return False
        player = self.current
        if player is None or player.uid != uid:
            target = self.find(uid)
            if target is not None:
                await self._inform(target, "还没轮到你，先别急着过牌哦")
                return True
            return False
        if self.last_play is None:
            await self.sender.group("这是你的回合，不可以不出哦", at=uid)
            return True

        self.pass_count += 1
        await self.sender.group(f"{player.display} 选择了不出")
        self._advance_turn()
        if self.pass_count >= 2:
            self.pass_count = 0
            self.last_play = None
            nxt = self.current
            if nxt is not None:
                await self.sender.group(f"两家都不要，{nxt.display} 可以重新出牌了", at=nxt.uid)
        await self._prompt_play()
        self._arm_timer()
        return True

    def _advance_turn(self) -> None:
        self.turn_index = (self.turn_index + 1) % len(self.table)

    # ------------------------------------------------------------------ 结算
    async def _settle(self, winner: Player) -> None:
        self.phase = Phase.FINISHED
        self._cancel_timer()

        amount = self.config.basic_bet * self.magnification
        for player in self.table:
            player.data.games += 1
        winner.data.wins += 1

        landlord = self.find(self.landlord) if self.landlord else None
        farmers = [player for player in self.table if player.uid != self.landlord]
        lines: list[str] = []

        if landlord is not None and winner.uid == landlord.uid:
            landlord.data.coins += amount * 2
            for farmer in farmers:
                farmer.data.coins -= amount
            lines.append("🎉 地主赢了！")
            lines.append(f"{landlord.display} 赢得 {amount * 2} 积分")
            lines.append(
                "、".join(farmer.display for farmer in farmers) + f" 各输掉 {amount} 积分",
            )
        else:
            if landlord is not None:
                landlord.data.coins -= amount * 2
            for farmer in farmers:
                farmer.data.coins += amount
            lines.append("🌾 农民赢了！")
            if landlord is not None:
                lines.append(f"{landlord.display} 输掉 {amount * 2} 积分")
            lines.append(
                "、".join(farmer.display for farmer in farmers) + f" 各赢得 {amount} 积分",
            )

        lines.append(f"（底分 {self.config.basic_bet} × 倍率 {self.magnification}）")
        lines.append("剩余手牌：")
        for player in self.table:
            left = format_cards(player.hand) or "（已出完）"
            lines.append(f"{player.display}：{left}")
        await self.sender.group("\n".join(lines))

        await self.on_change()
        self.on_finish()

    async def force_end(self, by_name: str) -> None:
        self.phase = Phase.FINISHED
        self._cancel_timer()
        await self.sender.group(f"{by_name} 结束了本局游戏")
        self.on_finish()

    # ------------------------------------------------------------------ 查询
    async def show_hand(self, uid: str) -> None:
        player = self.find(uid)
        if player is None:
            await self.sender.group("你不在本局游戏中哦")
            return
        if self.phase is Phase.WAITING:
            await self.sender.group(f"{player.display} 还没发牌呢，等人齐了再开始吧")
            return
        if not player.hand:
            await self.sender.group(f"{player.display} 你的牌已经出完啦")
            return
        await self._send_hand(player, "你的手牌")

    async def show_status(self) -> None:
        if not self.table:
            await self.sender.group("当前还没有玩家上桌，发送「上桌」加入")
            return
        lines = [f"当前玩家：{self.names()}"]
        if self.phase is Phase.WAITING:
            lines.append(f"已上桌 {len(self.table)}/3 人，满 3 人后发送「开始游戏」")
        else:
            if self.landlord:
                landlord = self.find(self.landlord)
                if landlord is not None:
                    lines.append(f"地主：{landlord.display}")
            lines.append(f"当前倍率：×{self.magnification}")
            if self.phase in (Phase.BIDDING, Phase.PLAYING):
                current = self.current
                if current is not None:
                    action = "抢地主" if self.phase is Phase.BIDDING else "出牌"
                    lines.append(f"轮到：{current.display}（{action}）")
        await self.sender.group("\n".join(lines))

    # ------------------------------------------------------------------ 超时
    def _arm_timer(self) -> None:
        self._cancel_timer()
        if self.config.turn_timeout <= 0:
            return
        self._turn_token += 1
        token = self._turn_token
        self._timer = asyncio.create_task(self._wait_timeout(token))

    def _cancel_timer(self) -> None:
        self._turn_token += 1
        task = self._timer
        self._timer = None
        if task is not None and not task.done():
            task.cancel()

    async def _wait_timeout(self, token: int) -> None:
        try:
            await asyncio.sleep(self.config.turn_timeout)
        except asyncio.CancelledError:
            return
        if token != self._turn_token:
            return
        # 当前任务即将结束，先摘掉引用，避免 _auto_move 里的 _cancel_timer 取消自己。
        self._timer = None
        await self._auto_move()

    async def _auto_move(self) -> None:
        player = self.current
        if player is None:
            return
        if self.phase is Phase.BIDDING:
            await self.sender.group(f"⏰ {player.display} 超时，自动不抢")
            await self.bid(player.uid, False)
            return
        if self.phase is not Phase.PLAYING:
            return
        if self.last_play is None:
            if not player.hand:
                return
            smallest = min(player.hand)
            await self.sender.group(f"⏰ {player.display} 超时，自动出{smallest}")
            await self.play(player.uid, smallest.label)
        else:
            await self.sender.group(f"⏰ {player.display} 超时，自动不出")
            await self.do_pass(player.uid)


def today_index() -> int:
    """按本地时区算出的"第几天"，用于每日低保（本地 0 点刷新）。"""
    return datetime.now().astimezone().date().toordinal()
