"""Build privacy-conscious summaries for ReHome sessions."""

from contextlib import contextmanager
from html import escape
from math import isfinite
from time import perf_counter
from urllib.parse import quote


CATEGORIES = ("projects", "chats", "results", "skills", "agents", "plugins", "settings")
STATUSES = ("included", "skipped", "error", "pending")
_ITEM_FIELDS = {"category", "id", "name", "project_id", "chat_id"}


def _string(value, label, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string%s" % (label, " or None" if nullable else ""))
    return value


def make_report(inventory, outcomes, timings):
    """Validate a complete inventory/outcome mapping and calculate totals."""
    if not isinstance(inventory, list) or not isinstance(outcomes, list) or not isinstance(timings, list):
        raise ValueError("inventory, outcomes and timings must be lists")
    items_by_key = {}
    for row in inventory:
        if not isinstance(row, dict) or set(row) != _ITEM_FIELDS:
            raise ValueError("inventory rows must contain exactly the required fields")
        category = _string(row["category"], "category")
        if category not in CATEGORIES:
            raise ValueError("unknown inventory category")
        ident = _string(row["id"], "id")
        _string(row["name"], "name")
        _string(row["project_id"], "project_id", nullable=True)
        _string(row["chat_id"], "chat_id", nullable=True)
        key = (category, ident)
        if key in items_by_key:
            raise ValueError("duplicate inventory item")
        items_by_key[key] = dict(row)

    outcome_map = {}
    for outcome in outcomes:
        if not isinstance(outcome, dict) or set(outcome) != {"category", "id", "status", "reason"}:
            raise ValueError("outcomes must contain exactly category, id, status and reason")
        key = (_string(outcome["category"], "category"), _string(outcome["id"], "id"))
        if key not in items_by_key:
            raise ValueError("outcome references unknown inventory item")
        if key in outcome_map:
            raise ValueError("duplicate outcome")
        status = _string(outcome["status"], "status")
        if status not in STATUSES:
            raise ValueError("unknown outcome status")
        reason = outcome["reason"]
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or None")
        if status in ("skipped", "error", "pending") and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("non-included outcomes require a non-empty reason")
        outcome_map[key] = (status, reason)
    if set(outcome_map) != set(items_by_key):
        raise ValueError("every inventory item must have exactly one outcome")

    items = []
    totals = {category: {"discovered": 0, "included": 0, "skipped": 0, "error": 0, "pending": 0}
              for category in CATEGORIES}
    for key, row in items_by_key.items():
        status, reason = outcome_map[key]
        item = dict(row)
        item.update(status=status, reason=reason)
        items.append(item)
        totals[row["category"]]["discovered"] += 1
        totals[row["category"]][status] += 1

    clean_timings = []
    for timing in timings:
        if not isinstance(timing, dict) or set(timing) != {"name", "elapsed_seconds", "status"}:
            raise ValueError("timing entries must contain exactly name, elapsed_seconds and status")
        name = _string(timing["name"], "timing name")
        elapsed = timing["elapsed_seconds"]
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not isfinite(elapsed) or elapsed < 0:
            raise ValueError("elapsed_seconds must be a non-negative number")
        status = _string(timing["status"], "timing status")
        if status not in ("passed", "failed"):
            raise ValueError("timing status must be passed or failed")
        clean_timings.append({"name": name, "elapsed_seconds": float(elapsed), "status": status})

    has_pending = any(t["pending"] for t in totals.values())
    has_omissions = any(t["skipped"] or t["error"] for t in totals.values())
    state = "incomplete" if has_pending else ("complete_with_omissions" if has_omissions else "complete")
    return {"status": state, "totals": totals, "items": items, "timings": clean_timings}


class Timings:
    """Collect elapsed durations while preserving exceptions from measured work."""
    def __init__(self):
        self.entries = []

    @contextmanager
    def measure(self, name):
        name = _string(name, "timing name")
        started = perf_counter()
        status = "passed"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.entries.append({"name": name, "elapsed_seconds": max(0.0, perf_counter() - started), "status": status})


def render_html(report):
    """Render a static, escaped Russian report without scripts or remote assets."""
    if not isinstance(report, dict) or not isinstance(report.get("totals"), dict) or not isinstance(report.get("items"), list):
        raise ValueError("invalid report")
    labels = (("discovered", "Обнаружено"), ("included", "Включено"), ("skipped", "Пропущено"),
              ("error", "Ошибки"), ("pending", "Ожидают"))
    category_labels = {"projects": "Проекты", "chats": "Чаты", "results": "Результаты",
                       "skills": "Навыки", "agents": "Агенты", "plugins": "Плагины", "settings": "Настройки"}
    status_labels = {"complete": "Завершено", "complete_with_omissions": "Завершено с пропусками",
                     "incomplete": "Не завершено", "included": "Включено", "skipped": "Пропущено",
                     "error": "Ошибка", "pending": "Ожидает", "passed": "Успешно", "failed": "Ошибка"}
    rows = []
    for category in CATEGORIES:
        counts = report["totals"].get(category, {})
        rows.append("<tr><th scope=\"row\">%s</th>%s</tr>" % (
            escape(category_labels[category]), "".join("<td>%s</td>" % int(counts.get(key, 0)) for key, _ in labels)))
    item_rows = []
    chat_targets = {}
    for candidate in report["items"]:
        chat_id = candidate.get("id") if isinstance(candidate, dict) and candidate.get("category") == "chats" else None
        if isinstance(chat_id, str) and chat_id:
            chat_targets.setdefault(chat_id, "chat-" + quote(chat_id, safe=""))
    for item in report["items"]:
        if not isinstance(item, dict):
            raise ValueError("invalid report item")
        cells = [escape(str(item.get(k) if item.get(k) is not None else ""))
                 for k in ("category", "id", "name", "project_id", "chat_id", "status", "reason")]
        cells[0] = escape(category_labels.get(item.get("category"), str(item.get("category", ""))))
        cells[5] = escape(status_labels.get(item.get("status"), str(item.get("status", ""))))
        chat_id = item.get("chat_id")
        anchor = ""
        if isinstance(chat_id, str) and chat_id in chat_targets:
            anchor = '<a href="#%s">Открыть чат</a>' % escape(chat_targets[chat_id], quote=True)
        if item.get("category") == "chats" and item.get("id") in chat_targets:
            cells[1] = '<span id="%s">%s</span>' % (escape(chat_targets[item["id"]], quote=True), cells[1])
        item_rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" %
                         (*cells, anchor))
    timing_rows = []
    for timing in report.get("timings", []):
        timing_rows.append("<tr><td>%s</td><td>%.6f</td><td>%s</td></tr>" %
                           (escape(str(timing.get("name", ""))), float(timing.get("elapsed_seconds", 0)),
                            escape(status_labels.get(timing.get("status"), str(timing.get("status", ""))))))
    headers = "".join("<th scope=\"col\">%s</th>" % label for _, label in labels)
    return ("<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Отчёт ReHome</title><main><h1>Отчёт ReHome</h1><p>Статус: %s</p>"
            "<h2>Итоги</h2><table><caption>Результаты по категориям</caption><thead><tr><th scope=\"col\">Категория</th>%s</tr></thead><tbody>%s</tbody></table>"
            "<h2>Элементы</h2><table><caption>Состав отчёта</caption><thead><tr><th>Категория</th><th>ID</th><th>Название</th><th>Проект</th><th>Чат</th><th>Результат</th><th>Причина</th><th>Ссылка</th></tr></thead><tbody>%s</tbody></table>"
            "<h2>Замеры</h2><table><caption>Длительность операций</caption><thead><tr><th>Операция</th><th>Секунды</th><th>Статус</th></tr></thead><tbody>%s</tbody></table></main></html>" %
            (escape(status_labels.get(report.get("status"), str(report.get("status", "")))), headers, "".join(rows), "".join(item_rows), "".join(timing_rows)))
