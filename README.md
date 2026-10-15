# 北京租房助手（Beijing Rental Assistant）

求职季自用：一条命令把公司周边的租房市场（贝壳 / 自如 / 豆瓣小组）扫一遍，
自动完成**通勤测算 → 硬过滤 → 多维评分 → 下架复查 → CSV 日报**，每天定时跑，桌面弹窗通知。

- 1157 条原始房源 → 硬过滤后 45 条在租候选
- 通勤口径：高德 v5 电动车路径规划（真实骑行距离/时间），无 key 时地铁站耗时表兜底
- 人工只需做一件事：偶尔点一下验证码（30 分钟内完成即自动继续，Windows 气泡通知提醒）

![自如价格雪碧图 alpha 通道解码](forum_ziroom_sprite.png)

![贝壳验证码人工接管通知](forum_beike_captcha.png)

## 功能

- **多渠道采集**（Playwright 有头浏览器，拟人化随机延迟、长停顿、seen 缓存）
  - 贝壳：强制有头模式，点选验证码 30 分钟人工接管窗口 + 系统通知
  - 自如：列表页免登录；价格雪碧图 **alpha 通道解码**（见下）
  - 豆瓣：9 个租房小组滚动扫描
  - 安居客 / 58：代码内置，默认关闭
- **硬过滤**：整租、独卫、2000–4000 元、≥25㎡、户型明确、通勤 ≤60 分钟、距公司 ≤10km；
  贝壳另有行政区闸门、片区关键词相关性、坐标与卡片地铁站 ≤2km 校验
- **评分模型**（权重可在 `config/filters.json` 调整）：
  通勤 35 / 性价比 25 / 户型 12 / 舒适度 10 / 面积 8 / 发布方 5 / 可信度 5
- **每日一条龙** `daily`：采集 → 旧链接下架复查 → 评分 → 导出 6 张 CSV（推荐排序 / 全部房源 / 硬过滤明细 / 降价 / 新上 / 片区价格统计）
- Windows 计划任务每天 09:00 自动运行，跑完弹窗通知并自动打开报表

## 技术亮点

**1. 自如价格雪碧图：形状藏在 alpha 通道**

自如列表页价格是一张 300×28 的雪碧图，RGB 通道每个像素都是同一个橙色 (255,150,30)，
数字形状只存在于 alpha 通道。按 21.4px 步长切出 10 个槽位，
统计非透明像素特征与参考图匹配，得到槽位 → 数字映射
`[6,5,4,7,0,9,8,2,1,3]`，价格零误差还原。

**2. 人机协作：验证码不硬刚**

贝壳出现点选验证码时不尝试破解，而是：暂停翻页 → Windows 气泡通知
"贝壳需要人机验证，请在浏览器窗口点选验证，30分钟内完成即自动继续"
→ 检测到通过后自动续跑；超时则该渠道冷却 60–120 分钟，其余渠道不受影响。

**3. 数据自校准**

公司坐标、31 个地铁站坐标均用高德 POI 检索校准；地理编码与路径规划结果落盘缓存，
避免重复消耗 API 配额（个人 key 每日 100 次）。

## 目录结构

```
config/        filters.json 预算/户型/过滤/评分/渠道开关；areas.json 片区与地铁站坐标
core/          geo 通勤测算、score 过滤评分、storage SQLite、export CSV、notify 桌面通知
spiders/       beike / ziroom / social / anjuke_58 采集器，base 浏览器基类
run.py         命令行入口
install_task.ps1 / 安装定时任务.bat   注册每天 09:00 的 Windows 计划任务
data/          运行时数据（浏览器登录态、SQLite、缓存，.gitignore 排除）
```

## 快速开始

Windows + Python 3.10+：

```powershell
pip install -r requirements.txt
python -m playwright install chromium
python run.py init

# 首次使用，人工登录各渠道（登录态保存在本地 data/profile/，不会上传）
python run.py login --source beike
python run.py login --source ziroom
python run.py login --source social

python run.py daily          # 立即跑一次完整流程
# 或双击 每日找房.bat；右键管理员运行 安装定时任务.bat 注册每日 09:00 自动执行
```

### 可选：高德 API（精确通勤）

申请高德开放平台免费 Web 服务 key 后设置环境变量（**不要把 key 写进代码**）：

```powershell
setx AMAP_KEY "你的key"
```

不设置也能用：自动退回到地铁站耗时表启发式估算。

### 改成你自己的城市 / 公司

1. 编辑 `config/areas.json`：公司坐标、关注片区与地铁站
2. 编辑 `config/filters.json`：预算、户型、通勤上限、评分权重、渠道开关

## 命令一览

| 命令 | 说明 |
| --- | --- |
| `python run.py login --source beike` | 人工登录并保存登录态 |
| `python run.py collect [--source X] [--headless]` | 采集（默认全部启用渠道） |
| `python run.py score` | 通勤测算 + 硬过滤 + 评分 |
| `python run.py check [--limit 30]` | 复访旧链接，标记已下架 |
| `python run.py export` | 导出当日 CSV 报表 |
| `python run.py daily [--headless] [--no-open]` | 以上一条龙 + 弹窗通知 |

## 免责声明

本项目仅用于个人学习 Python / Playwright / 数据处理，采集时保持低频、随机延迟，
不做任何分布式或高并发抓取。请遵守目标网站的服务条款与 robots 规则；
请勿用于商业用途或大规模分发采集数据。
