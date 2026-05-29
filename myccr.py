import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse
import requests
import argparse
from typing import Dict, List, Any, Optional

# ==========================================
# 全局配置与常量
# ==========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "progress_payloads.json")

BASE_URL = "https://myccr.net:13710/api/v1"

DEEPSEEK_CONFIG = {
    "apiKey": "",
    "baseUrl": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
}

# 题目类型枚举
QUIZ_SINGLE_CHOICE = 200 # 单选题
QUIZ_MULTI_CHOICE = 300  # 多选题
QUIZ_TRUE_FALSE = 400  # 判断题
QTYPE_SINGLE_CHOICE = 200  # 单选题
QTYPE_MULTI_CHOICE = 210  # 多选题
QTYPE_TRUE_FALSE = 310  # 判断题
QTYPE_FILL_BLANK = 320  # 填空题
TFDictionary={"A":1,"B":2}

# 统一的交互输出风格
BANNER_WIDTH = 60

def print_section(title: str):
    """打印分节标题，统一交互界面风格"""
    print()
    print("─" * BANNER_WIDTH)
    print(f"  {title}")
    print("─" * BANNER_WIDTH)


def parse_args() -> Dict[str, Any]:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="处理课程进度和测试的自动化脚本")
    parser.add_argument("--token", type=str, default=None, help="用户的认证 Token（提供后将跳过账号登录）")
    parser.add_argument("--school", type=str, default=None, help="学校名称或简称（如 njupt / 南京邮电），用于学号登录")
    parser.add_argument("--student-id", dest="student_id", type=str, default=None, help="学号，用于学号登录")
    parser.add_argument("--password", type=str, default=None, help="登录密码（默认: 学校简称 + 学号）")
    parser.add_argument("--url", type=str, default=None, help="课程的 URL，用于提取 encrypted_id 和 catalog（留空将自动列出已加入课程）")
    parser.add_argument("--course-id", dest="course_id", type=int, default=None, help="课程 ID，用于从已加入课程中直接选定（非交互）")
    parser.add_argument("--delay", type=int, default=600, help="网络请求延迟时间，单位：毫秒 (默认: 600)")
    parser.add_argument("--only-id", type=int, default=None, help="仅处理特定的 Quiz ID")

    parser.add_argument("--dry-run", action="store_true", help="试运行模式，仅打印输出，不实际提交数据")
    parser.add_argument("--skip-report", action="store_true", help="跳过课程进度的上报阶段")
    parser.add_argument("--skip-quiz", action="store_true", help="跳过自动答题阶段")
    parser.add_argument("--force", action="store_true", help="强制执行，覆盖已有的答题记录")
    parser.add_argument("--no-ai", action="store_true", help="禁用 AI 辅助答题功能")

    args = vars(parser.parse_args())

    # 从 URL 中提取关键参数
    args["encrypted_id"] = None
    args["catalog"] = None
    if args.get("url"):
        parsed_url = parse_course_url(args["url"])
        args["encrypted_id"] = parsed_url.get("encrypted_id")
        args["catalog"] = parsed_url.get("catalog")

    args.pop("url", None)
    return args


def parse_course_url(url: str) -> Dict[str, str]:
    """
    解析课程 URL，提取加密的课程 ID 和分类 catalog。
    兼容前端 Hash 路由 (如 http://host/#/course?id=...&catalog=...)
    """
    # 处理 Vue/React 常见的 Hash 路由参数
    fragment = url.split("#", 1)[1] if "#" in url else url
    query_string = fragment.split("?", 1)[1] if "?" in fragment else fragment

    params = parse_qs(query_string)
    encrypted_id = params.get("id", [None])[0]
    catalog = params.get("catalog", [None])[0]

    if not encrypted_id or not catalog:
        print(f"错误: 无法从 URL 解析出 id 和 catalog")
        print(f"  输入的 URL: {url}")
        sys.exit(1)

    return {"encrypted_id": encrypted_id, "catalog": catalog}


# ==========================================
# 账号登录模块（学校 + 学号）
# ==========================================
_LOGIN_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://myccr.net",
    "Referer": "https://myccr.net/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0",
}


