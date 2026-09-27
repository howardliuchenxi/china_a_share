"""Build and execute the reproducible companion notebook for the study."""

from __future__ import annotations

import json
from pathlib import Path

import nbformat
import pandas as pd
from nbclient import NotebookClient


STUDY_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = STUDY_DIR / "outputs"
NOTEBOOK_PATH = STUDY_DIR / "v224_effect_validation.ipynb"


def _percentage(value: float) -> str:
    return f"{value:.2%}"


def build_notebook() -> None:
    summary = pd.read_csv(OUTPUT_DIR / "summary.csv")
    comparison = pd.read_csv(OUTPUT_DIR / "comparison.csv")
    qa = json.loads((OUTPUT_DIR / "qa.json").read_text(encoding="utf-8"))
    headline = summary[
        (summary["group"] == "v2.24")
        & (summary["sample_type"] == "keep_first_10d")
        & (summary["entry_basis"] == "next_open")
        & (summary["horizon"] == 10)
    ].iloc[0]
    effect = comparison[
        (comparison["comparison"] == "v2.24_minus_counterfactual")
        & (comparison["entry_basis"] == "next_open")
        & (comparison["horizon"] == 10)
    ].iloc[0]

    notebook = nbformat.v4.new_notebook()
    notebook["metadata"]["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook["cells"] = [
        nbformat.v4.new_markdown_cell(
            "## tl;dr\n\n"
            f"From 2025-01-01 through {qa['analysis_end_date']}, the v2.24 "
            f"state machine produced {qa['event_rows']:,} rows across both "
            "simulations. For deduplicated v2.24 events with a mature 10-session "
            f"outcome, next-open hit rate was {_percentage(headline['hit_rate'])}, "
            f"mean return was {_percentage(headline['mean_return'])}, and median "
            f"return was {_percentage(headline['median_return'])}. The v2.24 minus "
            f"counterfactual 10-session mean-return difference was "
            f"{_percentage(effect['mean_return_difference'])}; its bootstrap interval "
            "crossed zero. The rule increased signal volume but did not establish a "
            "statistically reliable quality improvement."
        ),
        nbformat.v4.new_markdown_cell(
            "## Context & Methods\n\n"
            "### Key Assumptions\n\n"
            "- The canonical simulation applies v2.24's non-retroactive L1/L2 rule.\n"
            "- The comparison changes only that rule and is a counterfactual, not a "
            "complete reconstruction of v2.23.\n"
            "- Tushare current THS industry membership is applied historically because "
            "the interface does not provide effective dates.\n"
            "- Signal-close returns are diagnostics; next-open returns are the more "
            "executable approximation.\n"
            "- L7 is evaluated because L10 sell and holding rules remain undefined."
        ),
        nbformat.v4.new_code_cell(
            "from pathlib import Path\n"
            "import json\n"
            "import pandas as pd\n\n"
            "output_dir = Path('studies/v224_condition_validation/outputs')\n"
            "summary = pd.read_csv(output_dir / 'summary.csv')\n"
            "comparison = pd.read_csv(output_dir / 'comparison.csv')\n"
            "yearly = pd.read_csv(output_dir / 'yearly.csv')\n"
            "events = pd.read_parquet(output_dir / 'events.parquet')\n"
            "qa = json.loads((output_dir / 'qa.json').read_text())"
        ),
        nbformat.v4.new_markdown_cell("## Data"),
        nbformat.v4.new_code_cell(
            "pd.Series({\n"
            "    'analysis_start_date': qa['analysis_start_date'],\n"
            "    'analysis_end_date': qa['analysis_end_date'],\n"
            "    'panel_rows': qa['panel_rows'],\n"
            "    'panel_security_count': qa['panel_security_count'],\n"
            "    'minimum_monthly_daily_basic_complete_rate': "
            "qa['minimum_monthly_daily_basic_complete_rate'],\n"
            "    'minimum_monthly_industry_mapping_rate': "
            "qa['minimum_monthly_industry_mapping_rate'],\n"
            "    'duplicate_panel_keys': qa['duplicate_panel_keys'],\n"
            "    'duplicate_event_keys': qa['event_duplicate_keys'],\n"
            "})"
        ),
        nbformat.v4.new_code_cell(
            "pd.DataFrame(qa['spot_checks']).T"
        ),
        nbformat.v4.new_markdown_cell("## Results"),
        nbformat.v4.new_code_cell(
            "summary[(summary['sample_type'] == 'keep_first_10d') & "
            "summary['horizon'].isin([1, 3, 5, 10])][[\n"
            "    'group', 'entry_basis', 'horizon', 'event_count', 'hit_rate',\n"
            "    'mean_return', 'median_return', 'trimmed_mean_5pct',\n"
            "    'mean_excess_return', 'mean_return_ci_low', 'mean_return_ci_high'\n"
            "]]"
        ),
        nbformat.v4.new_code_cell(
            "comparison[comparison['horizon'].isin([1, 3, 5, 10])][[\n"
            "    'comparison', 'entry_basis', 'horizon', 'hit_rate_difference',\n"
            "    'mean_return_difference', 'hit_rate_difference_ci_low',\n"
            "    'hit_rate_difference_ci_high', 'mean_return_difference_ci_low',\n"
            "    'mean_return_difference_ci_high'\n"
            "]]"
        ),
        nbformat.v4.new_code_cell("yearly"),
        nbformat.v4.new_markdown_cell(
            "## Takeaways\n\n"
            "1. V2.24 materially expands the number of L7 signals.\n"
            "2. Ten-session mean returns are positive, but hit rates and medians are "
            "below zero-centered thresholds, indicating a right-tail payoff profile.\n"
            "3. Mean excess returns are negative against the same-date equal-weight "
            "SH/SZ benchmark.\n"
            "4. The v2.24 versus counterfactual differences have confidence intervals "
            "that cross zero; predictive improvement is not established.\n"
            "5. The historical industry-map limitation prevents a fully point-in-time "
            "production acceptance."
        ),
    ]
    nbformat.write(notebook, NOTEBOOK_PATH)
    client = NotebookClient(
        notebook,
        timeout=600,
        kernel_name="python3",
        resources={"metadata": {"path": str(REPO_ROOT)}},
    )
    executed = client.execute()
    nbformat.write(executed, NOTEBOOK_PATH)


REPO_ROOT = STUDY_DIR.parents[1]


if __name__ == "__main__":
    build_notebook()
