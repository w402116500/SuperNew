"""上传文件名白名单与文档解析能力的边界测试。"""

import unittest

from backend.api.upload_validation import is_supported_document, normalize_upload_filename


class UploadValidationTests(unittest.TestCase):
    def test_markdown_and_text_extensions_are_supported(self):
        self.assertTrue(is_supported_document("商品参数.MD"))
        self.assertTrue(is_supported_document("店铺规则.txt"))
        self.assertEqual(normalize_upload_filename(" vivo-y200.md "), "vivo-y200.md")

    def test_unsupported_extension_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Markdown"):
            normalize_upload_filename("evaluation.jsonl")


if __name__ == "__main__":
    unittest.main()
