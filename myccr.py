import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "progress_payloads.json")

BASE_URL = "https://myccr.net:13710/api/v1"

DEEPSEEK_CONFIG = {
    "apiKey": "",
    "baseUrl": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
}

QTYPE_SINGLE_CHOICE = 200
QTYPE_MULTI_CHOICE = 210
QTYPE_TRUE_FALSE = 310
QTYPE_FILL_BLANK = 320

def parse_args():
    args = {
        "token": None,
        "encrypted_id": None,
        "catalog": None,
        "dry_run": False,
        "skip_report": False,
        "skip_quiz": False,
        "force": False,
        "no_ai": False,
        "delay": 600,
        "only_id": None,
    }

    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--token" and i + 1 < len(argv):
            args["token"] = argv[i + 1]
            i += 2
        elif argv[i] == "--url" and i + 1 < len(argv):
            parsed = parse_course_url(argv[i + 1])
            args["encrypted_id"] = parsed["encrypted_id"]
            args["catalog"] = parsed["catalog"]
            i += 2
        elif argv[i] == "--dry-run":
            args["dry_run"] = True
            i += 1
        elif argv[i] == "--skip-report":
            args["skip_report"] = True
            i += 1
        elif argv[i] == "--skip-quiz":
            args["skip_quiz"] = True
            i += 1
        elif argv[i] == "--force":
            args["force"] = True
            i += 1
        elif argv[i] == "--no-ai":
            args["no_ai"] = True
            i += 1
        elif argv[i] == "--delay" and i + 1 < len(argv):
            args["delay"] = int(argv[i + 1])
            i += 2
        elif argv[i] == "--only-id" and i + 1 < len(argv):
            args["only_id"] = int(argv[i + 1])
            i += 2
        else:
            i += 1

    return args


def parse_course_url(url):
    fragment = url
    if "#" in url:
        fragment = url.split("#", 1)[1]
    if "?" in fragment:
        query = fragment.split("?", 1)[1]
    else:
        query = fragment

    params = urllib.parse.parse_qs(query)
    encrypted_id = params.get("id", [None])[0]
    catalog = params.get("catalog", [None])[0]

    if not encrypted_id or not catalog:
        print(f"错误: 无法从 URL 解析 id 和 catalog")
        print(f"  URL: {url}")
        print(f"  id={encrypted_id}, catalog={catalog}")
        sys.exit(1)

    return {"encrypted_id": encrypted_id, "catalog": catalog}

class APIClient:
    def __init__(self, token):
        self.token = token

    def get(self, endpoint, params=None):
        url = f"{BASE_URL}/{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": self.token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def put(self, endpoint, body):
        url = f"{BASE_URL}/{endpoint}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "Origin": "https://myccr.net",
                "Referer": "https://myccr.net/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, endpoint, body):
        url = f"{BASE_URL}/{endpoint}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "Origin": "https://myccr.net",
                "Referer": "https://myccr.net/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))


def flatten_dirs(dirs, course_id, base_dir_id=None, path=""):
    results = []
    for d in dirs:
        dir_id = d["id"]
        title = d.get("title", "")
        cur_path = f"{path}/{title}" if path else title

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
                "rate": 100,
                "dir_path": cur_path,
            })

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

        if d.get("children"):
            results.extend(flatten_dirs(d["children"], course_id, dir_id, cur_path))

    return results


def stage_extract(api, encrypted_id, catalog):
    print("=" * 70)
    print("阶段 1/3: 提取课程资源")
    print("=" * 70)

    course_data = api.get("course/course/as/student/", {
        "id": encrypted_id,
        "catalog": catalog,
    })
    course = course_data["data"]["rows"][0]
    course_id = str(course["id"])
    team_id = course["team_id"]
    catalog_name = course.get("catalog_name", "")
    course_title = course["title"]

    print(f"  课程: {course_title}")
    print(f"  分类: {catalog_name}")
    print(f"  course_id: {course_id}")
    print(f"  team_id: {team_id}")

    dir_data = api.get("team/TeamDir/dir/", {"id": course_id})
    dirs = dir_data["data"]["dirs"]

    payloads = flatten_dirs(dirs, course_id)

    type_counts = {}
    for p in payloads:
        t = p.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"  总计: {len(payloads)} 个资源")
    for t, c in sorted(type_counts.items()):
        print(f"    {t}: {c}")

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

    print(f"  已保存至: {OUTPUT_FILE}")
    return output


