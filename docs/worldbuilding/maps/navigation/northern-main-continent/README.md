# 北方主大陆候选导航数据

状态：`candidate`，等待人工审核；不得直接作为世界正典或运行时通行规则。

本目录从 `northern-main-continent.png` 的视觉地理参考和已确认的文字设定生成。当前分辨率为 `768×512`，候选世界坐标边界为经度 `[-170, 0]`、纬度 `[0, 72]`。

当前统计摘要：

- 陆地：约48.24%；
- 外海：约50.70%；
- 内陆水体与候选河流：约1.05%；
- 候选最高海拔：4396米；
- 候选山地格：31338；
- 候选雪地格：1754。

黄色线是候选道路，蓝色线是候选河流，白框是桥梁、港口或山口等转换门户，红点是已确认名称但候选几何位置的城市。

重新生成：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[terrain]"
.\.venv\Scripts\python.exe scripts\generate_northern_navigation_data.py
```

审核时优先打开 `audit-overlay.png`，并依次检查海岸、山脉、河流、道路和跨越门户。详细数据契约见 `docs/design/06-terrain-and-routing.md`。
