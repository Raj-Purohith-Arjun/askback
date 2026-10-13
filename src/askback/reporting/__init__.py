"""Report writers: JSON, HTML, Excel, and a Markdown summary.

Every writer redacts before it writes (see `redaction`), and defaults to `ci`.
The JSON is the canonical report; the HTML and the workbook are views of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..core.logging import get_logger
from ..evaluation.results import RunReport
from .html_report import render_html, write_html
from .json_report import write_json
from .markdown_report import render_markdown, write_markdown
from .redaction import DEFAULT_MODE, MODES, redact_report, redacted, resolve_mode

log = get_logger(__name__)

FORMATS = ("json", "html", "xlsx", "md")


def write_reports(
    report: RunReport,
    directory: str | Path,
    *,
    formats: Sequence[str] = ("json", "html"),
    stem: str | None = None,
    redaction: str = DEFAULT_MODE,
) -> dict[str, Path]:
    """Write the requested formats, redacted to `redaction`, and return {format: path}.

    A format that is unavailable (openpyxl missing, say) is logged and skipped
    rather than failing the run: the JSON report is the one that must survive.
    """
    base = Path(directory)
    name = stem or report.run_id
    written: dict[str, Path] = {}
    # Once, and outside the per-format guard below: a report that cannot be
    # redacted is not written at all, in any format.
    view = redacted(report, redaction)

    for fmt in formats:
        try:
            if fmt == "json":
                written["json"] = write_json(view, base / f"{name}.json", redaction=redaction)
            elif fmt == "html":
                written["html"] = write_html(view, base / f"{name}.html", redaction=redaction)
            elif fmt == "md":
                written["md"] = write_markdown(view, base / f"{name}.md", redaction=redaction)
            elif fmt == "xlsx":
                from .excel_report import write_excel

                written["xlsx"] = write_excel(view, base / f"{name}.xlsx", redaction=redaction)
            else:
                log.warning("Unknown report format %r; expected one of %s", fmt, FORMATS)
        except Exception as exc:  # noqa: BLE001 - one bad writer must not lose the run
            log.error("Could not write %s report: %s", fmt, exc)

    return written


__all__ = [
    "DEFAULT_MODE",
    "FORMATS",
    "MODES",
    "redact_report",
    "redacted",
    "render_html",
    "render_markdown",
    "resolve_mode",
    "write_html",
    "write_json",
    "write_markdown",
    "write_reports",
]
