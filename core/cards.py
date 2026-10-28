"""斗地主规则核心：牌、牌型解析与大小比较。

移植自 mirai 插件 dada.douDiZhu（https://github.com/kono-dada/doudizhu），
在保留原有牌型的基础上修正了几处判定问题（王炸比较、飞机带牌、四带两对等）。

本模块不依赖 AstrBot，可以脱离 bot 单独运行与测试。
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

# 牌值：1..13 依次对应 3..2，14 小鬼，15 大王。
RANK_LABELS: dict[int, str] = {
    1: "3",
    2: "4",
    3: "5",
    4: "6",
    5: "7",
    6: "8",
    7: "9",
    8: "10",
    9: "J",
    10: "Q",
    11: "K",
    12: "A",
    13: "2",
    14: "鬼",
    15: "王",
}

MAX_CHAIN_VALUE = 12
"""顺子 / 连对 / 飞机最大只能以 A（12）收尾，不能用 2 或王。"""

JOKER_SMALL = 14
JOKER_BIG = 15


@dataclass(frozen=True, order=True)
class Card:
    """一张牌，只记录大小，牌面由 value 映射得到。"""

    value: int

    @property
    def label(self) -> str:
        return RANK_LABELS[self.value]

    def __str__(self) -> str:
        return f"[{self.label}]"


CARD_BY_VALUE: dict[int, Card] = {value: Card(value) for value in RANK_LABELS}


def card(value: int) -> Card:
    """按牌值取牌，例如 card(1) 是 3，card(15) 是大王。"""
    return CARD_BY_VALUE[value]


# 解析玩家输入的牌面。注意："小王" 必须先于 "王" 匹配，"10" 必须先于单字符匹配。
_PARSE_TOKENS: tuple[tuple[str, int], ...] = (
    ("小王", JOKER_SMALL),
    ("大王", JOKER_BIG),
    ("10", 8),
    ("j", 9),
    ("q", 10),
    ("k", 11),
    ("a", 12),
    ("t", 8),
    ("3", 1),
    ("4", 2),
    ("5", 3),
    ("6", 4),
    ("7", 5),
    ("8", 6),
    ("9", 7),
    ("2", 13),
    ("鬼", JOKER_SMALL),
    ("王", JOKER_BIG),
)

_SEPARATORS = (" ", "\t", "\u3000", ",", "，", "、", "+")


def parse_cards(text: str) -> tuple[Card, ...] | None:
    """把 "10jqka" 这类字符串解析成牌列表；出现无法识别的字符时返回 None。

    支持 3-9、10（也接受 t）、J/Q/K/A、2、鬼（小王）、王（大王）以及 小王/大王。
    大小写不敏感，允许用空格或逗号分隔。
    """
    raw = (text or "").strip().lower()
    for sep in _SEPARATORS:
        raw = raw.replace(sep, "")
    if not raw:
        return None

    cards: list[Card] = []
    index = 0
    while index < len(raw):
        for token, value in _PARSE_TOKENS:
            if raw.startswith(token, index):
                cards.append(card(value))
                index += len(token)
                break
        else:
            return None
    return tuple(cards) if cards else None


def new_deck() -> list[Card]:
    """一副 54 张的牌。"""
    deck = [card(value) for value in range(1, 14) for _ in range(4)]
    deck.append(card(JOKER_SMALL))
    deck.append(card(JOKER_BIG))
    return deck


def shuffled_deck(rng: random.Random | None = None) -> list[Card]:
    deck = new_deck()
    (rng or random).shuffle(deck)
    return deck


def format_cards(cards: Iterable[Card]) -> str:
    """按大小排序后拼成 [3][3][4] 的形式。"""
    return "".join(str(item) for item in sorted(cards))


def _is_consecutive(values: list[int]) -> bool:
    return all(values[i + 1] - values[i] == 1 for i in range(len(values) - 1))


class Kind(Enum):
    """牌型。"""

    INVALID = "invalid"
    SINGLE = "single"
    PAIR = "pair"
    TRIPLE = "triple"
    TRIPLE_ONE = "triple_one"
    TRIPLE_PAIR = "triple_pair"
    STRAIGHT = "straight"
    PAIR_STRAIGHT = "pair_straight"
    TRIPLE_STRAIGHT = "triple_straight"
    PLANE_ONE = "plane_one"
    PLANE_PAIR = "plane_pair"
    FOUR_ONE = "four_one"
    FOUR_TWO = "four_two"
    FOUR_TWO_PAIR = "four_two_pair"
    BOMB = "bomb"
    ROCKET = "rocket"


KIND_NAMES: dict[Kind, str] = {
    Kind.INVALID: "不合法牌型",
    Kind.SINGLE: "单牌",
    Kind.PAIR: "对子",
    Kind.TRIPLE: "三张",
    Kind.TRIPLE_ONE: "三带一",
    Kind.TRIPLE_PAIR: "三带二",
    Kind.STRAIGHT: "顺子",
    Kind.PAIR_STRAIGHT: "连对",
    Kind.TRIPLE_STRAIGHT: "飞机",
    Kind.PLANE_ONE: "飞机带单",
    Kind.PLANE_PAIR: "飞机带对",
    Kind.FOUR_ONE: "四带一",
    Kind.FOUR_TWO: "四带二",
    Kind.FOUR_TWO_PAIR: "四带两对",
    Kind.BOMB: "炸弹",
    Kind.ROCKET: "王炸",
}


@dataclass(frozen=True)
class Combo:
    """一手牌。

    ``main`` 用于比较大小：单张取牌值，顺子/连对/飞机取最大的那组牌值。
    同样长度（``size``）的顺子类牌型比较 max 与比较 min 的结果一致，
    所以统一用 max 即可。
    """

    kind: Kind
    main: int
    size: int
    cards: tuple[Card, ...]

    @property
    def valid(self) -> bool:
        return self.kind is not Kind.INVALID

    @property
    def is_bomb(self) -> bool:
        return self.kind in (Kind.BOMB, Kind.ROCKET)

    def name(self) -> str:
        return KIND_NAMES[self.kind]

    def text(self) -> str:
        return format_cards(self.cards)

    def beats(self, other: Combo | None) -> bool:
        """这手牌能否压过 ``other``（None 表示自由出牌）。"""
        if not self.valid:
            return False
        if other is None or not other.valid:
            return True
        if self.kind is Kind.ROCKET:
            return other.kind is not Kind.ROCKET
        if other.kind is Kind.ROCKET:
            return False
        if self.kind is Kind.BOMB and other.kind is not Kind.BOMB:
            return True
        if other.kind is Kind.BOMB and self.kind is not Kind.BOMB:
            return False
        if self.kind is not other.kind:
            return False
        if self.size != other.size:
            return False
        return self.main > other.main

    def __str__(self) -> str:
        return self.text()


def _treble_runs(counts: Counter[int], length: int) -> list[list[int]]:
    """返回所有长度 ``length``、连续、且不超过 A 的三张链候选。"""
    values = sorted(
        value
        for value, count in counts.items()
        if count >= 3 and value <= MAX_CHAIN_VALUE
    )
    runs: list[list[int]] = []
    for start in range(len(values) - length + 1):
        window = values[start : start + length]
        if _is_consecutive(window):
            runs.append(window)
    return runs


def _is_pair_tail(counts: Counter[int]) -> bool:
    return bool(counts) and all(count == 2 for count in counts.values())


def detect(cards: Iterable[Card]) -> Combo:
    """识别牌型；无法识别时返回 ``Kind.INVALID``。"""
    ordered = tuple(sorted(cards))
    size = len(ordered)
    if size == 0:
        return Combo(Kind.INVALID, 0, 0, ordered)

    counts = Counter(item.value for item in ordered)
    values = sorted(counts)

    # 王炸
    if size == 2 and counts.get(JOKER_SMALL) == 1 and counts.get(JOKER_BIG) == 1:
        return Combo(Kind.ROCKET, JOKER_BIG, 2, ordered)

    if size == 1:
        return Combo(Kind.SINGLE, values[0], 1, ordered)

    if size == 2:
        if len(values) == 1:
            return Combo(Kind.PAIR, values[0], 2, ordered)
        return Combo(Kind.INVALID, 0, size, ordered)

    if size == 3:
        if len(values) == 1:
            return Combo(Kind.TRIPLE, values[0], 3, ordered)
        return Combo(Kind.INVALID, 0, size, ordered)

    if size == 4:
        if len(values) == 1:
            return Combo(Kind.BOMB, values[0], 4, ordered)
        triple = next((value for value, count in counts.items() if count == 3), None)
        if triple is not None:
            return Combo(Kind.TRIPLE_ONE, triple, 4, ordered)
        return Combo(Kind.INVALID, 0, size, ordered)

    if size == 5:
        triple = next((value for value, count in counts.items() if count == 3), None)
        if triple is not None and sorted(counts.values()) == [2, 3]:
            return Combo(Kind.TRIPLE_PAIR, triple, 5, ordered)
        quad = next((value for value, count in counts.items() if count == 4), None)
        if quad is not None:
            return Combo(Kind.FOUR_ONE, quad, 5, ordered)

    # 顺子
    if (
        size >= 5
        and all(count == 1 for count in counts.values())
        and values[-1] <= MAX_CHAIN_VALUE
        and _is_consecutive(values)
    ):
        return Combo(Kind.STRAIGHT, values[-1], size, ordered)

    # 连对
    if (
        size >= 6
        and size % 2 == 0
        and all(count == 2 for count in counts.values())
        and values[-1] <= MAX_CHAIN_VALUE
        and _is_consecutive(values)
    ):
        return Combo(Kind.PAIR_STRAIGHT, values[-1], size, ordered)

    # 纯飞机（连三，不带翅膀）
    if (
        size >= 6
        and size % 3 == 0
        and all(count == 3 for count in counts.values())
        and values[-1] <= MAX_CHAIN_VALUE
        and _is_consecutive(values)
    ):
        return Combo(Kind.TRIPLE_STRAIGHT, values[-1], size, ordered)

    # 四带二 / 四带两对
    quad = next((value for value, count in counts.items() if count == 4), None)
    if quad is not None:
        if size == 6:
            return Combo(Kind.FOUR_TWO, quad, 6, ordered)
        if size == 8:
            rest = Counter({value: count for value, count in counts.items() if value != quad})
            if _is_pair_tail(rest) and len(rest) == 2:
                return Combo(Kind.FOUR_TWO_PAIR, quad, 8, ordered)

    # 飞机带单 / 飞机带对
    for unit, kind in ((4, Kind.PLANE_ONE), (5, Kind.PLANE_PAIR)):
        if size % unit != 0:
            continue
        run_length = size // unit
        if run_length < 2:
            continue
        for run in _treble_runs(counts, run_length):
            rest = Counter(counts)
            for value in run:
                rest[value] -= 3
            rest = +rest
            leftover = sum(rest.values())
            if leftover != size - 3 * run_length:
                continue
            if kind is Kind.PLANE_ONE and leftover == run_length:
                return Combo(kind, run[-1], size, ordered)
            if (
                kind is Kind.PLANE_PAIR
                and leftover == 2 * run_length
                and _is_pair_tail(rest)
                and len(rest) == run_length
            ):
                return Combo(kind, run[-1], size, ordered)

    return Combo(Kind.INVALID, 0, size, ordered)


def hand_has(hand: Iterable[Card], cards: Iterable[Card]) -> bool:
    """手牌里是否包含选中的牌（按数量比对，重复的牌需要有多张）。"""
    pool = Counter(item.value for item in hand)
    for item in cards:
        pool[item.value] -= 1
        if pool[item.value] < 0:
            return False
    return True


def remove_cards(hand: list[Card], cards: Iterable[Card]) -> None:
    """从手牌中移除选中的牌。调用前需保证 ``hand_has`` 为真。"""
    pool = Counter(item.value for item in cards)
    kept: list[Card] = []
    for item in hand:
        if pool[item.value] > 0:
            pool[item.value] -= 1
        else:
            kept.append(item)
    hand[:] = kept
