"""证据评分模型异常时，RAG 必须保守结束而不能让聊天接口报错。"""

import unittest
from unittest.mock import Mock, patch

from backend.rag.pipeline import EvidenceGrade, _resolve_route, grade_documents_node


class EvidenceGradingFallbackTests(unittest.TestCase):
    def test_invalid_structured_grade_fails_closed(self):
        docs = [{"filename": "员工手册.md", "text": "试用期为三个月。"}]
        state = {
            "request_context": Mock(),
            "docs": docs,
            "question": "公司的报销规则是什么？",
            "context": "[1] 员工手册.md\n试用期为三个月。",
            "rag_trace": {"retrieved_chunks": docs},
        }

        with patch("backend.rag.pipeline._get_grader_model", return_value=Mock()), patch(
            "backend.rag.pipeline.invoke_structured_output",
            side_effect=RuntimeError("invalid structured response"),
        ):
            result = grade_documents_node(state)

        self.assertEqual(result["route"], "no_knowledge")
        self.assertEqual(result["retrieval_status"], "no_knowledge")
        self.assertEqual(result["rag_trace"]["evidence_reason"], "evidence_grading_unavailable")
        self.assertEqual(result["docs"], [])


if __name__ == "__main__":
    unittest.main()
