from china_a_share.core.contracts import UiFeedbackChatRequest
from china_a_share.repository_context import RepositorySourceSearch


def chat_request(selected_text):
    return UiFeedbackChatRequest(
        page_path="/analysis",
        feedback_id="results-panel",
        selected_text=selected_text,
        conversation=[
            {
                "role": "user",
                "content": "Which validation path produced this result?",
            }
        ],
    )


def test_repository_source_search_returns_line_numbered_matching_evidence(tmp_path):
    source_root = tmp_path / "src" / "china_a_share"
    source_root.mkdir(parents=True)
    source_file = source_root / "workflow.py"
    source_file.write_text(
        "\n".join(
            (
                "def validate_plan():",
                '    message = "no deterministic local transform or aggregation"',
                "    return message",
            )
        ),
        encoding="utf-8",
    )
    frontend_root = tmp_path / "frontend" / "src"
    frontend_root.mkdir(parents=True)
    (frontend_root / "App.tsx").write_text(
        '<section data-feedback-id="results-panel">Result</section>',
        encoding="utf-8",
    )

    evidence = RepositorySourceSearch(tmp_path).search(
        chat_request("no deterministic local transform or aggregation")
    )

    assert "SOURCE src/china_a_share/workflow.py" in evidence
    assert "2:     message =" in evidence
    assert "SOURCE frontend/src/App.tsx" in evidence


def test_repository_source_search_caches_only_allowlisted_files(tmp_path):
    source_root = tmp_path / "src"
    source_root.mkdir()
    source_file = source_root / "service.py"
    source_file.write_text("def cached_result():\n    return 'first'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=must-not-be-read\n", encoding="utf-8")
    search = RepositorySourceSearch(tmp_path)
    request = chat_request("cached_result")

    first_evidence = search.search(request)
    source_file.write_text("def cached_result():\n    return 'second'\n", encoding="utf-8")
    second_evidence = search.search(request)

    assert "return 'first'" in first_evidence
    assert second_evidence == first_evidence
    assert "must-not-be-read" not in second_evidence
