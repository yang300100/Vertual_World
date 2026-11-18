"""哥哥，入口跳转要保留旅人已经选好的世界与页面参数。"""

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", "/ui/"),
        ("?world=chosen-world", "/ui/?world=chosen-world"),
        ("?world=chosen-world&view=a%20b", "/ui/?world=chosen-world&view=a%20b"),
    ],
)
def test_root_redirect_preserves_world_query(settings, query, expected):
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/{query}", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == expected
