import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
FUNCTION = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+([\w]+)\.([\w]+)\s*\(([^)]*)\)", re.I
)
MUTATING = re.compile(r"\bupdate\s+[\w.\"]+\s+set\b|\bdelete\s+from\b", re.I)
WHERE = re.compile(r"\bwhere\b", re.I)


def latest_definitions() -> dict[str, tuple[str, str]]:
    definitions: dict[str, tuple[str, str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text()
        for match in FUNCTION.finditer(sql):
            body_start = sql.find("$$", match.end())
            body_end = sql.find("$$;", body_start + 2)
            if body_start == -1 or body_end == -1:
                continue
            signature = f"{match.group(1)}.{match.group(2)}({match.group(3).strip()})"
            definitions[signature] = (path.name, sql[body_start + 2:body_end])
    return definitions


def offending_statements(body: str) -> list[str]:
    without_comments = re.sub(r"--[^\n]*", "", body)
    without_literals = re.sub(r"'(?:''|[^'])*'", "''", without_comments)
    return [
        part.strip()
        for part in without_literals.split(";")
        if part.strip() and MUTATING.search(part) and not WHERE.search(part)
    ]


def test_latest_function_definitions_avoid_pg_safeupdate_violations():
    offenders = []
    for signature, (path, body) in latest_definitions().items():
        for statement in offending_statements(body):
            offenders.append(f"{path}: {signature}: {' '.join(statement.split())[:120]}")
    assert not offenders, (
        "pg-safeupdate rejects UPDATE/DELETE without a WHERE clause on hosted "
        "Supabase, including inside SECURITY DEFINER functions:\n" + "\n".join(offenders)
    )
