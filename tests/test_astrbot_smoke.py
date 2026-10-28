"""在 AstrBot 真实环境下做一次冒烟测试。

需要 AstrBot 的 Python 环境（本机是 uv tool 里的 python），所以默认不在 CI 里跑：

    C:\\Users\\...\\uv\\tools\\astrbot\\Scripts\\python.exe tests/test_astrbot_smoke.py

它会：
1. 以包的形式导入 main.py（触发 filter/decorator 注册）；
2. 用假的 Context / 事件对象跑一遍「创建游戏 → 上桌 → 开始游戏 → 抢地主 → 出牌」；
3. 校验 _conf_schema.json、metadata.yaml 能被正常读取。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import types

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "doudizhu_smoke_pkg"

try:
    import astrbot  # noqa: F401
except Exception:  # pragma: no cover
    print("SKIP: 当前 Python 环境没有 astrbot")
    raise SystemExit(0)

if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# 把插件目录注册成一个包，模拟 AstrBot 的 data.plugins.<name> 加载方式
package = types.ModuleType(PACKAGE)
package.__path__ = [PLUGIN_DIR]
sys.modules[PACKAGE] = package

main = importlib.import_module(f"{PACKAGE}.main")


class FakeContext:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, umo: str, chain) -> None:
        self.sent.append((umo, chain))

    def texts(self) -> str:
        out = []
        for _, chain in self.sent:
            for comp in getattr(chain, "chain", []):
                text = getattr(comp, "text", None)
                if text:
                    out.append(text)
        return "\n".join(out)


class FakeEvent:
    def __init__(
        self,
        text: str,
        uid: str = "1",
        name: str = "甲",
        group: str = "123456",
        private: bool = False,
        wake: bool = True,
        admin: bool = False,
    ) -> None:
        self.message_str = text
        self.uid = uid
        self.name = name
        self.group = group
        self.private = private
        self.wake = wake
        self.admin = admin
        self.stopped = False
        self.sent: list = []
        self.extras: dict = {}
        self.components: list = []
        self.self_id = "999999"

    def get_messages(self):
        return self.components

    def get_self_id(self) -> str:
        return self.self_id

    def set_extra(self, key, value) -> None:
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    # --- AstrMessageEvent 中我们用到的部分 ---
    def get_platform_id(self) -> str:
        return "default"

    def get_group_id(self) -> str:
        return "" if self.private else self.group

    def get_sender_id(self) -> str:
        return self.uid

    def get_sender_name(self) -> str:
        return self.name

    def get_message_str(self) -> str:
        return self.message_str

    def is_private_chat(self) -> bool:
        return self.private

    def is_at_or_wake_command(self) -> bool:
        return self.wake

    def is_admin(self) -> bool:
        return self.admin

    def stop_event(self) -> None:
        self.stopped = True

    @property
    def unified_msg_origin(self) -> str:
        if self.private:
            return f"default:FriendMessage:{self.uid}"
        return f"default:GroupMessage:{self.group}_{self.uid}"

    def plain_result(self, text: str):
        from astrbot.core.message.message_event_result import MessageEventResult

        return MessageEventResult().message(text)

    async def send(self, chain) -> None:
        self.sent.append(chain)


async def run() -> None:
    context = FakeContext()
    plugin = main.DoudizhuPlugin(context, {"basic_bet": 200, "turn_timeout": 0})
    plugin.plugin_id = "dada/astrbot_plugin_doudizhu"

    store: dict = {}

    async def get_kv_data(key, default):
        return store.get(key, default)

    async def put_kv_data(key, value):
        store[key] = value

    plugin.get_kv_data = get_kv_data
    plugin.put_kv_data = put_kv_data

    await plugin.initialize()

    manager = main.get_manager()
    assert manager is not None, "插件没有初始化 GameManager"

    # 1. 创建游戏
    event = FakeEvent("创建游戏", uid="1", name="甲")
    await plugin.on_game_word(event)
    assert event.stopped, "「创建游戏」应该被消费"
    assert "创建成功" in context.texts(), context.texts()

    # 2. 上桌
    for uid, name in (("1", "甲"), ("2", "乙"), ("3", "丙")):
        await plugin.on_game_word(FakeEvent("上桌", uid=uid, name=name))
    assert "当前玩家：甲、乙、丙" in context.texts(), context.texts()

    # 3. 开始游戏
    await plugin.on_game_word(FakeEvent("开始游戏", uid="1", name="甲"))
    game = manager.find_game("default", "123456")
    assert game is not None

    # 4. 抢地主：1、2 不抢，3 抢
    await plugin.on_game_word(FakeEvent("不抢", uid="1"))
    await plugin.on_game_word(FakeEvent("不抢", uid="2"))
    await plugin.on_game_word(FakeEvent("抢地主", uid="3"))
    assert game.landlord == "3", game.landlord
    assert game.current.uid == "3"

    # 5. 出牌（走 on_play_cards，真实用法是「出3」这种无唤醒前缀的消息）
    player = game.current
    smallest = min(player.hand)
    hand_size = len(player.hand)
    event = FakeEvent(f"出{smallest.label}", uid="3", name="丙", wake=False)
    await plugin.on_play_cards(event)
    assert event.stopped, "出牌消息应该被消费"
    assert len(player.hand) == hand_size - 1, (hand_size, len(player.hand))

    # 6. 私聊「我的牌」
    event = FakeEvent("我的牌", uid="2", name="乙", private=True)
    await plugin.on_play_cards(event)
    assert any(umo == "default:FriendMessage:2" for umo, _ in context.sent), context.sent[-3:]

    # 7. 斜杠指令 /d beg、/d me、/d help
    results = [item async for item in plugin.slash_command(FakeEvent("d beg", uid="1"), "beg")]
    assert results and "积分" in results[0].get_plain_text(), results

    results = [item async for item in plugin.slash_command(FakeEvent("d me", uid="1"), "me")]
    assert results and "胜率" in results[0].get_plain_text(), results

    results = [item async for item in plugin.slash_command(FakeEvent("d help", uid="1"), "help")]
    assert results and "斗地主玩法" in results[0].get_plain_text(), results

    # 7.0 开关关掉后，/d 系列全部静默
    plugin.config["enable_slash_commands"] = False
    for event_args in ("beg", "me", "help", ""):
        event = FakeEvent(f"d {event_args}".strip(), uid="3", name="丙")
        results = [item async for item in plugin.slash_command(event, event_args)]
        assert results == [], f"关闭后 /d {event_args} 不应有响应"
        assert event.stopped, f"关闭后 /d {event_args} 应被静默消费"
    plugin.config["enable_slash_commands"] = True

    # 7.1 群内口令「乞讨」应该等价于 /d beg
    event = FakeEvent("乞讨", uid="2", name="乙")
    await plugin.on_game_word(event)
    assert event.stopped, "「乞讨」应该被消费"
    assert "积分" in context.texts(), context.texts()

    # 7.2「斗地主帮助」代替了 /d help
    event = FakeEvent("斗地主帮助", uid="1")
    await plugin.on_game_word(event)
    assert event.stopped, "「斗地主帮助」应该被消费"
    assert "斗地主玩法" in event.sent[0].get_plain_text(), event.sent

    # 7.3「斗地主余额」代替了 /d me
    event = FakeEvent("斗地主余额", uid="1", name="甲")
    await plugin.on_game_word(event)
    assert event.stopped, "「斗地主余额」应该被消费"
    assert "胜率" in context.texts(), context.texts()

    # 7.4 主人送钱：送钱@某人，固定 500
    from astrbot.api.message_components import At

    event = FakeEvent("送钱", uid="1", name="甲", admin=True)
    event.components = [At(qq="123456789")]
    await plugin.on_gift(event)
    assert event.stopped, "送钱应该被消费"
    assert "主人送了你 500 积分" in context.texts(), context.texts()
    assert manager.player_data("123456789").coins == 500 + 500

    # 7.5 带金额也只送 500
    event = FakeEvent("送钱 2000", uid="1", name="甲", admin=True)
    event.components = [At(qq="123456789")]
    await plugin.on_gift(event)
    assert "主人送了你 500 积分" in context.texts(), context.texts()
    assert manager.player_data("123456789").coins == 500 + 500 + 500

    # 7.6 非主人被拒
    event = FakeEvent("送钱", uid="2", name="乙")
    event.components = [At(qq="123456789")]
    await plugin.on_gift(event)
    assert "只有主人" in event.sent[0].get_plain_text(), event.sent
    assert manager.player_data("123456789").coins == 1500

    # 7.7 没 @ 人时给用法
    event = FakeEvent("送钱", uid="1", name="甲", admin=True)
    await plugin.on_gift(event)
    assert "用法" in event.sent[0].get_plain_text(), event.sent

    # 8. 检查注册到 AstrBot 的 handler 和过滤器
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(f"{PACKAGE}.main")
    by_name = {handler.handler_name: handler for handler in handlers}
    assert "on_game_word" in by_name, sorted(by_name)
    assert "on_play_cards" in by_name, sorted(by_name)
    assert "slash_command" in by_name, sorted(by_name)
    # /d beg、/d me、/d help 都收在 slash_command 一个 handler 里
    slash_filter = by_name["slash_command"].event_filters[0]
    assert set(slash_filter.handler_params) == {"args"}, slash_filter.handler_params
    assert slash_filter.handler_params["args"] is not None

    play_filters = {type(f).__name__ for f in by_name["on_play_cards"].event_filters}
    assert play_filters == {"RegexFilter", "InGameFilter"}, play_filters

    # 游戏进行中：牌面消息应该被放行；指令消息应该被放行
    assert all(
        f.filter(FakeEvent("出34", uid="1"), {})
        for f in by_name["on_play_cards"].event_filters
    )
    assert all(
        f.filter(FakeEvent("创建游戏", uid="1"), {})
        for f in by_name["on_game_word"].event_filters
    )
    # /d beg 的指令过滤器
    assert all(
        f.filter(FakeEvent("d beg", uid="1"), {})
        for f in by_name["slash_command"].event_filters
    )
    # 其它普通消息不会被斗地主吃掉
    card_regex = next(
        f for f in by_name["on_play_cards"].event_filters
        if type(f).__name__ == "RegexFilter"
    )
    assert not card_regex.filter(FakeEvent("hello", uid="1"), {})

    await plugin.terminate()
    assert main.get_manager() is None

    # 没有牌局时 InGameFilter 必须拦住，避免把群聊消息吃掉
    in_game_filter = main.InGameFilter(raise_error=False)
    assert not in_game_filter.filter(FakeEvent("34", uid="1"), {})

    # 8. 配置文件
    with open(os.path.join(PLUGIN_DIR, "_conf_schema.json"), encoding="utf-8") as fp:
        schema = json.load(fp)
    assert schema["basic_bet"]["type"] == "int"

    import yaml

    with open(os.path.join(PLUGIN_DIR, "metadata.yaml"), encoding="utf-8") as fp:
        meta = yaml.safe_load(fp)
    assert meta["name"] == "astrbot_plugin_doudizhu"

    print("OK: AstrBot 冒烟测试通过")
    print(f"    共发送 {len(context.sent)} 条消息")


def test_regex_filters() -> None:
    assert main.WORD_PATTERN.match("创建游戏")
    assert main.WORD_PATTERN.match("要不起")
    assert main.WORD_PATTERN.match("斗地主帮助")
    assert main.WORD_PATTERN.match("斗地主余额")
    assert not main.WORD_PATTERN.match("你好")
    # 出牌消息必须带「出」前缀，且不需要 "/" 唤醒前缀
    assert main.PLAY_PATTERN.match("出10JQKA")
    assert main.PLAY_PATTERN.match("出鬼王")
    assert main.PLAY_PATTERN.match("出 3 4 5 6 7")
    assert not main.PLAY_PATTERN.match("10JQKA")
    assert not main.PLAY_PATTERN.match("/10JQKA")
    assert not main.PLAY_PATTERN.match("hello")
    assert not main.PLAY_PATTERN.match("出hello")
    # 送钱：带 @ 之后正文可能是空、空格+金额、或直接跟金额
    assert main.GIFT_PATTERN.match("送钱")
    assert main.GIFT_PATTERN.match("送钱 1000")
    assert main.GIFT_PATTERN.match("送钱1000")
    assert not main.GIFT_PATTERN.match("送钱吗")


if __name__ == "__main__":
    test_regex_filters()
    asyncio.run(run())
