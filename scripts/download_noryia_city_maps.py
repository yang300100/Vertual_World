"""从 Noryia Burgs CSV 的 watabou 预览链接并发渲染并批量补齐城镇详图。

watabou 城市生成器是动态 JS 页面（渲染到 canvas），需要浏览器执行。本脚本用
Playwright + 系统 Edge 并发渲染，断点续跑（已存在的 png 跳过），完成后刷新
`cities/index.json` 的 downloaded。在可访问 watabou.github.io 的环境运行。

用法：
    pip install playwright
    python scripts/download_noryia_city_maps.py --workers 4
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/worldbuilding/maps/map_new/data/Noryia Burgs 2026-08-29-11-38.csv"
CITIES = ROOT / "docs/worldbuilding/maps/map_new/cities"
INDEX = CITIES / "index.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def slug(value: str) -> str:
    return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", value.lower())) or "unnamed"


def asset_name(row: dict[str, str]) -> str:
    return f"{int(row['Id']):04d}-{slug(row['Burg'])}.png"


def refresh_index() -> None:
    if not INDEX.is_file():
        return
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    for city in index.get("cities", []):
        name = f"{int(city['id']):04d}-{slug(city['name'])}.png"
        city["downloaded"] = (CITIES / name).is_file()
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def worker(browser, tasks: list[dict[str, str]], done: dict[str, int]) -> None:
    context = await browser.new_context(
        viewport={"width": 1200, "height": 800},
        ignore_https_errors=True,
        user_agent=USER_AGENT,
    )
    page = await context.new_page()
    try:
        for row in tasks:
            url = (row.get("Preview link") or "").strip()
            dest = CITIES / asset_name(row)
            if dest.exists():
                continue
            last_error: Exception | None = None
            for _ in range(3):
                try:
                    await page.goto(url, timeout=45000, wait_until="load")
                    await page.wait_for_selector("canvas", timeout=45000)
                    await asyncio.sleep(0.8)
                    await page.screenshot(path=str(dest), full_page=True)
                    last_error = None
                    break
                except Exception as error:  # noqa: BLE001 - 重试瞬时连接/导航冲突
                    last_error = error
                    await asyncio.sleep(1)
            if last_error is None:
                done["ok"] += 1
                print(f"[{done['ok']}/{done['target']}] 已保存 {dest.name} ({row['Burg']})")
            else:
                done["fail"] += 1
                print(f"[跳过] {row['Burg']} (#{row['Id']}) 失败: {last_error}")
    finally:
        await context.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="并发渲染并补下 Noryia 城镇详图")
    parser.add_argument("--workers", type=int, default=2, help="并发浏览器页数")
    args = parser.parse_args()

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("缺少 playwright：pip install playwright")

    CITIES.mkdir(parents=True, exist_ok=True)
    with DATA.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    pending = [
        row
        for row in rows
        if (row.get("Preview link") or "").startswith("http")
        and not (CITIES / asset_name(row)).exists()
    ]
    done = {"ok": 0, "fail": 0, "target": len(pending)}
    print(f"待下载 {len(pending)} 张，并发 {args.workers} 页")

    chunks = [pending[index:: args.workers] for index in range(args.workers)]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge")
        try:
            await asyncio.gather(
                *[worker(browser, chunk, done) for chunk in chunks if chunk]
            )
        finally:
            await browser.close()

    refresh_index()
    print(f"\n完成：新增 {done['ok']} 张，失败 {done['fail']} 张")


if __name__ == "__main__":
    asyncio.run(main())
