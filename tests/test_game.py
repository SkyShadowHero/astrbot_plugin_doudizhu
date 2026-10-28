"""对局流程测试（不依赖 AstrBot）。

运行： python tests/test_game.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cards import Kind, parse_cards  # noqa: E402
from core.game import GameConfig, Phase  # noqa: E402
from core.manager import GameManager  # noqa: E402

PLATFORM = "default"
GROUP = "123456"


class FakeSender:
    """记下所有"发言"，供断言使用。"""

    def __init__(self) -> None:
        self.groups: list[tuple[str | None, str]] = []
        self.privates: list[tuple[str, str]] = []
        self.fail_private_to: set[str] = set()

    async def group(self, text: str, at: str | None = None) -> None:
        self.groups.append((at, text))

    async def private(self, uid: str, text: str) -> bool:
        if uid in self.fail_private_to:
            return False
        self.privates.append((uid, text))
        return True

    @property
    def all_text(self) -> str:
        return "\n".join(text for _, text in self.groups)

    def privates_of(self, uid: str) -> list[str]:
        return [text for who, text in self.privates if who == uid]


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.saved = 0

    async def load(self) -> dict[str, dict]:
        return dict(self.data)

    async def save(self, data: dict[str, dict]) -> None:
        self.data = dict(data)
        self.saved += 1


class GameFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sender = FakeSender()
        self.store = FakeStore()
        self.config = GameConfig(
            basic_bet=200,
            initial_coins=500,
            daily_allowance=500,
            turn_timeout=0,
        )
        self.manager = GameManager(self.config, self.store)

    async def asyncTearDown(self) -> None:
        await self.manager.terminate()

    # ------------------------------------------------------------------ 辅助
    async def say(self, uid: str, word: str, name: str | None = None, admin: bool = False) -> bool:
        return await self.manager.handle_word(
            platform_id=PLATFORM,
            group_id=GROUP,
            uid=uid,
            name=name or f"玩家{uid}",
            word=word,
            sender=self.sender,
            is_admin=admin,
        )

    async def cards(self, uid: str, text: str) -> bool:
        return await self.manager.handle_cards(
            platform_id=PLATFORM,
            group_id=GROUP,
            uid=uid,
            text=text,
            sender=self.sender,
        )

    async def setup_started_game(self):
        await self.say("1", "创建游戏")
        for uid in ("1", "2", "3"):
            await self.say(uid, "上桌")
        await self.say("1", "开始游戏")
        game = self.manager.find_game(PLATFORM, GROUP)
        self.assertIsNotNone(game)
        return game

    async def make_landlord(self, uid: str = "3"):
        """按顺序让前面的人「不抢」，最后 ``uid`` 抢地主，进入出牌阶段。"""
        game = self.manager.find_game(PLATFORM, GROUP)
        for player in game.table:
            if player.uid == uid:
                break
            await self.say(player.uid, "不抢")
        await self.say(uid, "抢地主")
        self.assertIs(game.phase, Phase.PLAYING)
        return game

    # ------------------------------------------------------------------ 用例
    async def test_create_and_join(self):
        self.assertTrue(await self.say("1", "创建游戏"))
        self.assertIn("创建成功", self.sender.all_text)

        # 重复创建
        await self.say("1", "创建游戏")
        self.assertIn("已经有一个游戏了", self.sender.all_text)

        await self.say("1", "上桌", name="甲")
        self.assertIn("加入成功", self.sender.all_text)
        self.assertIn("甲", self.sender.all_text)

        # 重复上桌
        await self.say("1", "上桌", name="甲")
        self.assertIn("你已经在桌上了", self.sender.all_text)

        await self.say("2", "上桌", name="乙")
        await self.say("3", "上桌", name="丙")
        # 第 4 个人
        await self.say("4", "上桌", name="丁")
        self.assertIn("人满了", self.sender.all_text)

    async def test_join_without_game(self):
        self.assertTrue(await self.say("9", "上桌"))
        self.assertIn("还没有游戏", self.sender.all_text)

    async def test_join_requires_enough_points(self):
        self.config.basic_bet = 1000
        self.config.initial_coins = 100
        manager = GameManager(
            self.config,
            self.store,
        )
        await manager.terminate()
        self.manager = manager
        await self.say("1", "创建游戏")
        await self.say("1", "上桌", name="穷鬼")
        self.assertIn("积分不够", self.sender.all_text)

    async def test_leave(self):
        await self.say("1", "创建游戏")
        await self.say("1", "上桌", name="甲")
        await self.say("2", "上桌", name="乙")
        await self.say("1", "下桌")
        self.assertIn("下桌成功", self.sender.all_text)
        game = self.manager.find_game(PLATFORM, GROUP)
        self.assertEqual(len(game.table), 1)

    async def test_start_requires_three(self):
        await self.say("1", "创建游戏")
        await self.say("1", "上桌", name="甲")
        await self.say("1", "开始游戏")
        self.assertIn("还没满人", self.sender.all_text)

    async def test_deal_and_bid(self):
        game = await self.setup_started_game()
        self.assertIs(game.phase, Phase.BIDDING)
        self.assertEqual(len(game.table), 3)
        self.assertEqual(len(game.bottom), 3)
        for player in game.table:
            self.assertEqual(len(player.hand), 17)
        # 每人收到自己的手牌
        for uid in ("1", "2", "3"):
            self.assertTrue(any("你的手牌" in text for text in self.sender.privates_of(uid)))

        # 1、2 不抢，3 抢地主
        await self.say("1", "不抢")
        await self.say("2", "不抢")
        game = await self.make_landlord()
        self.assertIs(game.phase, Phase.PLAYING)
        self.assertEqual(game.landlord, "3")
        landlord = game.find("3")
        self.assertEqual(len(landlord.hand), 20)
        self.assertEqual(game.current.uid, "3")
        self.assertIn("底牌是", self.sender.all_text)

    async def test_all_pass_redeals(self):
        game = await self.setup_started_game()
        first_hands = {p.uid: list(p.hand) for p in game.table}
        for uid in ("1", "2", "3"):
            await self.say(uid, "不抢")
        self.assertIs(game.phase, Phase.BIDDING)
        self.assertIn("重新发牌", self.sender.all_text)
        self.assertNotEqual(
            first_hands["1"],
            game.table[0].hand,
            "重新发牌后手牌应该变化",
        )

    async def test_not_your_turn(self):
        await self.setup_started_game()
        await self.make_landlord()
        # 现在轮到 3 出牌，1 抢先出牌会被拦下
        self.assertTrue(await self.cards("1", "3"))
        self.assertIn("还没轮到你出牌", "\n".join(self.sender.privates_of("1")))

    async def test_play_validation(self):
        await self.setup_started_game()
        game = await self.make_landlord()
        player = game.current
        hand_size = len(player.hand)

        # 手里不可能有 7 张 3
        await self.cards(player.uid, "3333333")
        # 回合没有前进
        self.assertEqual(game.current.uid, player.uid)
        # 出牌不成立时不会改变手牌数
        self.assertEqual(len(player.hand), hand_size)

    async def test_full_autoplay_until_settle(self):
        game = await self.setup_started_game()
        game = await self.make_landlord()
        landlord = game.landlord

        steps = 0
        while game.phase is Phase.PLAYING and steps < 300:
            steps += 1
            player = game.current
            previous = game.last_play[1] if game.last_play else None
            if previous is None:
                await game.play(player.uid, min(player.hand).label)
            elif previous.kind is Kind.SINGLE:
                candidates = [card for card in player.hand if card.value > previous.main]
                if candidates:
                    await game.play(player.uid, min(candidates).label)
                else:
                    await game.do_pass(player.uid)
            else:  # 理论上只会出现单牌
                await game.do_pass(player.uid)

        self.assertIs(game.phase, Phase.FINISHED, "对局应该正常结束")
        self.assertIsNone(self.manager.find_game(PLATFORM, GROUP), "结束后牌局应被移除")
        self.assertIn("剩余手牌", self.sender.all_text)

        # 零和：三家积分总和不变
        total = sum(data.coins for data in self.manager.players.values())
        self.assertEqual(total, 500 * 3)
        # 场次 +1，胜场只有一个
        for data in self.manager.players.values():
            self.assertEqual(data.games, 1)
        self.assertEqual(sum(data.wins for data in self.manager.players.values()), 1)
        # 地主输了双倍
        if game.find(landlord).data.wins == 1:
            self.assertEqual(game.find(landlord).data.coins, 500 + 400)
        else:
            self.assertEqual(game.find(landlord).data.coins, 500 - 400)
        # 数据已持久化
        self.assertGreaterEqual(self.store.saved, 1)
        self.assertEqual(len(self.store.data), 3)

    async def test_bomb_doubles(self):
        game = await self.setup_started_game()
        game = await self.make_landlord()
        player = game.current
        player.hand = list(parse_cards("3333444456789"))
        self.assertTrue(await self.cards(player.uid, "3333"))
        self.assertEqual(game.magnification, 2)
        self.assertIn("当前倍率：×2", self.sender.all_text)

    async def test_pass_resets_round(self):
        game = await self.setup_started_game()
        game = await self.make_landlord()
        player = game.current
        player.hand = list(parse_cards("3456789A"))
        other = [p for p in game.table if p.uid != player.uid]
        await self.cards(player.uid, "3")
        self.assertEqual(game.last_play[1].kind, Kind.SINGLE)
        await game.do_pass(other[0].uid)
        await game.do_pass(other[1].uid)
        self.assertIsNone(game.last_play)
        self.assertEqual(game.current.uid, player.uid)

        # 自由回合不能不出
        await game.do_pass(player.uid)
        self.assertIn("这是你的回合，不可以不出哦", self.sender.all_text)

    async def test_admin_end(self):
        game = await self.setup_started_game()
        await self.say("1", "结束游戏", admin=False)
        self.assertIn("只有管理员", self.sender.all_text)
        await self.say("1", "结束游戏", admin=True)
        self.assertIs(game.phase, Phase.FINISHED)
        self.assertIsNone(self.manager.find_game(PLATFORM, GROUP))

    async def test_whitelist(self):
        self.config.allowed_groups = ["999"]
        await self.say("1", "创建游戏")
        self.assertIn("还没有开启斗地主", self.sender.all_text)

    async def test_status_and_my_hand(self):
        game = await self.setup_started_game()
        await self.say("1", "当前玩家")
        self.assertIn("当前玩家：", self.sender.all_text)
        await self.say("1", "我的牌")
        self.assertTrue(any("你的手牌" in text for text in self.sender.privates_of("1")))

    async def test_private_messages(self):
        game = await self.setup_started_game()
        game = await self.make_landlord()
        player = game.current
        # 私聊看牌
        handled = await self.manager.handle_private(
            platform_id=PLATFORM, uid=player.uid, text="我的牌", sender=self.sender,
        )
        self.assertTrue(handled)
        # 私聊出牌
        smallest = min(player.hand)
        handled = await self.manager.handle_private(
            platform_id=PLATFORM, uid=player.uid, text=smallest.label, sender=self.sender,
        )
        self.assertTrue(handled)
        self.assertEqual(len(player.hand), 19)

    async def test_beg_and_me(self):
        # 群内口令「乞讨」等价于 /d beg
        self.assertTrue(await self.say("1", "乞讨", name="甲"))
        self.assertIn("500 积分", self.sender.all_text)

        # 同一天第二次领取被拒绝，且和 /d beg 共用冷却
        text = await self.manager.beg("1", "甲")
        self.assertIn("已经领过", text)
        self.assertTrue(await self.say("1", "乞讨", name="甲"))
        self.assertIn("已经领过", self.sender.all_text)

        # 私聊里也能乞讨
        handled = await self.manager.handle_private(
            platform_id=PLATFORM,
            uid="2",
            text="乞讨",
            sender=self.sender,
            name="乙",
        )
        self.assertTrue(handled)
        self.assertTrue(any("积分" in text for text in self.sender.privates_of("2")))

        # 群内口令「斗地主余额」等价于 /d me
        self.assertTrue(await self.say("1", "斗地主余额", name="甲"))
        self.assertIn("1000 积分", self.sender.all_text)
        self.assertIn("胜率", self.sender.all_text)

        text = await self.manager.me("1", "甲")
        self.assertIn("1000 积分", text)

    async def test_gift(self):
        # 新玩家默认 500，送 500 后变 1000
        balance = await self.manager.gift(target_uid="9", amount=500)
        self.assertEqual(balance, 1000)
        # 再送 2000
        balance = await self.manager.gift(target_uid="9", amount=2000)
        self.assertEqual(balance, 3000)
        # 负数 / 0 会被夹到 1
        balance = await self.manager.gift(target_uid="9", amount=0)
        self.assertEqual(balance, 3001)
        # 已经持久化，换一个 manager 也能读到
        fresh = GameManager(self.config, self.store)
        await fresh.ensure_loaded()
        self.assertEqual(fresh.player_data("9").coins, 3001)
        await fresh.terminate()

    async def test_persist_and_reload(self):
        await self.setup_started_game()
        game = await self.make_landlord()
        await self.manager.persist()
        fresh = GameManager(self.config, self.store)
        await fresh.ensure_loaded()
        self.assertEqual(set(fresh.players), {"1", "2", "3"})
        await fresh.terminate()


class TimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_auto_pass(self):
        sender = FakeSender()
        store = FakeStore()
        config = GameConfig(basic_bet=200, initial_coins=500, turn_timeout=1)
        manager = GameManager(config, store)
        for uid in ("1", "2", "3"):
            await manager.handle_word(
                platform_id=PLATFORM, group_id=GROUP, uid=uid, name=f"玩家{uid}",
                word="创建游戏", sender=sender,
            )
            await manager.handle_word(
                platform_id=PLATFORM, group_id=GROUP, uid=uid, name=f"玩家{uid}",
                word="上桌", sender=sender,
            )
        await manager.handle_word(
            platform_id=PLATFORM, group_id=GROUP, uid="1", name="玩家1",
            word="开始游戏", sender=sender,
        )
        game = manager.find_game(PLATFORM, GROUP)
        self.assertIsNotNone(game)
        first = game.current.uid

        await asyncio.sleep(1.3)

        self.assertIn("超时", sender.all_text)
        if game.phase is Phase.BIDDING:
            self.assertNotEqual(game.current.uid, first)
        await manager.terminate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
