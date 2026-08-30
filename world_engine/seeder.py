"""伊瑟拉·澜誓城正式初始世界数据与落地逻辑。

把世界观设定(`docs/worldbuilding/`)中的正式世界落成可运行的初始世界:
全部已命名公开人物(三大陆政体现任代表 + 澜誓城本地)都种入引擎,但只激活
一小部分作为核心驱动人物,其余作为背景存在。

数据格式完全对齐引擎写入约定(world_events / relationships / character_memories),
复用 `repository.create_world(seed_demo=False)` 建立骨架,避免触碰既有测试世界。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from world_engine.database import Database
from world_engine.repository import WorldRepository, to_iso

# 引擎硬编码起始世界时间(见 repository.create_world):H 24816 年 1 月 1 日 08:00。
_WORLD_START = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)
_WORLD_NAME = "伊瑟拉·澜誓城"

# 地点:13 个核心城市 + 澜誓城内部功能区(名字唯一)。
_LOCATIONS: list[tuple[str, str, dict[str, int]]] = [
    ("澜誓城", "city", {"grain": 5000, "water": 1200, "forage": 300}),
    ("锻谷城", "city", {"metal": 4500, "workshop": 220, "timber": 900}),
    ("望镜湖庭", "city", {"herb": 1400, "timber": 1800, "lake": 700}),
    ("东澜港", "city", {"salt": 2600, "port": 500, "trade": 900}),
    ("赭泉关", "city", {"pasture": 3200, "water": 640, "road": 400}),
    ("冠枝河庭", "city", {"herb": 2100, "timber": 1600, "river": 800}),
    ("阶泉城", "city", {"terrace": 3800, "reservoir": 1100, "mine": 1500}),
    ("九泉驿城", "city", {"water": 1500, "caravan": 700, "station": 360}),
    ("南潮门港", "city", {"shipyard": 900, "orchard": 1200, "trade": 1400}),
    ("烬湾", "city", {"volcanic_glass": 800, "fish": 1600, "harbor": 700}),
    ("灯庭", "city", {"lantern": 500, "fleet": 900, "harbor": 1100}),
    ("东门城", "city", {"trade": 2000, "court": 600, "warehouse": 1300}),
    ("避潮岛", "city", {"shelter": 500, "rescue": 700, "fish": 1300}),
    ("河务档案区", "public", {"archives": 800, "water_records": 600}),
    ("王室堤岸", "public", {"embankment": 900, "grain": 400}),
    ("河畔大粮仓", "workplace", {"grain": 3000, "storage": 1500}),
    ("法师家族宅区", "home", {"arts": 400, "storables": 900}),
]

# 统一世界经纬度：东经为正、北纬为正。城内功能区只做小幅偏移，便于放大地图显示。
_LOCATION_COORDINATES: dict[str, tuple[float, float]] = {
    "澜誓城": (-71.0, 28.0),
    "锻谷城": (-132.0, 31.0),
    "望镜湖庭": (-62.0, 50.0),
    "东澜港": (-22.0, 30.0),
    "赭泉关": (-67.0, 12.0),
    "冠枝河庭": (58.0, 14.0),
    "阶泉城": (42.0, -28.0),
    "九泉驿城": (78.0, -18.0),
    "南潮门港": (52.0, -48.0),
    "烬湾": (112.0, 26.0),
    "灯庭": (132.0, 20.0),
    "东门城": (148.0, 8.0),
    "避潮岛": (161.0, -8.0),
    "河务档案区": (-71.08, 28.04),
    "王室堤岸": (-70.94, 27.98),
    "河畔大粮仓": (-71.03, 27.92),
    "法师家族宅区": (-71.12, 28.10),
}


def _location_area(name: str, kind: str) -> tuple[float, int, str | None]:
    parent = (
        "澜誓城"
        if name in {"河务档案区", "王室堤岸", "河畔大粮仓", "法师家族宅区"}
        else None
    )
    radius = {
        "city": 20.0,
        "ruin": 8.0,
        "public": 2.0,
        "workplace": 2.0,
        "home": 1.5,
    }.get(kind, 3.0)
    priority = {
        "home": 40,
        "workplace": 35,
        "public": 30,
        "ruin": 20,
        "city": 10,
    }.get(kind, 5)
    return radius, priority, parent


def _npc_activation(identity: str | None, is_core: bool) -> tuple[str, str, str]:
    persistent_keywords = (
        "女王",
        "代表",
        "议长",
        "召集",
        "主持",
        "行誓者",
        "记录官",
        "书记官",
        "管事",
        "工长",
        "领航",
        "传声",
        "记录者",
        "调度官",
        "首席",
        "总监",
        "守潮",
        "井见",
        "灯判",
        "港守",
        "祭官",
    )
    persistent = is_core or any(
        keyword in (identity or "") for keyword in persistent_keywords
    )
    if persistent:
        return "active", "persistent", "core" if is_core else "special"
    return "background", "distance", "background"

# 人物:三大陆政体现任代表(24 人)+ 澜誓城本地(6 人)。
# 字段顺序:name, location_name, energy, satiety, money, traits, goals, is_core。
_CHARACTERS: list[tuple[str, str, int, int, int, list[str], list[str], bool]] = [
    # —— 核心驱动(is_core=True,状态偏低,容易进入每轮裁判) ——
    (
        "塞芙拉·维誓",
        "王室堤岸",
        58,
        52,
        40,
        ["主见", "务实", "王权"],
        ["在合流复誓前让河务六席同意扩大紧急调水权", "巩固阿德伦王室对江河堤岸的主导权"],
        True,
    ),
    (
        "洛弥·陶穗",
        "河务档案区",
        55,
        60,
        30,
        ["审慎", "讲程序", "下游立场"],
        ["确保紧急调水令在60天共同大周期内接受公开审计", "保护下游农区的灌溉与粮食安全"],
        True,
    ),
    (
        "北潭·漱泉",
        "河务档案区",
        62,
        58,
        25,
        ["严谨", "中立记录"],
        ["在王室与六席之间维护河务记录的准确与公开", "在调水权争执中保存完整的水位与议约副本"],
        True,
    ),
    (
        "兰珥·明灯",
        "法师家族宅区",
        58,
        55,
        45,
        ["克己", "重传承"],
        [
            "让家族掌握的水利魔纹在公共安全与私有知识间取得平衡",
            "保护法师家族对储能器物与纹路的传承",
        ],
        True,
    ),
    (
        "陶瓮·仓守",
        "河畔大粮仓",
        55,
        62,
        35,
        ["精打细算", "忧粮"],
        ["在粮价波动与调水争执中保住三座大粮仓的存粮", "避免粮仓被王室或六席单方面征用"],
        True,
    ),
    (
        "石桥·渡安",
        "王室堤岸",
        60,
        54,
        20,
        ["稳重", "重责任"],
        ["赶在春季洪汛前加固阿德河堤坝与渡口", "防止上游抬高堤坝损害下游灌溉"],
        True,
    ),
    # —— 背景 NPC(is_core=False,状态偏高,极少被选中为活跃人物) ——
    (
        "砾舟·苏锤",
        "锻谷城",
        84,
        86,
        120,
        ["重工程", "讲契约"],
        ["公布矿道与维修预算而非私下决定矿脉归属"],
        False,
    ),
    ("欧宁·望镜", "望镜湖庭", 88, 82, 90, ["记录", "记忆"], ["保存封河决定与跨湖债务副本"], False),
    (
        "白栖·杉岸",
        "望镜湖庭",
        86,
        88,
        80,
        ["守护", "决断"],
        ["决定何时关闭冰湖通行、保障冬季救援"],
        False,
    ),
    (
        "芮珀·海衡",
        "东澜港",
        88,
        84,
        150,
        ["务实", "重信誉"],
        ["主持船籍审查、换取东澜船籍互认"],
        False,
    ),
    ("奎澜·短帆", "东澜港", 84, 86, 100, ["公允", "为民"], ["清理债务船员制度"], False),
    (
        "乌尔格·长风",
        "赭泉关",
        90,
        86,
        110,
        ["讲誓约", "守边"],
        ["处理征税队与牧道誓团争执、拒绝永久王室道路"],
        False,
    ),
    (
        "芙宁·冠枝",
        "冠枝河庭",
        86,
        84,
        90,
        ["守林", "善辩"],
        ["继续封闭遭受卡尔萨测量队进入的支流"],
        False,
    ),
    (
        "茵塔·苔册",
        "冠枝河庭",
        84,
        88,
        80,
        ["求实", "重样"],
        ["推动受监督的科学取样、避免长期禁运"],
        False,
    ),
    (
        "阿诺·阶泉",
        "阶泉城",
        88,
        82,
        130,
        ["务实", "重调度"],
        ["优先修复西山谷水库以减少旱季粮险"],
        False,
    ),
    ("米娅·岩铃", "阶泉城", 86, 88, 90, ["守牧", "重迁徙"], ["保障迁徙群体与牲畜饮水"], False),
    (
        "梅里·九井",
        "九泉驿城",
        88,
        86,
        120,
        ["持重", "重协作"],
        ["与阿德伦、卡尔萨交换水文记录"],
        False,
    ),
    (
        "萨迦·砂铃",
        "九泉驿城",
        84,
        88,
        100,
        ["谨慎", "重商"],
        ["担心公开数据引外来政权干预井群管理"],
        False,
    ),
    ("莱莎·潮门", "南潮门港", 90, 84, 150, ["务实", "重舰队"], ["与中裂海航盟恢复护航互认"], False),
    ("恩索·葡萄", "南潮门港", 86, 88, 110, ["重商", "重税"], ["先解决诺赫兰粮食关税"], False),
    ("赫洛·烬湾", "烬湾", 84, 86, 90, ["警觉", "重预警"], ["火山震动后提前迁港"], False),
    (
        "瑟因·黑砂",
        "烬湾",
        88,
        84,
        80,
        ["重产", "求稳"],
        ["主张预警不足以停产、避免影响玻璃供应"],
        False,
    ),
    (
        "多里安·灯庭",
        "灯庭",
        90,
        86,
        160,
        ["重法", "严正"],
        ["要求东门港邦加强对走私单纹的检查"],
        False,
    ),
    (
        "佩娅·远帆",
        "灯庭",
        86,
        88,
        120,
        ["重护航", "顾局"],
        ["平息黑潮避难港争端、避免海军分散"],
        False,
    ),
    ("伊莎·东门", "东门城", 88, 84, 130, ["重中立", "慎罚"], ["扣押涉嫌走私完整单纹的船只"], False),
    ("博兰·白帆", "东门城", 86, 88, 110, ["重信誉", "护商"], ["担心扣船破坏中立港信誉"], False),
    ("桑达·避潮", "避潮岛", 90, 86, 100, ["守潮", "重救援"], ["拒绝中裂海海军接管避难港"], False),
    (
        "蕾恩·盐骨",
        "避潮岛",
        86,
        88,
        80,
        ["求实", "重归物"],
        ["先公开失踪者名单再讨论搜查权"],
        False,
    ),
    (
        "沃森·珠船",
        "河务档案区",
        84,
        87,
        95,
        ["多语", "重桥路"],
        ["作为跨区商旅中间人沟通阿德伦与东澜港"],
        False,
    ),
    (
        "棹云·潮生",
        "河畔大粮仓",
        85,
        89,
        70,
        ["勤恳", "重河运"],
        ["维持阿德河内河码头与粮船调度"],
        False,
    ),
]

# 关系网(只覆盖核心人物之间与少数政体对立):source, target, affinity, trust。
# 引擎运行时只会正向写入,故这里要双向对称;主键为 (world_id, source, target)。
# 人物身份/头衔:name -> 当代任职(来自 20 号文档与澜誓城本地角色)。
# 扩充的普通居民：地点 -> [(姓名, 身份, 特质, 目标)]，is_core=False 为背景 NPC。
# 让玩家到各地点都能立刻遇到看得见的人。
_EXTRA_CHARACTERS: dict[str, list[tuple[str, str, list[str], list[str]]]] = {
    "澜誓城": [
        ("满仓·河记", "河务书记小吏", ["勤记"], ["把市集河段的水位记录抄录进档"]),
        ("粟丰·粮市", "市集粮商", ["善察", "重利"], ["囤积适价粮米，待粮价回暖卖出"]),
        ("卫安·河巡", "巡城河卫", ["尽责"], ["巡查堤岸治安，防止夜间偷渡"]),
        ("远客·星来", "朝圣旅人", ["虔敬"], ["循天环星象朝圣，记录所见异象"]),
    ],
    "河务档案区": [
        ("抄吏·竹简", "书记学徒", ["勤学"], ["练好抄录，盼升为正式书记"]),
        ("录事·墨存", "档案录事", ["谨细"], ["整理三汇水文抄本的副本存案"]),
    ],
    "王室堤岸": [
        ("卫敬·堤台", "亲卫", ["忠谨"], ["守护堤岸上的王室仪仗与往来贵客"]),
        ("衡工·石鉴", "堤工头", ["识土"], ["赶在春汛前加固堤脚"]),
    ],
    "河畔大粮仓": [
        ("仓监·斗量", "仓吏", ["精算"], ["核对入库粮数，防止亏空"]),
        ("籴吏·平粜", "量籴工", ["勤快"], ["平粜时维持斗斛公正"]),
    ],
    "法师家族宅区": [
        ("学徒·明烛", "法师学徒", ["好学"], ["临摹家族传承的储能纹样"]),
        ("仆役·守院", "宅院仆役", ["本分"], ["看守宅院器物，谨防外泄"]),
    ],
    "锻谷城": [
        ("炉工·炎锻", "熔炉工", ["耐热"], ["守住炉温，炼出合用铁料"]),
        ("匠首·铁砧", "铁匠", ["老练"], ["打制耐用器物，赢得工坊声誉"]),
    ],
    "望镜湖庭": [
        ("药师·草庐", "药师", ["识药"], ["采集湖岸药草，制成常用药散"]),
        ("湖民·棹歌", "湖民", ["识水"], ["趁湖面未封，多捕一季鱼货"]),
    ],
    "东澜港": [
        ("码头工·缆桩", "码头工", ["有力"], ["卸完这船货，领了工钱"]),
        ("书吏·关单", "报关书吏", ["严核"], ["核对船籍与货单，防走私单纹"]),
    ],
    "赭泉关": [
        ("牧人·长鞭", "牧人", ["耐劳"], ["护牧群越冬，守住水源地"]),
        ("哨守·烽石", "哨守", ["警觉"], ["守望关道，报告过往商队"]),
    ],
    "冠枝河庭": [
        ("采药人·箩筐", "采药人", ["识林"], ["采当季药材上交河庭"]),
        ("护林人·青弓", "护林人", ["守林"], ["拦阻越界采伐，护住封河段"]),
    ],
    "阶泉城": [
        ("梯田户·垄上", "梯田户", ["勤作"], ["侍候梯田，盼水渠畅通"]),
        ("水官·渠正", "水官", ["持平"], ["按维护责任分配水库用水"]),
    ],
    "九泉驿城": [
        ("驿卒·快脚", "驿卒", ["脚快"], ["跑完这趟驿书，及时送到"]),
        ("驼商·沙铃", "驼商", ["识途"], ["把货物安全运过旱地驿站"]),
    ],
    "南潮门港": [
        ("船坞工·木楔", "船坞工", ["巧手"], ["修整船体，赶在开航前下水"]),
        ("果农·熟果", "果农", ["勤耕"], ["这季果子要趁甜运到北岸"]),
    ],
    "烬湾": [
        ("玻璃匠·琉光", "玻璃匠", ["细心"], ["烧出透亮的火山玻璃，卖个好价"]),
        ("渔船头·浪尾", "渔船头", ["识潮"], ["趁喷发间歇出海，避着风浪"]),
    ],
    "灯庭": [
        ("灯夫·长明", "灯夫", ["守夜"], ["当夜点亮航标灯，为归船引路"]),
        ("水手·桅影", "水手", ["健壮"], ["随船走一趟裂海，挣份工钱"]),
    ],
    "东门城": [
        ("通译·三语", "通译", ["多语"], ["替外来商旅译清合同，收取酬劳"]),
        ("货栈管事·箱账", "货栈管事", ["精账"], ["核实入库货物，防走私错账"]),
    ],
    "避潮岛": [
        ("救援者·浮桴", "救援者", ["勇毅"], ["在风暴里救回落水船员"]),
        ("渔夫·潮网", "渔夫", ["识潮"], ["趁平静海面多下一网"]),
    ],
}


_IDENTITY: dict[str, str] = {
    "塞芙拉·维誓": "议约女王",
    "洛弥·陶穗": "河务六席·当值召集人(下游农区)",
    "北潭·漱泉": "河务六席·书记官",
    "兰珥·明灯": "法师世家代表",
    "陶瓮·仓守": "大粮仓管事",
    "石桥·渡安": "渡口堤岸桥工长",
    "砾舟·苏锤": "锻谷城九炉代表会·轮值召集人",
    "欧宁·望镜": "望镜湖庭·夏季记录官",
    "白栖·杉岸": "望镜湖庭·冬季救援领航人",
    "芮珀·海衡": "东澜港·港守",
    "奎澜·短帆": "东澜港·船员工会代表",
    "乌尔格·长风": "赭泉关·行誓者",
    "芙宁·冠枝": "冠枝河庭·传声人",
    "茵塔·苔册": "冠枝河庭·药材记录者",
    "阿诺·阶泉": "阶泉城·议盟调度官",
    "米娅·岩铃": "阶泉城·牧道席代表",
    "梅里·九井": "九泉驿城·首席井见",
    "萨迦·砂铃": "九泉驿城·商队代表",
    "莱莎·潮门": "南潮门港·轮值海议长",
    "恩索·葡萄": "南潮门港·果湾代表",
    "赫洛·烬湾": "烬湾·火纹祭官",
    "瑟因·黑砂": "烬湾·玻璃工坊席代表",
    "多里安·灯庭": "灯庭·首席灯判",
    "佩娅·远帆": "灯庭·护航总监",
    "伊莎·东门": "东门城·居留庭主持人",
    "博兰·白帆": "东门城·货仓庭代表",
    "桑达·避潮": "避潮岛·守潮者",
    "蕾恩·盐骨": "避潮岛·漂流物席代表",
    "沃森·珠船": "跨区商旅中间人",
    "棹云·潮生": "河畔码头船工长",
}


_RELATIONSHIPS: list[tuple[str, str, int, int]] = [
    ("塞芙拉·维誓", "洛弥·陶穗", -25, -15),  # 调水权对峙(张力核心)
    ("塞芙拉·维誓", "北潭·漱泉", 20, 30),
    ("洛弥·陶穗", "北潭·漱泉", 30, 40),
    ("塞芙拉·维誓", "陶瓮·仓守", 10, 5),
    ("洛弥·陶穗", "陶瓮·仓守", 35, 30),
    ("塞芙拉·维誓", "兰珥·明灯", 5, 0),
    ("洛弥·陶穗", "兰珥·明灯", 15, 10),
    ("塞芙拉·维誓", "石桥·渡安", 20, 25),
    ("洛弥·陶穗", "石桥·渡安", 30, 35),
    ("塞芙拉·维誓", "沃森·珠船", 5, 0),
    ("芮珀·海衡", "莱莎·潮门", -10, -5),  # 东澜港与南潮港税之争
    ("多里安·灯庭", "伊莎·东门", 15, 20),
]

# 初始客观事件:开局设定,给核心人物现成目标(合流复誓临近 + 调水权争执)。
_INITIAL_EVENT = {
    "summary": (
        "H 24816 年 1 月,阿德伦议约王国进入新年。女王塞芙拉·维誓提议在"
        "合流复誓(第 3 月第 7 天)之前扩大紧急调水权;下游农区代表洛弥·陶穗要求"
        "所有紧急命令在 60 天共同大周期内接受公开审计。三汇水文抄本的解释之争"
        "在王室、下游城市与商队之间继续发酵。"
    ),
    "event_type": "world.initial",
    "tick_id": "initial-seed",
    "payload": {
        "kind": "world_initial",
        "era_h": 24816,
        "river_treaty": "河流议约",
        "midwater_records_dispute": True,
    },
    "location_name": "河务档案区",
    # 哪些核心人物获得该事件的初始记忆
    "memory_importance": 8,
    "memory_characters": [
        "塞芙拉·维誓",
        "洛弥·陶穗",
        "北潭·漱泉",
        "兰珥·明灯",
        "陶瓮·仓守",
        "石桥·渡安",
    ],
}


def create_iserra_world(database: Database, name: str = _WORLD_NAME) -> str:
    """创建一个正式伊瑟拉·澜誓城初始世界,返回新 world_id。

    复用 `create_world(seed_demo=False)` 建骨架(不触碰既有测试世界),随后按引擎
    写入约定补地点、人物、关系、初始事件与人物记忆。
    """
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name=name,
            minutes_per_tick=60,
            time_scale=1.0,
            seed_demo=False,
        )
        _seed_locations(connection, world_id)
        _seed_detail_map(connection, world_id)
        character_ids = _seed_characters(connection, world_id)
        _seed_map_features(connection, world_id)
        _seed_relationships(connection, world_id, character_ids)
        event_id = _seed_initial_event(connection, world_id, character_ids)
        _seed_memories(connection, world_id, character_ids, event_id)
        _seed_accumulators(connection, world_id)
    return world_id


def _seed_locations(connection: sqlite3.Connection, world_id: str) -> None:
    connection.executemany(
        """
        INSERT INTO locations(
            id, world_id, name, kind, resources_json, longitude, latitude,
            area_radius_km, area_priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(uuid4()), world_id, name, kind,
                json.dumps(resources, ensure_ascii=False),
                _LOCATION_COORDINATES[name][0], _LOCATION_COORDINATES[name][1],
                _location_area(name, kind)[0], _location_area(name, kind)[1],
            )
            for name, kind, resources in _LOCATIONS
        ],
    )
    for name, kind, _resources in _LOCATIONS:
        parent_name = _location_area(name, kind)[2]
        if parent_name:
            connection.execute(
                """
                UPDATE locations
                SET parent_location_id = (
                    SELECT id FROM locations
                    WHERE world_id = ? AND name = ?
                )
                WHERE world_id = ? AND name = ?
                """,
                (world_id, parent_name, world_id, name),
            )