def stage_report(api, data, dry_run, delay_ms):
    print("\n" + "=" * 70)
    print("阶段 2/3: 上报学习进度")
    print("=" * 70)

    payloads = data["payloads"]
    non_quiz = [p for p in payloads if p.get("type") != "quiz"]

    print(f"  资源总数: {len(payloads)} (quiz: {len(payloads) - len(non_quiz)}, 其他: {len(non_quiz)})")
    print(f"  请求间隔: {delay_ms}ms")
    print(f"  模式: {'DRY RUN（仅打印）' if dry_run else '实际发包'}")

    success = 0
    fail = 0

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
            print(f"  [{i+1}/{len(non_quiz)}] DRY-RUN  {ptype:12s}  id={pid:6d}  {title}")
        else:
            try:
                result = api.put(f"course/course/progress/{pid}/", body)
                if result.get("code") == 0:
                    success += 1
                    status = "OK"
                else:
                    fail += 1
                    status = f"FAIL: {result.get('message', '')}"
            except Exception as e:
                fail += 1
                status = f"FAIL: {e}"
            print(f"  [{i+1}/{len(non_quiz)}] {status:40s}  {ptype:12s}  id={pid:6d}  {title}")
            time.sleep(delay_ms / 1000)

    print(f"  结果: 成功 {success}, 失败 {fail}")
    return {"success": success, "fail": fail}

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").strip()


def normalize_answer(raw, qtype):
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
            return json.loads(raw)
        return raw
    return str(raw).upper()


