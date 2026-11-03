from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


COMPANY_CODE_PATTERN = re.compile(r"\b(?:[0345689]\d{5})(?:\.(?:SH|SZ|BJ))?\b", re.IGNORECASE)
DATE_PATTERN = re.compile(r"(?:截止|截至|as-of)\s*[:：]?\s*(20\d{2}-\d{2}-\d{2})", re.IGNORECASE)


@dataclass(frozen=True)
class UpdateCommand:
    scope: str
    company_ids: list[str]
    as_of_date: str


def parse_update_command(command: str, default_as_of: str | date | None = None) -> UpdateCommand:
    text = command.strip()
    if not text.startswith("更新"):
        raise ValueError("命令须以“更新”开头")
    if "景气" in text:
        return UpdateCommand(scope="prosperity", company_ids=[], as_of_date=str(default_as_of or date.today().isoformat()))
    date_match = DATE_PATTERN.search(text)
    as_of = date_match.group(1) if date_match else str(default_as_of or date.today().isoformat())
    codes = [code.upper() for code in COMPANY_CODE_PATTERN.findall(text)]
    if codes:
        return UpdateCommand(scope="all", company_ids=codes, as_of_date=as_of)
    if "全部" in text or "所有" in text:
        return UpdateCommand(scope="all", company_ids=[], as_of_date=as_of)
    if "知名" in text or "种子" in text or "默认" in text:
        return UpdateCommand(scope="seed", company_ids=[], as_of_date=as_of)
    raise ValueError("支持：更新知名公司、更新全部公司、更新公司 600000.SH、更新行业景气度，可追加“截止 YYYY-MM-DD”")
