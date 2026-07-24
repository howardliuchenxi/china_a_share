"""Test script for limit-up/limit-down queries against running backend."""
import json
import time
import urllib.request
import urllib.error
import sys

BASE_URL = "http://127.0.0.1:8000"

QUERIES = [
    {
        "id": "Q1",
        "prompt": "昨天涨停多少只股票，分别是哪些？",
        "expected": "应返回昨天的涨停股票列表及数量，使用 limit_list_d 接口，limit_type=U"
    },
    {
        "id": "Q2",
        "prompt": "昨天跌停多少只股票，列出代码和名称。",
        "expected": "应返回昨天的跌停股票列表及数量，使用 limit_list_d 接口，limit_type=D"
    },
    {
        "id": "Q3",
        "prompt": "昨天有哪些股票炸板了？",
        "expected": "应返回昨天的炸板（炸板/开板）股票列表，使用 limit_list_d 接口，limit_type=Z"
    },
    {
        "id": "Q4",
        "prompt": "昨天涨停、跌停和炸板的数量分别是多少？",
        "expected": "应返回三个统计数字：涨停数量、跌停数量、炸板数量"
    },
    {
        "id": "Q5",
        "prompt": "最近一个交易日涨停的股票有哪些？",
        "expected": "应返回最近交易日的涨停股票列表，使用 limit_list_d，limit_type=U"
    },
    {
        "id": "Q6",
        "prompt": "2026年7月20日有哪些股票涨停？",
        "expected": "应返回2026-07-20的涨停股票列表，使用 limit_list_d，limit_type=U，trade_date=20260720"
    },
    {
        "id": "Q7",
        "prompt": "上周一到上周五每天分别有多少只股票涨停？",
        "expected": "应返回上周5个交易日每天的涨停数量"
    },
    {
        "id": "Q8",
        "prompt": "过去一个月出现过涨停的股票有哪些？",
        "expected": "应返回过去一个月内至少涨停过一次的股票列表（去重）"
    },
    {
        "id": "Q9",
        "prompt": "最近20个交易日涨停次数最多的十只股票是谁？",
        "expected": "应返回按涨停次数排序的前10只股票及次数"
    },
    {
        "id": "Q10",
        "prompt": "最近10个交易日涨停超过两次的股票有哪些？",
        "expected": "应返回涨停次数>2的股票列表"
    },
    {
        "id": "Q11",
        "prompt": "过去一个月从未涨停的股票有多少只？",
        "expected": "应计算总股票数 - 涨停过的股票数"
    },
    {
        "id": "Q12",
        "prompt": "最近一个月炸板次数最多的股票有哪些？",
        "expected": "应返回按炸板次数排序的股票列表，使用 limit_type=Z"
    },
]


def call_analysis(prompt: str) -> dict:
    """Call the analysis API and return the response."""
    data = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/analysis",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8")}
    except Exception as e:
        return {"error": str(e)}


def summarize_result(result: dict) -> str:
    """Summarize the result for reporting."""
    if result.get("error"):
        return f"ERROR: {result['error']}"

    status = result.get("status", "unknown")
    plan = result.get("plan", {})
    feasibility = plan.get("feasibility", "unknown")
    queries = plan.get("queries", [])
    results = result.get("results", [])

    lines = [f"  Status: {status}"]
    lines.append(f"  Planner: {result.get('planner', '?')}")
    lines.append(f"  Feasibility: {feasibility}")

    if plan.get("limitations"):
        lines.append(f"  Limitations: {plan['limitations']}")

    # Plan queries
    lines.append(f"  Plan queries ({len(queries)}):")
    for q in queries:
        op = q.get("operation", "?")
        params = q.get("params", {})
        fields = q.get("fields", [])
        purpose = q.get("purpose", "")
        lines.append(f"    - {q.get('query_id', '?')}: {op} params={params} fields={fields}")
        if purpose:
            lines.append(f"      purpose: {purpose}")

    transform = plan.get("result_transform")
    if transform:
        lines.append(f"  Result transform: {transform}")

    # Results summary
    lines.append(f"  Result datasets ({len(results)}):")
    for r in results:
        r_status = r.get("status", "?")
        r_op = r.get("operation", "?")
        row_count = r.get("row_count", 0)
        columns = r.get("columns", [])
        err = r.get("error")
        summary = r.get("summary", {})

        if err:
            lines.append(f"    - {r.get('query_id', '?')}: {r_op} [{r_status}] ERROR: {err}")
        else:
            lines.append(f"    - {r.get('query_id', '?')}: {r_op} [{r_status}] rows={row_count} cols={columns[:10]}")
            if summary:
                lines.append(f"      summary: {summary}")

    # Check for actual rows
    if results:
        for r in results:
            rows = r.get("rows", [])
            if rows:
                lines.append(f"  Sample rows ({r.get('query_id')}):")
                for row in rows[:5]:
                    lines.append(f"    {row}")

    # Decision trace
    trace = result.get("decision_trace", {})
    if trace:
        lines.append(f"  Decision trace: {trace}")

    return "\n".join(lines)


def run_all():
    """Run all queries and print results."""
    print("=" * 70)
    print("LIMIT-UP/LIMIT-DOWN QUERY TESTING")
    print("=" * 70)
    print(f"Backend: {BASE_URL}")
    print(f"Total queries: {len(QUERIES)}")
    print()

    passed = 0
    failed = 0
    errors = []

    for i, q in enumerate(QUERIES):
        print(f"[{q['id']}] {q['prompt']}")
        print(f"  Expected: {q['expected']}")
        print()

        start = time.time()
        result = call_analysis(q["prompt"])
        elapsed = time.time() - start

        print(f"  Elapsed: {elapsed:.1f}s")
        print(summarize_result(result))
        print()

        # Determine pass/fail
        if result.get("error"):
            failed += 1
            errors.append(f"{q['id']}: API error - {result.get('error')}")
        elif result.get("status") == "success":
            # Check if we have actual results with rows
            results = result.get("results", [])
            has_data = any(r.get("row_count", 0) > 0 for r in results)
            if has_data:
                passed += 1
            else:
                failed += 1
                errors.append(f"{q['id']}: No data rows returned")
        else:
            failed += 1
            plan = result.get("plan", {})
            errors.append(f"{q['id']}: status={result.get('status')}, feasibility={plan.get('feasibility')}, limitations={plan.get('limitations')}")

        print("-" * 70)
        print()

    # Summary
    print("=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  - {e}")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
