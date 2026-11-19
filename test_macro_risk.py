"""macro_risk 离线单测：投票限次、评论双栈增删精选、原子存储。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from macro_risk import (
    DEFAULT_RISK_VALUES,
    add_comment,
    delete_comment,
    feature_comment,
    load_comments,
    load_regime,
    load_votes,
    load_voters,
    new_session_id,
    save_regime,
    vote_risk,
)


class MacroRiskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.votes_path = Path(self.tmp.name) / "votes.json"
        self.comments_path = Path(self.tmp.name) / "comments.json"
        self.regime_path = Path(self.tmp.name) / "regime.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_values(self) -> None:
        values = load_votes(self.votes_path)
        self.assertEqual(values, DEFAULT_RISK_VALUES)
        self.assertEqual(values["战争"], 80.0)
        self.assertEqual(values["其它"], 50.0)

    def test_vote_repeated_allowed(self) -> None:
        session = new_session_id()
        values = vote_risk("战争", True, session, self.votes_path)
        self.assertAlmostEqual(values["战争"], 80.1)
        # 同会话可重复投票，分值持续累计
        values = vote_risk("战争", True, session, self.votes_path)
        self.assertAlmostEqual(values["战争"], 80.2)
        values = vote_risk("战争", False, new_session_id(), self.votes_path)
        self.assertAlmostEqual(values["战争"], 80.1)
        voters = load_voters(self.votes_path)
        self.assertEqual(len(voters["战争"]), 2)

    def test_vote_clamped_and_unknown_item(self) -> None:
        values = vote_risk("不存在项", True, new_session_id(), self.votes_path)
        self.assertEqual(values, DEFAULT_RISK_VALUES)
        # 极限值限幅
        self.votes_path.write_text(
            '{"values": {"战争": 100.0, "美元紧缩": 80.0, "中国通缩": 80.0, "其它": 50.0}, "votes": []}',
            encoding="utf-8",
        )
        values = vote_risk("战争", True, new_session_id(), self.votes_path)
        self.assertEqual(values["战争"], 100.0)

    def test_comment_lifecycle(self) -> None:
        session = new_session_id()
        record = add_comment("市场风险可控", session, "1.2.3.4", self.comments_path)
        data = load_comments(self.comments_path)
        self.assertEqual(len(data["buffer"]), 1)
        self.assertEqual(data["global"], [])
        self.assertEqual(data["buffer"][0]["ip"], "1.2.3.4")

        # 普通用户不能精选
        self.assertFalse(feature_comment(record["id"], self.comments_path, is_admin=False))
        # 管理员精选 → 全局栈
        self.assertTrue(feature_comment(record["id"], self.comments_path, is_admin=True))
        data = load_comments(self.comments_path)
        self.assertEqual(data["buffer"], [])
        self.assertEqual(len(data["global"]), 1)

        # 普通用户不能删全局栈评论（也不是自己的会话）
        self.assertFalse(delete_comment(record["id"], session, self.comments_path, is_admin=False))
        # 管理员可删全局栈评论
        self.assertTrue(delete_comment(record["id"], session, self.comments_path, is_admin=True))
        self.assertEqual(load_comments(self.comments_path)["global"], [])

    def test_delete_own_buffer_comment_only(self) -> None:
        mine = new_session_id()
        other = new_session_id()
        rec_mine = add_comment("我的评论", mine, "9.9.9.9", self.comments_path)
        add_comment("他人评论", other, "8.8.8.8", self.comments_path)
        # 本人可删自己的缓冲栈评论
        self.assertTrue(delete_comment(rec_mine["id"], mine, self.comments_path, is_admin=False))
        data = load_comments(self.comments_path)
        self.assertEqual(len(data["buffer"]), 1)
        self.assertEqual(data["buffer"][0]["session_id"], other)
        # 不能删他人的
        others_id = data["buffer"][0]["id"]
        self.assertFalse(delete_comment(others_id, mine, self.comments_path, is_admin=False))
        self.assertEqual(len(data["buffer"]), 1)

    def test_comment_content_stripped_and_truncated(self) -> None:
        record = add_comment("  " + "字" * 600 + "  ", new_session_id(), "1.1.1.1", self.comments_path)
        self.assertEqual(len(record["content"]), 500)
        with self.assertRaises(ValueError):
            add_comment("   ", new_session_id(), "1.1.1.1", self.comments_path)

    def test_regime_load_default_and_admin_only_save(self) -> None:
        self.assertEqual(load_regime(self.regime_path), -80.0)
        # 非管理员不能改
        self.assertFalse(save_regime(30.0, self.regime_path, is_admin=False))
        self.assertEqual(load_regime(self.regime_path), -80.0)
        # 管理员可改
        self.assertTrue(save_regime(30.0, self.regime_path, is_admin=True))
        self.assertEqual(load_regime(self.regime_path), 30.0)
        # 限幅
        save_regime(500, self.regime_path, is_admin=True)
        self.assertEqual(load_regime(self.regime_path), 100.0)
        save_regime(-999, self.regime_path, is_admin=True)
        self.assertEqual(load_regime(self.regime_path), -100.0)


if __name__ == "__main__":
    unittest.main()
