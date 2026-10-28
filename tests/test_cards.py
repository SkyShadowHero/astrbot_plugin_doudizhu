"""牌型解析与比较的单元测试（不依赖 AstrBot）。

运行： python tests/test_cards.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cards import (  # noqa: E402
    Kind,
    detect,
    format_cards,
    hand_has,
    new_deck,
    parse_cards,
    remove_cards,
)


def combo(text: str):
    cards = parse_cards(text)
    assert cards is not None, f"无法解析：{text}"
    return detect(cards)


class ParseTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(format_cards(parse_cards("3")), "[3]")
        self.assertEqual(format_cards(parse_cards("10")), "[10]")
        self.assertEqual(format_cards(parse_cards("jqka")), "[J][Q][K][A]")
        self.assertEqual(format_cards(parse_cards("JQKA")), "[J][Q][K][A]")
        self.assertEqual(format_cards(parse_cards("2鬼王")), "[2][鬼][王]")

    def test_multi_char_and_separator(self):
        self.assertEqual(format_cards(parse_cards("10 10 J")), "[10][10][J]")
        self.assertEqual(format_cards(parse_cards("小王 大王")), "[鬼][王]")

    def test_invalid(self):
        self.assertIsNone(parse_cards(""))
        self.assertIsNone(parse_cards("xyz"))
        self.assertIsNone(parse_cards("1"))
        self.assertIsNone(parse_cards("3炸"))

    def test_deck(self):
        deck = new_deck()
        self.assertEqual(len(deck), 54)
        # 牌只按大小区分，所以 54 张牌里只有 15 种不同的 "牌"
        self.assertEqual(len(set(deck)), 15)
        for value in range(1, 14):
            self.assertEqual(sum(1 for item in deck if item.value == value), 4)


class DetectTest(unittest.TestCase):
    def test_simple_kinds(self):
        self.assertIs(combo("3").kind, Kind.SINGLE)
        self.assertIs(combo("33").kind, Kind.PAIR)
        self.assertIs(combo("333").kind, Kind.TRIPLE)
        self.assertIs(combo("3333").kind, Kind.BOMB)
        self.assertIs(combo("鬼王").kind, Kind.ROCKET)
        self.assertIs(combo("3334").kind, Kind.TRIPLE_ONE)
        self.assertIs(combo("33344").kind, Kind.TRIPLE_PAIR)
        self.assertIs(combo("33334").kind, Kind.FOUR_ONE)
        self.assertIs(combo("333345").kind, Kind.FOUR_TWO)
        self.assertIs(combo("33334455").kind, Kind.FOUR_TWO_PAIR)

    def test_straight(self):
        self.assertIs(combo("34567").kind, Kind.STRAIGHT)
        self.assertIs(combo("10JQKA").kind, Kind.STRAIGHT)
        # 只有 4 张，不成顺子
        self.assertIs(combo("3456").kind, Kind.INVALID)
        # 2 不能参与顺子
        self.assertIs(combo("JQKA2").kind, Kind.INVALID)
        self.assertIs(combo("A2345").kind, Kind.INVALID)

    def test_pair_straight(self):
        self.assertIs(combo("334455").kind, Kind.PAIR_STRAIGHT)
        self.assertIs(combo("QQKKAA").kind, Kind.PAIR_STRAIGHT)
        # 两对不够
        self.assertIs(combo("3344").kind, Kind.INVALID)
        self.assertIs(combo("KKAA22").kind, Kind.INVALID)

    def test_triple_straight(self):
        self.assertIs(combo("333444").kind, Kind.TRIPLE_STRAIGHT)
        self.assertIs(combo("333444555").kind, Kind.TRIPLE_STRAIGHT)
        self.assertIs(combo("333444555666").kind, Kind.TRIPLE_STRAIGHT)
        self.assertIs(combo("333555").kind, Kind.INVALID)

    def test_plane(self):
        self.assertIs(combo("33344456").kind, Kind.PLANE_ONE)
        self.assertIs(combo("3334445566").kind, Kind.PLANE_PAIR)
        self.assertIs(combo("333444555678").kind, Kind.PLANE_ONE)
        self.assertIs(combo("333444555667788").kind, Kind.PLANE_PAIR)
        # 三张不连续
        self.assertIs(combo("33355567").kind, Kind.INVALID)
        # 带牌数量不对
        self.assertIs(combo("3334445").kind, Kind.INVALID)

    def test_invalid(self):
        self.assertIs(combo("34").kind, Kind.INVALID)
        self.assertIs(combo("3345").kind, Kind.INVALID)
        self.assertIs(combo("333445").kind, Kind.INVALID)


class CompareTest(unittest.TestCase):
    def test_same_kind(self):
        self.assertTrue(combo("4").beats(combo("3")))
        self.assertFalse(combo("3").beats(combo("4")))
        self.assertFalse(combo("3").beats(combo("3")))
        self.assertTrue(combo("45678").beats(combo("34567")))
        self.assertTrue(combo("QQKKAA").beats(combo("334455")))

    def test_different_kind(self):
        self.assertFalse(combo("44").beats(combo("3")))
        self.assertFalse(combo("34567").beats(combo("345678")))

    def test_different_length_straight(self):
        # 长度不同的顺子不能比较
        self.assertFalse(combo("456789").beats(combo("34567")))

    def test_bomb(self):
        self.assertTrue(combo("3333").beats(combo("AAA")))
        self.assertTrue(combo("4444").beats(combo("3333")))
        self.assertFalse(combo("3333").beats(combo("4444")))
        self.assertTrue(combo("鬼王").beats(combo("3333")))
        self.assertTrue(combo("鬼王").beats(combo("2222")))
        self.assertFalse(combo("3333").beats(combo("鬼王")))

    def test_free_play(self):
        self.assertTrue(combo("3").beats(None))

    def test_invalid_never_beats(self):
        self.assertFalse(combo("34").beats(None))


class HandOpsTest(unittest.TestCase):
    def test_hand_has(self):
        hand = parse_cards("334455")
        self.assertTrue(hand_has(hand, parse_cards("345")))
        self.assertTrue(hand_has(hand, parse_cards("334")))
        self.assertFalse(hand_has(hand, parse_cards("333")))

    def test_remove_cards(self):
        hand = list(parse_cards("334455"))
        remove_cards(hand, parse_cards("345"))
        self.assertEqual(format_cards(hand), "[3][4][5]")

    def test_format_sorted(self):
        # 排序按牌值：3 < ... < A < 2 < 鬼 < 王
        self.assertEqual(format_cards(parse_cards("A32")), "[3][A][2]")
        self.assertEqual(format_cards(parse_cards("王鬼2A")), "[A][2][鬼][王]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
