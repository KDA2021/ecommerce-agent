# -*- coding: utf-8 -*-
"""Badcase JSONL 追加写入：点踩样本回流离线评测（P10-1）。

每次点踩追加一行 JSON 到 ``{data_dir}/badcases.jsonl``，字段尽量与
``user_feedback`` 对齐，额外带 ``kind`` 便于 ``eval_regression.py --with-badcases``
把它转换成评测用例。写失败只记录日志、绝不影响反馈接口主流程。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.domain.feedback.feedback import UserFeedback, reason_label

logger = logging.getLogger(__name__)


class JsonlBadcaseWriter:
    """把点踩反馈追加写入 JSONL 文件。"""

    def __init__(self, path: str | Path) -> None:
        """初始化目标文件路径。

        Args:
            path: badcases.jsonl 路径。
        """
        self.path = Path(path)

    def build_record(self, feedback: UserFeedback) -> dict:
        """构造一行 badcase 记录。

        Args:
            feedback: 点踩反馈。

        Returns:
            可 JSON 序列化的 dict。
        """
        return {
            "id": f"badcase-{feedback.trace_id or feedback.session_id}-{feedback.created_at}",
            "session_id": feedback.session_id,
            "trace_id": feedback.trace_id,
            "buyer_id": feedback.buyer_id,
            "kind": "general",
            "query": feedback.query,
            "reply_preview": feedback.reply_preview,
            "score": feedback.score,
            "reason": feedback.reason,
            "reason_label": reason_label(feedback.reason),
            "created_at": feedback.created_at,
        }

    def append_nowait(self, feedback: UserFeedback) -> bool:
        """同步追加一条 badcase；任何异常都只记日志。

        Args:
            feedback: 点踩反馈。

        Returns:
            是否成功写入。
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(self.build_record(feedback), ensure_ascii=False, default=str) + "\n",
                )
            return True
        except Exception as exc:  # noqa: BLE001 - badcase 是旁路能力，失败不影响反馈
            logger.warning("badcase 写入失败 path=%s err=%s", self.path, exc)
            return False

    async def append(self, feedback: UserFeedback) -> bool:
        """异步追加一条 badcase（线程池落盘，避免阻塞事件循环）。

        Args:
            feedback: 点踩反馈。

        Returns:
            是否成功写入。
        """
        return await asyncio.to_thread(self.append_nowait, feedback)
