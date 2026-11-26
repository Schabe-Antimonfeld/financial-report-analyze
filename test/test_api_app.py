"""FastAPI 应用装配测试（不启动服务、不连数据库）。"""

from __future__ import annotations

import unittest

from backend.api import create_app
from backend.config import Settings


class AppRouteTest(unittest.TestCase):
    """路由与错误处理是否装配。"""

    def test_required_routes_exist(self) -> None:
        """四个交付接口与健康检查都应注册。"""
        app = create_app(Settings())
        paths = {getattr(route, "path", "") for route in app.routes}
        for expected in (
            "/health",
            "/opinions",
            "/opinions/{opinion_id}",
            "/opinion-trends",
            "/evidence/{evidence_id}/source",
        ):
            with self.subTest(path=expected):
                self.assertIn(expected, paths)

    def test_openapi_schema_is_built(self) -> None:
        """OpenAPI 能生成，保证 /docs 可用。"""
        app = create_app(Settings())
        schema = app.openapi()
        self.assertEqual(schema["info"]["title"], "期货研报观点")
        self.assertIn("/opinions", schema["paths"])


if __name__ == "__main__":
    unittest.main()