def _seed_detail_map(connection: sqlite3.Connection, world_id: str) -> None:
    city = connection.execute(
        """
        SELECT id, longitude, latitude FROM locations
        WHERE world_id = ? AND name = '澜誓城'
        """,
        (world_id,),
    ).fetchone()
    if city is None:
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO world_maps(
            id, world_id, name, kind, asset_path,
            min_longitude, max_longitude, min_latitude, max_latitude,
            width_pixels, height_pixels, zoom_level,
            location_id, map_role, review_status
        ) VALUES (?, ?, '澜誓城详细地图', 'settlement', ?, ?, ?, ?, ?,
                  1600, 1600, 2, ?, 'detail', 'candidate')
        """,
        (
            f"{world_id}:map:detail:oathflow",
            world_id,
            "navigation/settlements/oathflow/detail-map.svg",
            city["longitude"] - 0.21,
            city["longitude"] + 0.21,
            city["latitude"] - 0.18,
            city["latitude"] + 0.18,
            city["id"],
        ),
    )


def _seed_characters(connection: sqlite3.Connection, world_id: str) -> dict[str, str]:
    """插入全部人物,返回 {人物名 -> character_id} 映射。"""
    location_ids = {
        row["name"]: row["id"]
        for row in connection.execute(
            "SELECT id, name FROM locations WHERE world_id = ?",
            (world_id,),
        ).fetchall()
    }
    now = to_iso(datetime.now(UTC))
    character_ids: dict[str, str] = {}
    rows = []
    for name, location_name, energy, satiety, money, traits, goals, is_core in _CHARACTERS:
        character_id = str(uuid4())
        character_ids[name] = character_id
        identity = _IDENTITY.get(name)
        activation_state, activation_policy, activation_reason = _npc_activation(
            identity, is_core
        )
        # 正式 NPC 不占用玩家主视角；主视角仅由后续创建的玩家角色获得。
        is_pov = 0
        checksum = sum(ord(character) for character in name)
        base_longitude, base_latitude = _LOCATION_COORDINATES[location_name]
        longitude = base_longitude + ((checksum % 17) - 8) * 0.002
        latitude = base_latitude + (((checksum // 17) % 17) - 8) * 0.002
        rows.append(
            (
                character_id,
                world_id,
                name,
                location_ids[location_name],
                energy,
                satiety,
                money,
                json.dumps(traits, ensure_ascii=False),
                json.dumps(goals, ensure_ascii=False),
                identity,
                1 if is_core else 0,
                is_pov,
                longitude,
                latitude,
                location_ids[location_name],
                activation_state,
                activation_policy,
                activation_reason,
                now,
                now,
            )
        )
    for location_name, people in _EXTRA_CHARACTERS.items():
        for name, identity, traits, goals in people:
            character_id = str(uuid4())
            character_ids[name] = character_id
            checksum = sum(ord(character) for character in name)
            base_longitude, base_latitude = _LOCATION_COORDINATES[location_name]
            activation_state, activation_policy, activation_reason = _npc_activation(
                identity, False
            )
            rows.append(
                (
                    character_id,
                    world_id,
                    name,
                    location_ids[location_name],
                    86,
                    90,
                    40,
                    json.dumps(traits, ensure_ascii=False),
                    json.dumps(goals, ensure_ascii=False),
                    identity,
                    0,
                    0,
                    base_longitude + ((checksum % 17) - 8) * 0.002,
                    base_latitude + (((checksum // 17) % 17) - 8) * 0.002,
                    location_ids[location_name],
                    activation_state,
                    activation_policy,
                    activation_reason,
                    now,
                    now,
                )
            )
    connection.executemany(
        """
        INSERT INTO characters(
            id, world_id, name, location_id, energy, satiety, money,
            traits_json, goals_json, identity, is_core, is_pov,
            longitude, latitude, current_location_id,
            activation_state, activation_policy, activation_reason,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return character_ids


def _seed_map_features(connection: sqlite3.Connection, world_id: str) -> None:
    """用现有设定登记建筑/地标投影，为后续奇观与遗迹扩展预留统一入口。"""
    now = to_iso(datetime.now(UTC))
    feature_types = {
        "河务档案区": "building",
        "王室堤岸": "landmark",
        "河畔大粮仓": "building",
        "法师家族宅区": "district",
    }
    rows = connection.execute(
        """
        SELECT id, name, longitude, latitude
        FROM locations
        WHERE world_id = ? AND name IN (?, ?, ?, ?)
        """,
        (world_id, *feature_types),
    ).fetchall()
    connection.executemany(
        """
        INSERT INTO map_features(
            id, world_id, name, feature_type, longitude, latitude,
            location_id, is_known, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, '{}', ?, ?)
        """,
        [
            (
                f"{world_id}:feature:{row['id']}",
                world_id, row["name"], feature_types[row["name"]],
                row["longitude"], row["latitude"], row["id"], now, now,
            )
            for row in rows
        ],
    )


def _seed_relationships(
    connection: sqlite3.Connection,
    world_id: str,
    character_ids: dict[str, str],
) -> None:
    """按名字为给定关系写入双向 affinity/trust。"""
    now = to_iso(datetime.now(UTC))
    for source_name, target_name, affinity, trust in _RELATIONSHIPS:
        source_id = character_ids[source_name]
        target_id = character_ids[target_name]
        for first, second in ((source_id, target_id), (target_id, source_id)):
            connection.execute(
                """
                INSERT OR IGNORE INTO relationships(
                    world_id, source_character_id, target_character_id, affinity, trust, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (world_id, first, second, affinity, trust, now),
            )


def _seed_initial_event(
    connection: sqlite3.Connection,
    world_id: str,
    character_ids: dict[str, str],
) -> str:
    location_id = connection.execute(
        "SELECT id FROM locations WHERE world_id = ? AND name = ?",
        (world_id, _INITIAL_EVENT["location_name"]),
    ).fetchone()["id"]
    event_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO world_events(
            id, world_id, tick_id, occurred_at, event_type, location_id,
            summary, importance, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'major', ?, ?)
        """,
        (
            event_id,
            world_id,
            _INITIAL_EVENT["tick_id"],
            to_iso(_WORLD_START),
            _INITIAL_EVENT["event_type"],
            location_id,
            _INITIAL_EVENT["summary"],
            json.dumps(_INITIAL_EVENT["payload"], ensure_ascii=False),
            to_iso(datetime.now(UTC)),
        ),
    )
    return event_id


def _seed_memories(
    connection: sqlite3.Connection,
    world_id: str,
    character_ids: dict[str, str],
    event_id: str,
) -> None:
    """为核心人物挂上同一初始事件的个人记忆。"""
    now = to_iso(datetime.now(UTC))
    summary = (
        "我得知女王塞芙拉·维誓提议在合流复誓前扩大紧急调水权,"
        "下游代表洛弥·陶穗要求 60 天共同大周期内公开审计。"
    )
    for name in _INITIAL_EVENT["memory_characters"]:
        connection.execute(
            """
            INSERT INTO character_memories(
                id, world_id, character_id, event_id, memory_type,
                summary, importance, confidence, created_at
            ) VALUES (?, ?, ?, ?, 'experienced', ?, ?, 1.0, ?)
            """,
            (
                str(uuid4()),
                world_id,
                character_ids[name],
                event_id,
                summary,
                _INITIAL_EVENT["memory_importance"],
                now,
            ),
        )


def _seed_accumulators(connection: sqlite3.Connection, world_id: str) -> None:
    """为新增人物补 character_state_accumulators 行(create_world 在空世界时不会预置)。"""
    connection.execute(
        """
        INSERT OR IGNORE INTO character_state_accumulators(
            character_id, world_id, satiety_residual, energy_residual, updated_at
        )
        SELECT id, world_id, 0, 0, updated_at FROM characters WHERE world_id = ?
        """,
        (world_id,),
    )
