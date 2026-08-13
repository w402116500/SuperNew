"""会话消息路由应能稳定把存储记录封装为严格响应。"""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.api.routes.sessions import get_session_messages


class SessionMessagesRouteTests(unittest.TestCase):
    def test_returns_stored_messages_as_response_models(self):
        records = [
            {
                "type": "human",
                "content": "Y200 的电池容量是多少？",
                "timestamp": "2026-08-11T12:00:00+08:00",
                "rag_trace": None,
            }
        ]

        with patch("backend.api.routes.sessions.storage.get_session_messages", return_value=records):
            response = asyncio.run(
                get_session_messages("session-1", SimpleNamespace(username="admin"))
            )

        self.assertEqual(len(response.messages), 1)
        self.assertEqual(response.messages[0].content, records[0]["content"])


if __name__ == "__main__":
    unittest.main()
