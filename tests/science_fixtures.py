"""哥哥，测试工坊只属于离线用例，不进入真实初始化。"""

import pytest

from world_engine.science_schema import create_session
from world_engine.science_workshop import seed_workshop

pytest_plugins = ["test_collective_research"]


@pytest.fixture
def science_lab(lab):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        assets = seed_workshop(c, wid, at, seed=7)
        sid = create_session(c, wid, at)
    return db, wid, sid, actors, assets, at


def advance(c, wid, at):
    from test_science_work import finish

    from world_engine.science_programs import ScienceService

    finish(c, wid, at)
    ScienceService.tick(c, wid, at)
