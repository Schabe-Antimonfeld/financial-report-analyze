"""配置读取测试：本地 JSON、缺省值与非法配置。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.config import load_settings, settings_from_dict
from backend.errors import ConfigurationError


class SettingsTest(unittest.TestCase):
    """config.local.json 的读取与校验。"""

    def setUp(self) -> None:
        """创建临时目录承载配置文件。"""
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def write_config(self, payload: object, name: str = "config.json") -> Path:
        """写入一份临时配置并返回路径。

        :param payload: 可 JSON 序列化的对象。
        :param name: 文件名。
        :return: 配置文件路径。
        """
        path = self.directory / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def test_full_config_is_parsed(self) -> None:
        """数据库、资料目录、模型与 API 配置都能读到。"""
        path = self.write_config(
            {
                "database": {
                    "host": "10.0.0.2",
                    "port": 3307,
                    "name": "reports",
                    "user": "app",
                    "password": "secret",
                    "connect_timeout": 5,
                },
                "data_root": "D:/data",
                "max_attachments": 4,
                "fallback": {
                    "endpoint": "http://example/stream",
                    "api_token": "token",
                    "enabled": True,
                    "max_fragments": 2,
                },
                "api": {
                    "page_size": 20,
                    "max_page_size": 40,
                    "cors_origins": ["http://localhost:5173"],
                },
            }
        )
        settings = load_settings(path)
        self.assertEqual(settings.database.host, "10.0.0.2")
        self.assertEqual(settings.database.port, 3307)
        self.assertEqual(settings.database.name, "reports")
        self.assertTrue(settings.database.is_password_set)
        self.assertEqual(settings.data_root, Path("D:/data"))
        self.assertEqual(settings.max_attachments, 4)
        self.assertEqual(settings.fallback.endpoint, "http://example/stream")
        self.assertEqual(settings.fallback.max_fragments, 2)
        self.assertEqual(settings.api_page_size, 20)
        self.assertEqual(settings.api_cors_origins, ("http://localhost:5173",))

    def test_empty_object_returns_defaults(self) -> None:
        """空配置使用代码内默认值，密码为空。"""
        settings = settings_from_dict({})
        self.assertEqual(settings.database.host, "127.0.0.1")
        self.assertEqual(settings.database.port, 3306)
        self.assertFalse(settings.database.is_password_set)
        self.assertEqual(settings.data_root, Path("data"))

    def test_missing_explicit_file_is_rejected(self) -> None:
        """显式给出的配置路径不存在时应报错，而不是静默用默认值。"""
        with self.assertRaises(ConfigurationError):
            load_settings(self.directory / "missing.json")

    def test_invalid_json_is_rejected(self) -> None:
        """非法 JSON 报配置错误。"""
        path = self.directory / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            load_settings(path)

    def test_wrong_field_type_is_rejected(self) -> None:
        """端口写成非数字时报配置错误。"""
        path = self.write_config({"database": {"port": "abc"}})
        with self.assertRaises(ConfigurationError):
            load_settings(path)

    def test_invalid_section_type_is_rejected(self) -> None:
        """数据库段写成数组时报配置错误。"""
        path = self.write_config({"database": []})
        with self.assertRaises(ConfigurationError):
            load_settings(path)


if __name__ == "__main__":
    unittest.main()
