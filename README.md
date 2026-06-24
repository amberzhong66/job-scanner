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

## 7. AI 精判层(DeepSeek,可选)

规则负责砍量,AI 负责读 JD 正文、判断那些标题骗人的岗位(标题没关键词但其实是好岗位 / 标题命中关键词但其实不对路)。**没 key 就还是纯规则跑,加了 key 自动启用。**

### 7.1 拿一个 DeepSeek key
1. 去 platform.deepseek.com 注册(无需信用卡),充一点点钱(几块钱能用很久)。
2. 在 API Keys 页面生成一个 key,形如 `sk-xxxxxxxx`。

### 7.2 把 key 放进 .env(不会进 Git)
在 `job-scanner` 文件夹里建一个 `.env` 文件:
```bash
echo 'DEEPSEEK_API_KEY=sk-把你的key贴这里' > .env
```
`.gitignore` 已经挡掉 `.env`,所以它永远不会被 `git push` 上去。

### 7.3 装库 + 跑
```bash
pip install openai        # 已在 requirements.txt 里
python scan.py --reset    # 第一次会把规则筛过的岗位逐个让 AI 判一遍
```
之后正常 `python scan.py`,**AI 只判新增/更新的岗位**,判过的按 content_hash 缓存,不重复花钱。运行结尾会显示 `AI: N new verdicts, M reused from cache`。

### 7.4 队列怎么变
有 AI 之后,`Fit` 列会显示 **STRONG / MAYBE / NO**,还带一句 AI 给的理由:
- AI 判 `no` 的岗位**直接从队列剔除**,哪怕规则分很高(这就是用来杀假阳性的)。
- `strong` 排最前,然后 `maybe`,再按规则分排序。
- AI 没看过的岗位,回退到规则门槛,不会凭空消失。

### 7.5 成本与开关
- 模型默认 `deepseek-v4-flash`(便宜,分类够用),在 `preferences.yaml` 的 `ai.model` 改。
- `python scan.py --no-ai` 强制纯规则;`--max-ai 50` 限制本次最多调用 50 次(防第一次扫太多家时意外花钱);`--refresh-ai` 忽略缓存重判。
- 想让更多边缘岗位进 AS(标题怪但可能是好岗位),把 `preferences.yaml` 里 `ai.prefilter_min` 调低甚至设 0;想省钱就调高。

### 7.6 个人画像
AI 判断依据来自 `config/profile.md` —— 我已按你的背景填好(finance+IT liaison、tax automation、在补 SQL/dbt/BigQuery、不要纯 accounting/audit 也不要纯 SWE)。想让 AI 判得更准,就用大白话改这个文件。