def call_deepseek(questions):
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

    prompt = "\n".join(prompt_lines)
    payload = {
        "model": DEEPSEEK_CONFIG["model"],
        "messages": [
            {"role": "system", "content": "你是一个选择题答题助手。你必须严格按照用户要求输出每题标准答案行。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }

    endpoint = DEEPSEEK_CONFIG["baseUrl"].rstrip("/") + "/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_CONFIG['apiKey']}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    return parse_ai_response(content, len(questions))


def parse_ai_response(text, question_count):
    answer_map = {}
    for line in text.split("\n"):
        line = line.strip()
        m = re.match(r"(?:第\s*(\d+)\s*题[：:]\s*|(\d+)\s*[\.\、:：]\s*)(?:答案[：:]\s*)?([A-Z]+)", line, re.I)
        if m:
            qnum = int(m.group(1) or m.group(2))
            ans = m.group(3).upper()
            answer_map[qnum] = sorted(list(ans))
    return answer_map


def solve_one_quiz(api, quiz_entry, team_id, dry_run, force, use_ai):
    quiz_chapter_id = quiz_entry["id"]
    title = quiz_entry["title"]
    quiz_id = quiz_entry["object_id"]

    print(f"\n  {'─'*60}")
    print(f"  Quiz: {title}")
    print(f"  chapter_id={quiz_chapter_id}, quiz_id={quiz_id}")

    start_data = api.get("question/teamquiz/start/", {"id": quiz_chapter_id})
    if start_data["code"] != 0:
        print(f"  失败: {start_data['message']}")
        return None
    result_id = start_data["data"]["result"]
    attempt = start_data["data"]["attempt"]
    if not start_data["data"].get("rqmode", True):
        print("  已完成答题，不允许重做")
        return None
    print(f"  result_id={result_id}, attempt={attempt}")

    quiz_data = api.get("question/quizanswer/cache/", {"id": result_id})
    if "data" not in quiz_data:
        print("  获取题目失败")
        return None
    d = quiz_data["data"]
    groups = d.get("questiongroups", [])

    all_questions = []
    unanswered_questions = []

    for g in groups:
        for q in g["question"]:
            qobj = {
                "r_id": q["r_id"],
                "answer_id": q["answer_id"],
                "group": g["group_id"],
                "type": g["type_id"],
                "desc": strip_html(q.get("discription", "")),
                "options": q.get("option", []),
                "answer": q.get("answer") or "",
                "choose": q.get("choose", False),
            }
            all_questions.append(qobj)
            if not qobj["choose"] and qobj["answer"] == "":
                unanswered_questions.append(qobj)

    total_q = len(all_questions)
    print(f"  共 {total_q} 题, 已作答 {total_q - len(unanswered_questions)}, 未作答 {len(unanswered_questions)}")

    if unanswered_questions and use_ai:
        print(f"  AI 解题中 ({len(unanswered_questions)} 题)...")
        ai_answers = call_deepseek(unanswered_questions)
        print(f"  AI 返回答案: {len(ai_answers)} 题")
        for qnum, ans in sorted(ai_answers.items()):
            idx = qnum - 1
            if idx < len(unanswered_questions):
                unanswered_questions[idx]["answer"] = "".join(ans)
                unanswered_questions[idx]["ai_solved"] = True
                print(f"    Q{qnum} → {''.join(ans)}")

    ok_count = 0
    skip_count = 0
    fail_count = 0

    for idx, q in enumerate(all_questions):
        r_id = q["r_id"]
        answer_val = q["answer"]
        already_answered = q["choose"]
        qtype = q["type"]

        if not answer_val:
            skip_count += 1
            print(f"  跳过 Q{idx+1} ({'已作答' if already_answered else '无答案'})")
            continue

        if already_answered and not force:
            skip_count += 1
            print(f"  跳过 Q{idx+1} (已作答, answer={answer_val})")
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

        if dry_run:
            ok_count += 1
            extra = " [AI]" if q.get("ai_solved") else ""
            print(f"  DRY-RUN  Q{idx+1}: → {answer_val}{extra}  {q['desc'][:40]}...")
            time.sleep(0.05)
        else:
            try:
                endpoint = f"question/quizanswer/quiz/{quiz_id}/question/{r_id}/attempt/{attempt}/"
                result = api.post(endpoint, payload)
                extra = " [AI]" if q.get("ai_solved") else ""
                if result.get("code") == 0:
                    ok_count += 1
                    print(f"  OK  Q{idx+1}: → {answer_val}{extra}  {q['desc'][:30]}...")
                else:
                    fail_count += 1
                    print(f"  FAIL  Q{idx+1}: {result.get('message', '')}")
            except Exception as e:
                fail_count += 1
                print(f"  FAIL  Q{idx+1}: {e}")
            time.sleep(0.6)

    quiz_submitted = False
    submit_body = {
        "quiz": quiz_id,
        "team": team_id,
        "attempt": attempt,
        "platform": "网页端",
    }
    if dry_run:
        print(f"  DRY-RUN  提交quiz  quiz={quiz_id} team={team_id} attempt={attempt}")
        quiz_submitted = True
    else:
        try:
            result = api.post("question/submit/", submit_body)
            if result.get("code") == 0:
                print(f"  提交quiz OK  quiz={quiz_id} attempt={attempt}")
                quiz_submitted = True
            else:
                print(f"  提交quiz FAIL  quiz={quiz_id}: {result.get('message', '')}")
        except Exception as e:
            print(f"  提交quiz FAIL  quiz={quiz_id}: {e}")
        time.sleep(0.6)

    progress_reported = False
    pid = quiz_entry["id"]
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
        print(f"  DRY-RUN  上报quiz进度  id={pid}")
        progress_reported = True
    else:
        try:
            result = api.put(f"course/course/progress/{pid}/", progress_body)
            if result.get("code") == 0:
                print(f"  上报quiz进度 OK  id={pid}")
                progress_reported = True
            else:
                print(f"  上报quiz进度 FAIL  id={pid}: {result.get('message', '')}")
        except Exception as e:
            print(f"  上报quiz进度 FAIL  id={pid}: {e}")
        time.sleep(0.6)

    print(f"  结果: {ok_count} 提交, {skip_count} 跳过, {fail_count} 失败, 进度上报: {'OK' if progress_reported else '未上报'}")
    return {"ok": ok_count, "skip": skip_count, "fail": fail_count}


def stage_quiz(api, data, dry_run, force, use_ai, delay_ms, only_id):
    print("\n" + "=" * 70)
    print("阶段 3/3: 自动答题")
    print("=" * 70)

    quizzes = [p for p in data["payloads"] if p.get("type") == "quiz"]
    if only_id:
        quizzes = [q for q in quizzes if q["id"] == only_id]
        if not quizzes:
            print(f"  未找到 quiz chapter id={only_id}")
            return

    team_id = data.get("team_id", 9558)

    labels = []
    if dry_run:
        labels.append("DRY RUN")
    if force:
        labels.append("强制重答")
    if not use_ai:
        labels.append("禁用AI")
    print(f"  Quiz 总数: {len(quizzes)}")
    print(f"  模式: {' | '.join(labels) if labels else '实际提交'}")

    total_ok = 0
    total_skip = 0
    total_fail = 0

    for quiz_entry in quizzes:
        result = solve_one_quiz(api, quiz_entry, team_id, dry_run, force, use_ai)
        if result:
            total_ok += result["ok"]
            total_skip += result["skip"]
            total_fail += result["fail"]
        time.sleep(1)

    print(f"\n  全部完成: {total_ok} 提交, {total_skip} 跳过, {total_fail} 失败")
    return {"ok": total_ok, "skip": total_skip, "fail": total_fail}

def main():
    args = parse_args()
    print("本项目由ecx开发 开源地址https://github.com/ecxwxz/myccr")
    print("猫娘交流群 105859360")
    # DeepSeek API Key
    if not args["no_ai"]:
        ds_key = input("请输入 DeepSeek API Key（留空则禁用 AI 答题）: ").strip()
        if ds_key:
            DEEPSEEK_CONFIG["apiKey"] = ds_key
            print(f"  AI 答题已启用 (model: {DEEPSEEK_CONFIG['model']})")
        else:
            print("  未提供 API Key，AI 答题已禁用")
            args["no_ai"] = True

    # 交互式输入 token
    token = args["token"]
    if not token:
        token = input("请输入 Token: ").strip()
        if not token:
            print("错误: 必须提供 --token 或输入 Token")
            sys.exit(1)

    encrypted_id = args["encrypted_id"]
    catalog = args["catalog"]
    if not encrypted_id or not catalog:
        url = input("请输入课程 URL: ").strip()
        if url:
            parsed = parse_course_url(url)
            encrypted_id = parsed["encrypted_id"]
            catalog = parsed["catalog"]
        else:
            print("错误: 必须提供 --url 或输入课程 URL")
            sys.exit(1)

    print("=" * 70)
    print("myccr 一键任务脚本")
    print("=" * 70)
    print(f"  Token: {token[:30]}...")
    print(f"  encrypted_id: {encrypted_id}")
    print(f"  catalog: {catalog}")
    print(f"  dry_run: {args['dry_run']}")
    print(f"  skip_report: {args['skip_report']}")
    print(f"  skip_quiz: {args['skip_quiz']}")
    print(f"  delay: {args['delay']}ms")

    api = APIClient(token)

    # 阶段 1: 提取
    data = stage_extract(api, encrypted_id, catalog)

    # 阶段 2: 上报进度
    if not args["skip_report"]:
        stage_report(api, data, args["dry_run"], args["delay"])
    else:
        print("\n[跳过] 阶段 2/3: 上报学习进度")

    # 阶段 3: 答题
    if not args["skip_quiz"]:
        stage_quiz(api, data, args["dry_run"], args["force"], not args["no_ai"], args["delay"], args["only_id"])
    else:
        print("\n[跳过] 阶段 3/3: 自动答题")

    print("\n" + "=" * 70)
    print("全部完成!")
    print("=" * 70)


if __name__ == "__main__":
    main()