def lookup_school(keyword: str) -> Dict[str, Any]:
    """根据学校名称或简称查询学校信息，返回包含 id、name、prefix 的字典。

    优先精确匹配简称(prefix)或名称(name)，否则返回第一条结果。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        print("错误: 学校不能为空")
        sys.exit(1)

    try:
        resp = requests.get(
            f"{BASE_URL}/account/school/",
            params={"key": keyword, "pagenum": 1, "pagesize": 1000},
            headers=_LOGIN_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"错误: 查询学校失败: {e}")
        sys.exit(1)

    rows = (body.get("data") or {}).get("rows") or []
    if not rows:
        print(f"错误: 未找到学校 “{keyword}”，请尝试输入学校简称（如 njupt）或完整名称")
        sys.exit(1)

    kw_lower = keyword.lower()
    for s in rows:
        if str(s.get("prefix", "")).lower() == kw_lower or s.get("name") == keyword:
            return s

    if len(rows) > 1:
        print(f"  [提示] 匹配到 {len(rows)} 所学校，已自动选择第一所；如有误请用更精确的关键词或简称")
    return rows[0]


def login(school_keyword: str, student_id: str, password: Optional[str] = None) -> str:
    """使用学校 + 学号登录，返回认证 Token。

    密码默认为 “学校简称 + 学号”。
    """
    student_id = (student_id or "").strip()
    if not student_id:
        print("  [×] 学号不能为空")
        sys.exit(1)

    school = lookup_school(school_keyword)
    prefix = str(school.get("prefix", "")).strip()
    pwd = password if password else f"{prefix}{student_id}"

    print(f"  学校 : {school.get('name')} (简称 {prefix})")
    print(f"  学号 : {student_id}")
    if not password:
        print(f"  密码 : 默认（学校简称 + 学号）")

    payload = {"school": school["id"], "username": student_id, "password": pwd}
    try:
        resp = requests.post(
            f"{BASE_URL}/account/student/login/",
            json=payload,
            headers=_LOGIN_HEADERS,
            timeout=30,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [×] 登录请求失败: {e}")
        sys.exit(1)

    token = body.get("token")
    if not token:
        print(f"  [×] 登录失败: {body.get('message', '账号或密码错误')}")
        sys.exit(1)

    print("  [√] 登录成功")
    return token


def select_course(api: "APIClient", course_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """列出账号已加入的课程并返回选定的课程行。

    course_id 指定时直接匹配（非交互）；否则交互式选择。
    返回 None 表示无可用课程或用户放弃选择（调用方可回退到 URL 方式）。
    """
    courses = api.list_student_courses()
    if not courses:
        print("  [!] 未获取到已加入的课程")
        return None

    # 非交互：按课程 ID 直接匹配
    if course_id is not None:
        for c in courses:
            if c.get("id") == course_id:
                return c
        print(f"  [!] 已加入课程中未找到 ID={course_id}")
        return None

    print("  已加入的课程：")
    for i, c in enumerate(courses):
        title = c.get("title", "")
        cat = c.get("catalog_name", "")
        print(f"    [{i + 1}] {title}  （分类: {cat} | id={c.get('id')}）")

    while True:
        sel = input(f"  选择课程序号 [1-{len(courses)}]（回车 = 改用课程 URL）: ").strip()
        if not sel:
            return None
        if sel.isdigit() and 1 <= int(sel) <= len(courses):
            return courses[int(sel) - 1]
        print("  [!] 输入无效，请重新输入")


class APIClient:
    """封装 requests 的 API 客户端，处理认证、会话复用及基础异常拦截"""

    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": self.token,
            "Origin": "https://myccr.net",
            "Referer": "https://myccr.net/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0",
        })

    def _handle_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """通用的请求处理方法，统一拦截网络异常，防止程序直接崩溃"""
        url = f"{BASE_URL}/{endpoint}"
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            # 伪造一个失败的返回字典，适配原有业务逻辑
            return {"code": -1, "message": f"网络请求异常: {e}"}
        except ValueError:
            return {"code": -1, "message": "响应内容不是有效的 JSON 格式"}

    def get(self, endpoint: str, params: dict = None) -> Dict[str, Any]:
        return self._handle_request("GET", endpoint, params=params)

    def put(self, endpoint: str, body: dict) -> Dict[str, Any]:
        return self._handle_request("PUT", endpoint, json=body)

    def post(self, endpoint: str, body: dict) -> Dict[str, Any]:
        return self._handle_request("POST", endpoint, json=body)

    def list_student_courses(self) -> List[Dict[str, Any]]:
        """拉取当前账号已加入的全部课程列表。

        与课程详情共用 course/course/as/student/ 接口，不传 id 时返回 rows 列表。
        """
        result = self.get(
            "course/course/as/student/",
            {"pagenum": 1, "pagesize": 1000, "status": 3, "property": 0},
        )
        if result.get("code") != 0:
            print(f"获取课程列表失败: {result.get('message', '未知错误')}")
            return []
        return result.get("data", {}).get("rows", []) or []


def flatten_dirs(dirs: List[dict], dirs_q: List[dict], course_id: str, base_dir_id: str = None, path: str = "") -> List[dict]:
    """递归遍历课程目录树，将所有资源平铺为一维列表，方便后续批量处理"""
    results = []
    for d in dirs:
        dir_id = d["id"]
        title = d.get("title", "")
        cur_path = f"{path}/{title}" if path else title

        # 1. 提取普通视频/文档资源
        for r in d.get("resource", {}).get("resource", []):
            results.append({
                "title": r.get("title", ""),
                "type": r.get("type", ""),
                "duration": r.get("duration"),
                "id": r["id"],
                "course_id": course_id,
                "dir_id": dir_id,
                "content_type_id": r["content_type_id"],
                "object_id": r["object_id"],
                "type_id": r.get("type_id", 0),
                "rate": 100,  # 进度设为 100%
                "dir_path": cur_path,
            })

        # 2. 提取测试题 (Quiz)
        for q in d.get("quiz", {}).get("quiz_chapter", []):
            results.append({
                "title": q.get("title", ""),
                "type": "quiz",
                "quiz_id": q.get("quiz"),
                "id": q["id"],
                "course_id": course_id,
                "dir_id": dir_id,
                "content_type_id": 76,
                "object_id": q.get("quiz", 0),
                "type_id": q.get("type_id", 0),
                "rate": 100,
                "dir_path": cur_path,
            })

        # 3. 提取实验 (Experiment)
        for exp in d.get("experiment", {}).get("experiment", []):
            results.append({
                "title": exp.get("title", ""),
                "type": "experiment",
                "id": exp["id"],
                "course_id": course_id,
                "dir_id": dir_id,
                "content_type_id": 76,
                "object_id": exp.get("id", 0),
                "type_id": exp.get("type", 0),
                "rate": 100,
                "dir_path": cur_path,
            })

        # 4. 递归处理子目录
        if d.get("children"):
            results.extend(flatten_dirs(d["children"],[], course_id, dir_id, cur_path))

    for q in dirs_q:
        dir_id = q["id"]
        title = q.get("title", "")
        cur_path = f"{path}/{title}" if path else title
        results.append({
            "title": q.get("title", ""),
            "type": "quiz",
            "quiz_id": q.get("quiz"),
            "id": q["id"],
            "course_id": course_id,
            "dir_id": dir_id,
            "content_type_id": 76,
            "object_id": q.get("quiz", 0),
            "type_id": q.get("type_id", 0),
            "rate": 100,
            "dir_path": cur_path,
        })

    return results


def stage_extract(api: APIClient, encrypted_id: str, catalog: str, course: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """阶段一：拉取课程详细信息，解析目录并生成任务清单。

    若已通过课程列表选定 course（含 id/team_id/title 等字段），则直接复用，
    无需再以 encrypted_id + catalog 查询。
    """
    print("=" * 70)
    print("阶段 1/3: 提取课程资源")
    print("=" * 70)

    if course is None:
        course_data = api.get("course/course/as/student/", {"id": encrypted_id, "catalog": catalog})
        if course_data.get("code") != 0 or not course_data.get("data", {}).get("rows"):
            print(f"提取课程信息失败: {course_data.get('message', '未知错误')}")
            sys.exit(1)
        course = course_data["data"]["rows"][0]

    course_id = str(course["id"])
    team_id = course["team_id"]
    catalog_name = course.get("catalog_name", "")
    course_title = course["title"]

    print(f"  课程: {course_title}")
    print(f"  分类: {catalog_name}")
    print(f"  ID信息: course_id={course_id}, team_id={team_id}")

    # 拉取课程目录树
    dir_data = api.get("team/TeamDir/dir/", {"id": course_id})
    dirs = dir_data.get("data", {}).get("dirs", [])

    dir_q_data = api.get("question/teamquiz/student/", {"course": course_id})
    dirs_q = dir_q_data.get("data", {}).get("quiz", [])

    payloads = flatten_dirs(dirs, dirs_q, course_id)

    # 统计资源类型
    type_counts = {}
    for p in payloads:
        t = p.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"  总计: {len(payloads)} 个资源")
    for t, c in sorted(type_counts.items()):
        print(f"    - {t:10s}: {c} 个")

    # 保存缓存文件以备查验
    output = {
        "course_id": course_id,
        "team_id": team_id,
        "course_title": course_title,
        "catalog": catalog_name,
        "total": len(payloads),
        "type_summary": type_counts,
        "payloads": payloads,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  清单已保存至: {OUTPUT_FILE}")
    return output


def stage_report(api: APIClient, data: Dict[str, Any], dry_run: bool, delay_ms: int):
    """阶段二：遍历清单，向服务器发送学习进度（刷课）"""
    print("\n" + "=" * 70)
    print("阶段 2/3: 上报学习进度")
    print("=" * 70)

    payloads = data["payloads"]
    non_quiz = [p for p in payloads if p.get("type") != "quiz"]

    print(f"  资源总数: {len(payloads)} (Quiz: {len(payloads) - len(non_quiz)}, 其它资源: {len(non_quiz)})")
    print(f"  请求间隔: {delay_ms} ms")
    print(f"  运行模式: {'DRY RUN（仅打印不发包）' if dry_run else '实际发包'}")

    success, fail = 0, 0

    for i, p in enumerate(non_quiz):
        pid = p["id"]
        title = p["title"][:40]
        ptype = p["type"]
        body = {
            "id": p["id"],
            "course_id": p["course_id"],
            "dir_id": p["dir_id"],
            "content_type_id": p["content_type_id"],
            "object_id": p["object_id"],
            "type_id": p["type_id"],
            "rate": p["rate"],
        }

        if dry_run:
            success += 1
            print(f"  [{i + 1}/{len(non_quiz)}] [DRY-RUN] {ptype:10s} id={pid:6d}  {title}")
            continue

        # 实际发包逻辑
        result = api.put(f"course/course/progress/{pid}/", body)
        if result.get("code") == 0:
            success += 1
            status = "OK"
        else:
            fail += 1
            status = f"FAIL: {result.get('message', '未知错误')}"

        print(f"  [{i + 1}/{len(non_quiz)}] {status:30s} {ptype:10s} id={pid:6d}  {title}")
        time.sleep(delay_ms / 1000.0)  # 毫秒转秒

    print(f"\n  进度上报结果: 成功 {success} 个, 失败 {fail} 个")


# ==========================================
# 答题与 AI 辅助模块
# ==========================================

def strip_html(text: str) -> str:
    """清除题目描述中的 HTML 标签"""
    return re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").strip()


def normalize_answer(raw: Any, qtype: int) -> Any:
    """标准化不同题型的答案格式，适配服务端接收要求"""
    if raw is None:
        return ""
    if qtype == QTYPE_MULTI_CHOICE:
        if isinstance(raw, str):
            return sorted(list(raw.upper()))
        if isinstance(raw, list):
            return sorted([str(x).upper() for x in raw])
        return raw
    if qtype == QTYPE_FILL_BLANK:
        if isinstance(raw, str) and raw.startswith("{"):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        return raw
    return str(raw).upper()


def call_deepseek(questions: List[dict]) -> Dict[int, List[str]]:
    """调用 DeepSeek 接口对未作答的题目进行求解"""
    prompt_lines = [
        "你要解答选择题，并返回可被脚本解析的答案。",
        "要求：",
        "1. 每题最后单独输出一行标准答案。",
        "2. 标准答案格式必须严格是：第1题：A 或 第2题：AC。",
        "3. 不要省略题号，不要把多个题目的标准答案写在同一行。",
        "",
    ]

    for i, q in enumerate(questions):
        prompt_lines.append(f"第 {i + 1} 题：{q['desc']}")
        for opt in q["options"]:
            prompt_lines.append(f"{opt['k']}. {opt['v']}")
        prompt_lines.append("")

    payload = {
        "model": DEEPSEEK_CONFIG["model"],
        "messages": [
            {"role": "system", "content": "你是一个严谨的选择题答题助手。你必须严格按照用户要求输出每题的答案。"},
            {"role": "user", "content": "\n".join(prompt_lines)},
        ],
        "stream": False,
    }

    endpoint = DEEPSEEK_CONFIG["baseUrl"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_CONFIG['apiKey']}"}

    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return parse_ai_response(content)
    except requests.RequestException as e:
        print(f"  [!] 请求 AI 接口失败: {e}")
        return {}


def parse_ai_response(text: str) -> Dict[int, List[str]]:
    """使用正则提取 AI 回复中的题号和答案"""
    answer_map = {}
    for line in text.split("\n"):
        line = line.strip()
        # 匹配诸如 "第1题：A" 或 "1. 答案：AB"
        m = re.match(r"(?:第\s*(\d+)\s*题[：:]\s*|(\d+)\s*[\.\、:：]\s*)(?:答案[：:]\s*)?([A-Z]+)", line, re.I)
        if m:
            qnum = int(m.group(1) or m.group(2))
            ans = m.group(3).upper()
            answer_map[qnum] = sorted(list(ans))
    return answer_map


def fetch_correct_answers(api: APIClient, quiz_id: int) -> List[Dict[str, Any]]:
    """通过试卷解析接口 question/quiz/preview/<quiz_id>/ 获取每题的正确答案。

    返回按试卷顺序排列的列表，每项为 {answer, ids}，其中 ids 收集了该题
    所有可用的标识字段（id / r_id / p_id），供后续按 id 或按顺序匹配。

    注意：解析接口的题目层级用的是复数 key —— questiongroups[].questions[]、
    quizparts[].questiongroups[].questions[]，与答题缓存(question 单数)不同。
    """
    preview = api.get(f"question/quiz/preview/{quiz_id}/", None)
    data = preview.get("data")
    if not data:
        return []

    ordered: List[Dict[str, Any]] = []

    def collect(question: dict):
        ans = question.get("answer")
        ids = {}
        for key in ("id", "r_id", "p_id"):
            if question.get(key) is not None:
                ids[key] = question[key]
        ordered.append({"answer": ans if ans is not None else "", "ids": ids})

    def walk_groups(groups: List[dict]):
        for g in groups:
            # 解析接口用复数 questions；兼容个别返回单数 question 的情况
            for q in g.get("questions", g.get("question", [])):
                collect(q)

    walk_groups(data.get("questiongroups", []))
    for part in data.get("quizparts", []):
        walk_groups(part.get("questiongroups", part.get("groups", [])))

    return ordered


def solve_one_quiz(api: APIClient, quiz_entry: dict, team_id: int, dry_run: bool, force: bool, use_ai: bool, delay_ms: int = 600) -> \
Optional[Dict[str, int]]:
    """阶段三：处理单个测试题的核心逻辑"""
    quiz_chapter_id = quiz_entry["id"]
    title = quiz_entry["title"]
    quiz_id = quiz_entry["object_id"]

    print(f"\n  {'─' * 60}")
    print(f"  处理 Quiz: {title} (chapter_id={quiz_chapter_id}, quiz_id={quiz_id})")

    # 1. 尝试开始答题 / 恢复答题进度
    start_data = api.get("question/teamquiz/start/", {"id": quiz_chapter_id})
    if start_data.get("code") != 0:
        print(f"  无法开启测验: {start_data.get('message')}")
        return None

    result_id = start_data["data"]["result"]
    attempt = start_data["data"]["attempt"]

    if not start_data["data"].get("rqmode", True):
        print("  测验已提交完成，且不允许重做，跳过...")
        return None

    # 2. 拉取缓存中的题目详情
    quiz_data = api.get("question/quizanswer/cache/", {"id": result_id})
    if "data" not in quiz_data:
        print("  获取题目缓存失败，跳过...")
        return None

    all_questions = []
    unanswered_questions = []

    # 解析所有题目
    for g in quiz_data["data"].get("questiongroups", []):
        for q in g.get("question", []):
            qobj = {
                "r_id": q["r_id"],
                "answer_id": q["answer_id"],
                "group": g["group_id"],
                "type": g["type_id"],
                "desc": strip_html(q.get("discription", "")),
                "options": q.get("option", []),
                "answer": q.get("answer") or "",
                "choose": q.get("choose", False),  # 是否已作答
            }
            all_questions.append(qobj)
            if not qobj["choose"] and not qobj["answer"]:
                unanswered_questions.append(qobj)

    for g in quiz_data["data"].get("quizparts", []):
        for b in g.get("groups", []):
            for q in b.get("question", []):
                qobj = {
                    "r_id": q["r_id"],
                    "answer_id": q["answer_id"],
                    "group": b["group_id"],
                    "type": b["type_id"],
                    "desc": strip_html(q.get("discription", "")),
                    "options": q.get("option", [{"k": "A", "v": "正确"},{"k": "B", "v": "错误"}]),
                    "answer": q.get("answer") or "",
                    "choose": q.get("choose", False),  # 是否已作答
                }
                all_questions.append(qobj)
                if not qobj["choose"] and not qobj["answer"]:
                    unanswered_questions.append(qobj)


    total_q = len(all_questions)
    print(
        f"  状态: 共 {total_q} 题 | 已作答 {total_q - len(unanswered_questions)} 题 | 待作答 {len(unanswered_questions)} 题")

    # 3. 呼叫 AI 处理未答题目
    if unanswered_questions and use_ai:
        print(f"  正在请求 AI 辅助答题 ({len(unanswered_questions)} 题)...")
        ai_answers = call_deepseek(unanswered_questions)
        print(f"  AI 成功返回了 {len(ai_answers)} 题的答案")

        # 将 AI 的答案注入到本地数据中
        for qnum, ans in sorted(ai_answers.items()):
            idx = qnum - 1
            if idx < len(unanswered_questions):
                final_answer = "".join(ans)
                if unanswered_questions[idx]["type"] == 400:
                    final_answer = TFDictionary[unanswered_questions[idx]["answer"]]
                unanswered_questions[idx]["answer"] = final_answer
                unanswered_questions[idx]["ai_solved"] = True
                print(f"    AI 推理: 第 {qnum} 题 → {''.join(ans)}")

    ok_count, skip_count, fail_count = 0, 0, 0

    # 4. 逐题提交答案 —— 已废弃（错误逻辑）
    # 章末习题的正确上报方式是下方第 5 步的整卷 /submit 接口，
    # 单题 question/quizanswer/quiz/... 接口为旧的错误逻辑，故整体注释掉。
    # for idx, q in enumerate(all_questions):
    #     r_id = q["r_id"]
    #     answer_val = q["answer"]
    #     already_answered = q["choose"]
    #     qtype = q["type"]
    #
    #     if not answer_val:
    #         skip_count += 1
    #         print(f"  [跳过] 第 {idx + 1} 题: 尚未有答案")
    #         continue
    #
    #     if already_answered and not force:
    #         skip_count += 1
    #         print(f"  [跳过] 第 {idx + 1} 题: 已作答过 (答案={answer_val})")
    #         continue
    #
    #     answer = normalize_answer(answer_val, qtype)
    #     payload = {
    #         "group": q["group"],
    #         "question": r_id,
    #         "answer": answer,
    #         "answer_id": q["answer_id"],
    #         "questiontype": qtype,
    #         "quiz": quiz_id,
    #         "team": team_id,
    #         "attempt": attempt,
    #     }
    #
    #     extra_tag = " [AI生成]" if q.get("ai_solved") else ""
    #
    #     if dry_run:
    #         ok_count += 1
    #         print(f"  [DRY-RUN] 第 {idx + 1} 题 提交 → {answer_val}{extra_tag} | {q['desc'][:20]}...")
    #         time.sleep(0.05)
    #         continue
    #
    #     # 实际提交单题答案
    #     endpoint = f"question/quizanswer/quiz/{quiz_id}/question/{r_id}/attempt/{attempt}/"
    #     result = api.post(endpoint, payload)
    #
    #     if result.get("code") == 0:
    #         ok_count += 1
    #         print(f"  [成功] 第 {idx + 1} 题 提交 → {answer_val}{extra_tag}")
    #     else:
    #         fail_count += 1
    #         print(f"  [失败] 第 {idx + 1} 题: {result.get('message', '未知错误')}")
    #     time.sleep(0.6)  # 保护性延时

    # 5. 提交整份考卷 (交卷) —— 章末习题正确的上报逻辑
    # 交卷成功即代表该测验完成，平台会据此自动计算进度，
    # 无需再单独调用 course/course/progress（见下方第 6 步说明）。
    submit_body = {"quiz": quiz_id, "team": team_id, "attempt": attempt, "platform": "网页端"}
    if dry_run:
        print(f"  [DRY-RUN] 模拟交卷")
        ok_count += 1
    else:
        result = api.post("question/submit/", submit_body)
        if result.get("code") == 0:
            ok_count += 1
            print(f"  考卷提交状态: 成功")
        else:
            fail_count += 1
            print(f"  考卷提交状态: 失败: {result.get('message', '未知错误')}")
        time.sleep(0.6)

    # 5.5 错题修正：交卷后从试卷解析接口获取标准答案，逐题订正后重新交卷，确保全对
    preview_answers = fetch_correct_answers(api, quiz_id)
    if not preview_answers:
        print("  [!] 未能获取标准答案，跳过错题修正")
    elif len(preview_answers) != len(all_questions):
        # 题数对不上则不敢按顺序匹配，避免张冠李戴
        print(f"  [!] 标准答案题数({len(preview_answers)})与试卷题数({len(all_questions)})不一致，跳过错题修正")
    else:
        # 解析接口的题目不带 r_id，但与答题缓存同序排列，故按位置匹配。
        # 同时建立 id 索引（id/r_id/p_id → 答案）作为兜底校验。
        id_to_answer = {}
        for item in preview_answers:
            for v in item["ids"].values():
                id_to_answer[v] = item["answer"]

        def canon(val: Any) -> str:
            # 统一比较口径：去空白、转大写、对多选答案按字母排序，避免顺序差异误判
            s = "".join(str(val or "").split()).upper()
            return "".join(sorted(s)) if s.isalpha() else s

        # 找出当前答案与标准答案不一致的题目
        wrong = []
        for idx, q in enumerate(all_questions):
            # 优先用 id 匹配（缓存的 r_id / p_id 可能等于解析接口的 id），否则按顺序
            std = id_to_answer.get(q.get("r_id"))
            if std is None:
                std = id_to_answer.get(q.get("p_id"))
            if std is None:
                std = preview_answers[idx]["answer"]
            if std in (None, ""):
                continue
            if canon(q.get("answer")) != canon(std):
                wrong.append((q, std))

        if not wrong:
            print("  错题修正: 全部正确，无需订正")
        else:
            print(f"  错题修正: 检测到 {len(wrong)} 题需要订正")
            fixed = 0
            for q, std in wrong:
                r_id = q["r_id"]
                qtype = q["type"]
                answer = normalize_answer(std, qtype)
                payload = {
                    "group": q["group"],
                    "question": r_id,
                    "answer": answer,
                    "answer_id": q["answer_id"],
                    "questiontype": qtype,
                    "quiz": quiz_id,
                    "team": team_id,
                    "attempt": attempt,
                }
                if dry_run:
                    fixed += 1
                    print(f"  [DRY-RUN] 订正 r_id={r_id} → {std}")
                    time.sleep(0.05)
                    continue

                endpoint = f"question/quizanswer/quiz/{quiz_id}/question/{r_id}/attempt/{attempt}/"
                res = api.post(endpoint, payload)
                if res.get("code") == 0:
                    fixed += 1
                    q["answer"] = std
                    print(f"  [订正] r_id={r_id} → {std}")
                else:
                    print(f"  [订正失败] r_id={r_id}: {res.get('message', '未知错误')}")
                time.sleep(delay_ms / 1000.0)

            # 订正后重新交卷
            if dry_run:
                print(f"  [DRY-RUN] 模拟重新交卷（已订正 {fixed} 题）")
            else:
                res = api.post("question/submit/", submit_body)
                status = "成功" if res.get("code") == 0 else f"失败: {res.get('message', '未知错误')}"
                print(f"  订正后重新交卷: {status}（共订正 {fixed} 题）")
                time.sleep(0.6)

    # 6. 标记测验学习进度 —— 已废弃（错误逻辑）
    # 测验（章末习题）的完成度由第 5 步的整卷 /submit 接口决定，
    # course/course/progress 是视频/文档资源（sendNoticeAfterStudy）的上报方式，
    # 对测验调用会返回失败，故整体注释掉。
    # progress_body = {
    #     "id": quiz_entry["id"],
    #     "course_id": quiz_entry["course_id"],
    #     "dir_id": quiz_entry["dir_id"],
    #     "content_type_id": quiz_entry["content_type_id"],
    #     "object_id": quiz_entry["object_id"],
    #     "type_id": quiz_entry["type_id"],
    #     "rate": quiz_entry["rate"],
    # }
    #
    # if dry_run:
    #     print(f"  [DRY-RUN] 模拟测验进度上报")
    # else:
    #     result = api.put(f"course/course/progress/{quiz_entry['id']}/", progress_body)
    #     print(f"  测验进度上报: {'成功' if result.get('code') == 0 else '失败'}")
    #     time.sleep(0.6)

    print(f"  小计: 交卷成功 {ok_count} 份 | 失败 {fail_count} 份")
    return {"ok": ok_count, "skip": skip_count, "fail": fail_count}


def stage_quiz(api: APIClient, data: Dict[str, Any], dry_run: bool, force: bool, use_ai: bool, delay_ms: int,
               only_id: int):
    """阶段三入口：过滤筛选所有测试卷并批量派发处理任务"""
    print("\n" + "=" * 70)
    print("阶段 3/3: 自动答题")
    print("=" * 70)

    quizzes = [p for p in data["payloads"] if p.get("type") == "quiz"]
    if only_id:
        quizzes = [q for q in quizzes if q["id"] == only_id]
        if not quizzes:
            print(f"  未找到指定 ID ({only_id}) 的测验。")
            return

    team_id = data.get("team_id", 9558)

    labels = []
    if dry_run: labels.append("DRY RUN")
    if force:   labels.append("强制重答")
    if not use_ai: labels.append("禁用AI")

    print(f"  需处理 Quiz 总数: {len(quizzes)}")
    print(f"  运行模式: {' | '.join(labels) if labels else '正常提交模式'}")

    total_ok, total_skip, total_fail = 0, 0, 0

    for quiz_entry in quizzes:
        result = solve_one_quiz(api, quiz_entry, team_id, dry_run, force, use_ai, delay_ms)
        if result:
            total_ok += result["ok"]
            total_skip += result["skip"]
            total_fail += result["fail"]
        time.sleep(1)  # 两份考卷之间额外休息一秒

    print(f"\n  答题阶段全部完成: 累计交卷成功 {total_ok} 份, 失败 {total_fail} 份")


def main():
    args = parse_args()
    print("=" * BANNER_WIDTH)
    print("  myccr 刷课助手")
    print("  开源: https://github.com/ecxwxz/myccr  |  猫娘交流群 105859360")
    print("=" * BANNER_WIDTH)

    # ① 账号登录：优先使用 Token，否则使用 学校 + 学号 登录
    print_section("① 账号登录")
    token = args["token"]
    if token:
        print("  已通过命令行提供 Token，跳过登录")
    else:
        school = args["school"]
        student_id = args["student_id"]

        # 未通过命令行提供登录信息时，进入交互式登录
        if not school and not student_id:
            mode = input("  登录方式  [1] 学号登录(默认)   [2] 直接输入 Token : ").strip()
            if mode == "2":
                token = input("  平台认证 Token : ").strip()
                if not token:
                    print("  [×] 必须提供认证 Token")
                    sys.exit(1)

        if not token:
            if not school:
                school = input("  学校（名称或简称，如 njupt）: ").strip()
            if not student_id:
                student_id = input("  学号 : ").strip()
            password = args["password"]
            if not password:
                pwd_in = input("  密码（回车 = 默认: 学校简称+学号）: ").strip()
                password = pwd_in or None
            token = login(school, student_id, password)

    api = APIClient(token)

    # ② 选择课程：优先 URL/命令行参数，否则自动列出已加入课程供选择
    print_section("② 选择课程")
    encrypted_id = args["encrypted_id"]
    catalog = args["catalog"]
    selected_course = None

    if encrypted_id and catalog:
        print(f"  已通过命令行提供课程参数 (id={encrypted_id})")
    else:
        # 命令行指定了课程 ID：直接匹配
        if args["course_id"] is not None:
            selected_course = select_course(api, args["course_id"])
            if not selected_course:
                sys.exit(1)
        else:
            # 交互：先尝试自动列出课程，回车则回退到手动输入 URL
            selected_course = select_course(api)
            if not selected_course:
                url = input("  课程 URL（用于提取参数）: ").strip()
                if url:
                    parsed = parse_course_url(url)
                    encrypted_id = parsed["encrypted_id"]
                    catalog = parsed["catalog"]
                else:
                    print("  [×] 必须选择课程或提供课程 URL")
                    sys.exit(1)

    # ③ 运行参数：请求间隔 + AI 答题
    print_section("③ 运行参数")
    delay_in = input(f"  请求间隔 ms（回车 = 默认 {args['delay']}）: ").strip()
    if delay_in:
        if delay_in.isdigit():
            args["delay"] = int(delay_in)
        else:
            print(f"  [!] 输入无效，沿用默认 {args['delay']}ms")

    if not args["no_ai"]:
        ds_key = input("  DeepSeek API Key（回车 = 禁用 AI 答题）: ").strip()
        if ds_key:
            DEEPSEEK_CONFIG["apiKey"] = ds_key
        else:
            args["no_ai"] = True

    # 运行前确认
    print_section("即将开始")
    print(f"  Token   : {token[:20]}...{token[-5:] if len(token) > 25 else ''}")
    if selected_course:
        print(f"  课程    : {selected_course.get('title')} (id={selected_course.get('id')})")
    else:
        print(f"  课程    : id={encrypted_id} | 分类={catalog}")
    print(f"  请求间隔: {args['delay']}ms")
    print(f"  AI 答题 : {'启用 (' + DEEPSEEK_CONFIG['model'] + ')' if not args['no_ai'] else '禁用'}")
    flags = []
    if args["dry_run"]: flags.append("试运行")
    if args["force"]:   flags.append("强制重答")
    if args["skip_report"]: flags.append("跳过进度上报")
    if args["skip_quiz"]:   flags.append("跳过答题")
    print(f"  其它    : {' | '.join(flags) if flags else '无'}")

    # 阶段 1: 数据拉取与结构提取
    data = stage_extract(api, encrypted_id, catalog, selected_course)

    # 阶段 2: 课程进度上报
    if not args["skip_report"]:
        stage_report(api, data, args["dry_run"], args["delay"])
    else:
        print("\n  [提示] 已跳过 阶段 2: 上报学习进度")

    # 阶段 3: AI 自动辅助答题
    if not args["skip_quiz"]:
        stage_quiz(api, data, args["dry_run"], args["force"], not args["no_ai"], args["delay"], args["only_id"])
    else:
        print("\n  [提示] 已跳过 阶段 3: 自动答题")

    print()
    print("=" * BANNER_WIDTH)
    print("  任务全部执行完毕！")
    print("=" * BANNER_WIDTH)


if __name__ == "__main__":
    main()