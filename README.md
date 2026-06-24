# Job Scanner

监控目标公司官网/ATS 的公开岗位 → 清洗 → 增量 diff(新增/更新/关闭)→ 规则打分 → 输出 apply queue。
**不自动申请、不自动发消息**,只做发现、筛选、打分、记录。

当前版本用**规则打分**,跑起来不需要任何 API key。LLM 分类是后面要加的一层(见文末)。

---

## 1. 安装(一次性)

需要 Python 3.10+。

```bash
cd job-scanner
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. 第一次跑(建立基线)

```bash
python scan.py --reset
```

`--reset` 会清空数据库,把"这一次抓到的所有岗位"当作基线。所以**第一次跑,所有岗位都是 NEW**,这是正常的。

你会看到三部分:
1. **Scan summary**:抓到多少岗位,以及 NEW / UPDATED / CLOSED 各多少。
2. **Fetch issues**(如果有):哪些公司没抓到。`404` 几乎都是 slug 写错 → 见 §4。这些公司会被跳过,不影响别人,也不会污染数据库。
3. **Apply Queue**:打分达标(默认 ≥45)的岗位,按分数排序。带 `NEW` 标记的是本次新增/更新。

结果同时写到 `output/apply_queue_<时间>.csv` 和 `.json`,可以直接拖进 Excel 看。

## 3. 之后每天/每周跑(看增量)

```bash
python scan.py
```

**不要**加 `--reset`。这次它会和上次的快照对比,只告诉你:
- 哪些是新开的岗位(NEW)
- 哪些岗位信息变了(UPDATED,比如改了 title / location / JD)
- 哪些岗位关掉了(CLOSED)

这就是你想要的"增量监控"——每天只看变化,不用重新扫一整页。

常用参数:
- `python scan.py --top 40` 队列显示更多行
- `python scan.py --no-close` 这次不做关闭判断(网络不稳时用)

---

## 4. 如何找 slug(关键)

`config/companies.yaml` 里每家公司要写对 `ats_type` 和 `slug`。slug 就是 ATS 看板的代号,找法:

打开目标公司的招聘页,看浏览器地址栏 / 或按 F12 看 Network 里的请求:

| ATS | 招聘页长这样 | slug 是 | 直接验证(浏览器打开) |
|-----|------------|--------|-------------------|
| **Greenhouse** | `boards.greenhouse.io/<slug>` 或 `job-boards.greenhouse.io/<slug>` | 那段 `<slug>` | `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs` |
| **Lever** | `jobs.lever.co/<slug>` | 那段 `<slug>` | `https://api.lever.co/v0/postings/<slug>?mode=json` |
| **Ashby** | `jobs.ashbyhq.com/<slug>` | 那段 `<slug>`(注意大小写,有的是 `Linear` 不是 `linear`) | `https://api.ashbyhq.com/posting-api/job-board/<slug>` |

把验证链接贴进浏览器:**出 JSON = slug 对了;404 = 错了**。
我在 `companies.yaml` 里给的 10 家是示例,**请逐个验证**——slug 会变,不保证全部还活着。先确认几家能出数据,再慢慢把你真正想盯的公司加进去。

> 还没覆盖的 ATS(Workday / iCIMS / SmartRecruiters 等):Workday 和 SmartRecruiters 也有规律的 JSON 端点,可以照着 `job_scanner/adapters.py` 里现成的三个再写一个 adapter,在文件底部 `ADAPTERS` 字典注册一下即可。需要的话告诉我,我帮你加。

---

## 5. 调打分(改 `config/preferences.yaml`)

- `roles.positive`:岗位关键词 → 分数(命中 title/department 取最高分)。想更看重 RevOps 就把它调高。
- `roles.negative`:命中就重罚(audit / 纯 SWE / ML 等),把你不想要的方向挡掉。
- `location`:Remote=100,NJ/PA/CT/MA/upstate NY=80,NYC=55(标了 commute concern),其它 onsite=20。
- `weights`:role 0.65 / location 0.35,自己调比例。
- `thresholds.apply_queue_min`:进队列的最低分,觉得队列太长就调高。

改完直接重跑 `python scan.py`,分数会按新配置重算(不需要 reset)。

---

## 6. 数据存在哪

- `jobs.db`:SQLite,所有岗位的当前状态(open/closed)、首次见到时间、打分。可用任何 SQLite 工具直接查。
- `events` 表:每次扫描的 new/updated/closed 事件流(岗位历史)。
- `scans` 表:每次扫描的汇总。
- `output/`:每次跑导出的 apply queue 文件。

查历史的例子:
```bash
sqlite3 jobs.db "SELECT ts,company,event,title FROM events ORDER BY id DESC LIMIT 20;"
```

---

## 7. 下一步:接上 LLM 分类(可选)

现在规则已经能砍掉大部分噪音。LLM 该放在**规则之后**,只处理边界模糊的岗位
(比如 "Strategic Finance" vs "Finance Systems" vs "RevOps" 这种你清单里高度重叠的方向),
输出一个 fit 理由和细分标签。这样既省 token 又稳定。

这一层我建议下一轮再加——你先把抓取和增量监控跑顺,确认数据质量,再谈分类要不要更聪明。
