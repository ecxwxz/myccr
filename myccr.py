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

def parse_args() -> Dict[str, Any]:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="处理课程进度和测试的自动化脚本")
    parser.add_argument("--token", type=str, default=None, help="用户的认证 Token")
    parser.add_argument("--url", type=str, default=None, help="课程的 URL，用于提取 encrypted_id 和 catalog")
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


def stage_extract(api: APIClient, encrypted_id: str, catalog: str) -> Dict[str, Any]:
    """阶段一：拉取课程详细信息，解析目录并生成任务清单"""
    print("=" * 70)
    print("阶段 1/3: 提取课程资源")
    print("=" * 70)

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


def solve_one_quiz(api: APIClient, quiz_entry: dict, team_id: int, dry_run: bool, force: bool, use_ai: bool) -> \
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

    # 4. 遍历题目，逐个提交答案
    for idx, q in enumerate(all_questions):
        r_id = q["r_id"]
        answer_val = q["answer"]
        already_answered = q["choose"]
        qtype = q["type"]

        if not answer_val:
            skip_count += 1
            print(f"  [跳过] 第 {idx + 1} 题: 尚未有答案")
            continue

        if already_answered and not force:
            skip_count += 1
            print(f"  [跳过] 第 {idx + 1} 题: 已作答过 (答案={answer_val})")
            continue

        answer = normalize_answer(answer_val, qtype)
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

        extra_tag = " [AI生成]" if q.get("ai_solved") else ""

        if dry_run:
            ok_count += 1
            print(f"  [DRY-RUN] 第 {idx + 1} 题 提交 → {answer_val}{extra_tag} | {q['desc'][:20]}...")
            time.sleep(0.05)
            continue

        # 实际提交单题答案
        endpoint = f"question/quizanswer/quiz/{quiz_id}/question/{r_id}/attempt/{attempt}/"
        result = api.post(endpoint, payload)

        if result.get("code") == 0:
            ok_count += 1
            print(f"  [成功] 第 {idx + 1} 题 提交 → {answer_val}{extra_tag}")
        else:
            fail_count += 1
            print(f"  [失败] 第 {idx + 1} 题: {result.get('message', '未知错误')}")
        time.sleep(0.6)  # 保护性延时

    # 5. 提交整份考卷 (交卷)
    submit_body = {"quiz": quiz_id, "team": team_id, "attempt": attempt, "platform": "网页端"}
    if dry_run:
        print(f"  [DRY-RUN] 模拟交卷")
    else:
        result = api.post("question/submit/", submit_body)
        status = "成功" if result.get("code") == 0 else f"失败: {result.get('message', '未知错误')}"
        print(f"  考卷提交状态: {status}")
        time.sleep(0.6)

    # 6. 将该测验标记为 100% 学习进度
    progress_body = {
        "id": quiz_entry["id"],
        "course_id": quiz_entry["course_id"],
        "dir_id": quiz_entry["dir_id"],
        "content_type_id": quiz_entry["content_type_id"],
        "object_id": quiz_entry["object_id"],
        "type_id": quiz_entry["type_id"],
        "rate": quiz_entry["rate"],
    }

    if dry_run:
        print(f"  [DRY-RUN] 模拟测验进度上报")
    else:
        result = api.put(f"course/course/progress/{quiz_entry['id']}/", progress_body)
        print(f"  测验进度上报: {'成功' if result.get('code') == 0 else '失败'}")
        time.sleep(0.6)

    print(f"  小计: 提交 {ok_count} 题 | 跳过 {skip_count} 题 | 失败 {fail_count} 题")
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
        result = solve_one_quiz(api, quiz_entry, team_id, dry_run, force, use_ai)
        if result:
            total_ok += result["ok"]
            total_skip += result["skip"]
            total_fail += result["fail"]
        time.sleep(1)  # 两份考卷之间额外休息一秒

    print(f"\n  答题阶段全部完成: 累计提交 {total_ok} 题, 跳过 {total_skip} 题, 失败 {total_fail} 题")


def main():
    args = parse_args()
    print("本项目由ecx开发 开源地址 https://github.com/ecxwxz/myccr")
    print("猫娘交流群 105859360\n")

    # 交互式处理 DeepSeek API 密钥
    if not args["no_ai"]:
        ds_key = input("请输入 DeepSeek API Key (留空回车则禁用 AI 答题): ").strip()
        if ds_key:
            DEEPSEEK_CONFIG["apiKey"] = ds_key
            print(f"  [√] AI 答题已启用 (模型: {DEEPSEEK_CONFIG['model']})\n")
        else:
            print("  [!] 未提供 API Key，AI 答题已自动禁用\n")
            args["no_ai"] = True

    # 交互式处理 Token
    token = args["token"]
    if not token:
        token = input("请输入平台认证 Token: ").strip()
        if not token:
            print("错误: 必须提供认证 Token")
            sys.exit(1)

    # 交互式处理课程 URL
    encrypted_id = args["encrypted_id"]
    catalog = args["catalog"]
    if not encrypted_id or not catalog:
        url = input("请输入课程 URL (用于提取参数): ").strip()
        if url:
            parsed = parse_course_url(url)
            encrypted_id = parsed["encrypted_id"]
            catalog = parsed["catalog"]
        else:
            print("错误: 必须提供课程的 URL")
            sys.exit(1)

    print("\n" + "=" * 70)
    print("myccr 刷课自动化任务启动")
    print("=" * 70)
    print(f"  Token: {token[:20]}...{token[-5:] if len(token) > 25 else ''}")
    print(f"  课程 ID: {encrypted_id}")
    print(f"  分类目录: {catalog}")
    print(f"  运行设置: 延迟 {args['delay']}ms | 试运行: {args['dry_run']} | 强制执行: {args['force']}")

    api = APIClient(token)

    # 阶段 1: 数据拉取与结构提取
    data = stage_extract(api, encrypted_id, catalog)

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

    print("\n" + "=" * 70)
    print("任务全部执行完毕！")
    print("=" * 70)


if __name__ == "__main__":
    main()