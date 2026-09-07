#!/usr/bin/env python3
import base64
import json
import logging
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl, quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DOC_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё._/-]{1,50}$")
CACHE = {}
WAREHOUSE_CACHE = {"loaded_at": 0.0, "items": {}, "error": ""}
WAITING_FOR_DOC = set()

BUTTON_CHECK = "🔍 Проверить заявку"
BUTTON_HELP = "ℹ️ Инструкция"
BUTTON_HEALTH = "🩺 Состояние систем"
BUTTON_CANCEL = "❌ Отмена"


def main_keyboard():
    return {
        "keyboard": [
            [{"text": BUTTON_CHECK}],
            [{"text": BUTTON_HELP}, {"text": BUTTON_HEALTH}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Введите номер заявки",
    }


def help_text():
    return (
        "Как пользоваться ботом:\n\n"
        "1. Нажмите «🔍 Проверить заявку».\n"
        "2. Отправьте ID заявки из Smartup.\n"
        "3. Бот покажет текущую стадию, склад и найденные проблемы.\n\n"
        "Можно сразу отправить номер заявки без нажатия кнопки.\n\n"
        "Ответственные по стадиям:\n"
        "• Черновик — оператор завершает оформление.\n"
        "• Новый — оператор переводит заявку в обработку.\n"
        "• В обработке — проверка финансовым отделом.\n"
        "• В ожидании — склад должен принять заявку в WMS.\n"
        "• Отгружен или Доставлен — проверяется сборка в WMS.\n"
        "• Архивирован — проверяются бухгалтерия и отправка ЭСФ.\n\n"
        "Проверка TMS временно отключена."
    )


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError("Нет config.json. Скопируйте config.example.json в config.json и заполните его.")
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not str(data.get("telegram_bot_token", "")).strip():
        raise RuntimeError("В config.json не заполнен telegram_bot_token")
    return data


def add_doc_number(url, doc_number):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["doc_number"] = doc_number
    encoded_path = quote(parts.path, safe="/:@-._~!$&'()*+,;=%")
    return urlunsplit((parts.scheme, parts.netloc, encoded_path, urlencode(query), parts.fragment))


def request_json(service, doc_number, timeout):
    url = add_doc_number(str(service.get("url", "")).strip(), doc_number)
    if not url.startswith(("http://", "https://")):
        return {"_error": "неверный или пустой URL"}

    headers = {"Accept": "application/json", "User-Agent": "OrderControlBot/1.0"}
    username = str(service.get("username", ""))
    password = str(service.get("password", ""))
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
            if response.status != 200:
                return {"_error": f"HTTP {response.status}"}
    except HTTPError as error:
        try:
            error_body = error.read().decode("utf-8", errors="replace").strip()
        except Exception:
            error_body = ""
        logging.error("HTTP %s; URL=%s; body=%s", error.code, url, error_body[:3000])
        return {"_error": f"HTTP {error.code}", "_technical": error_body[:3000]}
    except URLError as error:
        return {"_error": f"нет соединения: {error.reason}"}
    except TimeoutError:
        return {"_error": "превышено время ожидания"}
    except Exception as error:
        return {"_error": f"ошибка запроса: {error}"}

    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else {"_error": "ответ JSON не является объектом"}
    except json.JSONDecodeError:
        logging.error("Получен не JSON; URL=%s; body=%s", url, body[:3000])
        return {"_error": "база вернула не JSON"}


def request_smartup(service, doc_number, timeout):
    base_url = str(service.get("url", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return {"_error": "неверный или пустой URL"}

    path = str(service.get("path", "b/trade/txs/tdeal/order$export")).strip().lstrip("/")
    url = f"{base_url}/{path}"
    payload = {
        "filial_codes": [{"filial_code": ""}], "filial_code": "", "external_id": "", "deal_id": str(doc_number),
        "begin_deal_date": "", "end_deal_date": "", "delivery_date": "", "begin_created_on": "",
        "end_created_on": "", "begin_modified_on": "", "end_modified_on": ""
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "OrderControlBot/1.0"}
    username = str(service.get("username", ""))
    password = str(service.get("password", ""))
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return {"_error": f"HTTP {error.code}: {body[:500]}"}
    except URLError as error:
        return {"_error": f"нет соединения: {error.reason}"}
    except TimeoutError:
        return {"_error": "превышено время ожидания"}
    except Exception as error:
        return {"_error": f"ошибка запроса: {error}"}

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"_error": "Smartup вернул не JSON"}

    if isinstance(data, dict) and data.get("error"):
        return {"_error": str(data.get("error"))}

    result = data.get("result", data) if isinstance(data, dict) else {}
    orders = result.get("order", []) if isinstance(result, dict) else []
    if not orders:
        return {"document_found": False, "status_code": "", "status": "Не найден"}

    order = orders[0]
    status_code = str(order.get("status") or "").strip().upper() if isinstance(order, dict) else ""
    return {
        "document_found": True,
        "status_code": status_code,
        "status": smartup_status_name(status_code),
        "order": order,
    }


def smartup_headers(service, with_json=False):
    headers = {
        "Accept": "application/json",
        "User-Agent": "OrderControlBot/1.0",
    }
    if with_json:
        headers["Content-Type"] = "application/json"

    username = str(service.get("username", ""))
    password = str(service.get("password", ""))
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    return headers


def request_smartup_warehouses(service, timeout):
    cache_seconds = int(service.get("warehouse_cache_seconds", 3600))
    if WAREHOUSE_CACHE["items"] and time.time() - WAREHOUSE_CACHE["loaded_at"] < cache_seconds:
        return WAREHOUSE_CACHE["items"], ""

    base_url = str(service.get("url", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return {}, "неверный или пустой URL Smartup"

    path = str(service.get("warehouse_path", "b/anor/mkw/warehouse_list:table")).strip().lstrip("/")
    url = f"{base_url}/{path}"
    columns = [
        "warehouse_id",
        "code",
        "name",
        "warehouse_type_name",
        "responsible_person_name",
        "order_no",
        "state",
        "state_name",
    ]
    limit = 50
    offset = 0
    rows = []

    while True:
        payload = {
            "p": {
                "column": columns,
                "filter": ["state", "=", "A"],
                "limit": limit,
                "offset": offset,
                "sort": [],
            }
        }
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=smartup_headers(service, with_json=True),
            method="POST",
        )

        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
        except HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            message = f"HTTP {error.code}: {error_body[:500]}"
            WAREHOUSE_CACHE["error"] = message
            return {}, message
        except URLError as error:
            message = f"нет соединения: {error.reason}"
            WAREHOUSE_CACHE["error"] = message
            return {}, message
        except TimeoutError:
            WAREHOUSE_CACHE["error"] = "превышено время ожидания"
            return {}, WAREHOUSE_CACHE["error"]
        except Exception as error:
            message = f"ошибка запроса: {error}"
            WAREHOUSE_CACHE["error"] = message
            return {}, message

        try:
            response_data = json.loads(body)
        except json.JSONDecodeError:
            WAREHOUSE_CACHE["error"] = "Smartup вернул не JSON"
            return {}, WAREHOUSE_CACHE["error"]

        page_rows = response_data.get("data", []) if isinstance(response_data, dict) else []
        total_count = int(response_data.get("count", 0)) if isinstance(response_data, dict) else 0
        rows.extend(page_rows)

        if not page_rows or len(rows) >= total_count or len(page_rows) < limit:
            break

        offset += limit

    warehouses = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 8:
            continue

        code = str(row[1] or "").strip()
        if not code:
            continue

        warehouses[code] = {
            "id": str(row[0] or "").strip(),
            "code": code,
            "name": str(row[2] or "").strip(),
            "region": str(row[3] or "").strip(),
            "responsible": str(row[4] or "").strip(),
            "active": str(row[6] or "").strip().upper() == "A",
        }

    if not warehouses:
        WAREHOUSE_CACHE["error"] = "Smartup вернул пустой справочник складов"
        return {}, WAREHOUSE_CACHE["error"]

    WAREHOUSE_CACHE["loaded_at"] = time.time()
    WAREHOUSE_CACHE["items"] = warehouses
    WAREHOUSE_CACHE["error"] = ""
    return warehouses, ""


def smartup_status_name(status_code):
    statuses = {
        "D": "Черновик",
        "C": "Отменён",
        "A": "Архивирован",
        "B#N": "Новый",
        "B#E": "В обработке",
        "B#W": "В ожидании",
        "B#S": "Отгружен",
        "B#V": "Доставлен",
    }
    return statuses.get(status_code, f"Неизвестный статус ({status_code})" if status_code else "Статус не указан")


def smartup_warehouse_codes(smartup):
    order = smartup.get("order", {})
    products = order.get("order_products", []) if isinstance(order, dict) else []
    codes = []
    for product in products:
        if not isinstance(product, dict):
            continue
        code = str(product.get("warehouse_code") or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def configured_warehouse_codes(region):
    values = region.get("warehouse_codes", [])
    if isinstance(values, str):
        values = [values]
    return {str(value).strip() for value in values if str(value).strip()}


def telegram_call(token, method, params, timeout=40):
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = urlencode(params).encode("utf-8")
    request = Request(url, data=body, method="POST")
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Ошибка Telegram API"))
    return data.get("result")


def bool_value(data, *names):
    for name in names:
        if name in data:
            value = data[name]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "да")
            return bool(value)
    return False


def describe_error(error_text):
    text = str(error_text or "неизвестная ошибка")
    descriptions = {
        "HTTP 401": "неверный логин или пароль",
        "HTTP 403": "доступ запрещён",
        "HTTP 404": "неправильный URL или сервис не опубликован",
        "HTTP 500": "внутренняя ошибка HTTP-сервиса",
        "превышено время ожидания": "сервер не ответил вовремя",
        "база вернула не JSON": "сервер вернул страницу вместо JSON",
    }
    for prefix, description in descriptions.items():
        if prefix in text:
            return f"{prefix} — {description}"
    if text.startswith("нет соединения:"):
        return "нет соединения с сервером"
    return text[:180]


def responsible_line(name):
    name = str(name or "").strip()
    return f"   Ответственный: {name}" if name else ""


def format_region(region_name, wms, tms, wms_responsible="", tms_responsible=""):
    lines = [f"📍 Регион: {region_name}"]
    problems = 0
    unavailable = 0

    if wms.get("_error"):
        lines.append(f"❌ WMS недоступна: {describe_error(wms['_error'])}")
        unavailable += 1
    else:
        found = bool_value(wms, "order_found", "document_found")
        assembled = bool_value(wms, "assembly_completed", "assembled")
        lines.append("✅ WMS: ордер найден" if found else "❌ WMS: ордер не найден")
        if found:
            lines.append("✅ WMS: сборка завершена" if assembled else "⚠️ WMS: сборка не завершена")
        if not found or not assembled:
            problems += 1
            owner = responsible_line(wms_responsible)
            if owner:
                lines.append(owner)

    if tms.get("_error"):
        lines.append(f"❌ TMS недоступна: {describe_error(tms['_error'])}")
        unavailable += 1
    else:
        found = bool_value(tms, "order_found", "document_found")
        driver = str(tms.get("driver") or "").strip()
        ttn = bool_value(tms, "ttn_sent")
        lines.append("✅ TMS: документ найден" if found else "❌ TMS: документ не найден")
        if found:
            lines.append(f"✅ Водитель: {driver}" if driver else "⚠️ Водитель не назначен")
            lines.append("✅ ТТН отправлена" if ttn else "⚠️ ТТН не отправлена")
        if not found or not driver or not ttn:
            problems += 1
            owner = responsible_line(tms_responsible)
            if owner:
                lines.append(owner)

    return lines, problems, unavailable


def region_has_order(region_result):
    wms = region_result.get("wms", {})
    tms = region_result.get("tms", {})
    return (
        bool_value(wms, "order_found", "document_found")
        or bool_value(tms, "order_found", "document_found")
    )


def format_result(doc_number, results):
    lines = [f"Заявка {doc_number}", ""]
    problems = 0
    unavailable = 0
    cancelled = False

    smartup = results.get("smartup", {})
    if smartup.get("_error"):
        lines.append(f"❌ Smartup недоступен: {describe_error(smartup['_error'])}")
        unavailable = 1
        lines.append("➡️ Невозможно определить, какую систему нужно проверять")
    elif not bool_value(smartup, "document_found"):
        lines.append("❌ Smartup: заявка не найдена")
        problems = 1
        lines.append("➡️ Проверьте правильность номера заявки")
    else:
        status_code = str(smartup.get("status_code") or "").strip().upper()
        status_name = str(smartup.get("status") or "Статус не указан")
        lines.append("✅ Smartup: заявка найдена")
        lines.append(f"📌 Стадия заявки: {status_name}")

        warehouse_codes = results.get("warehouse_codes", [])
        warehouse_names = results.get("warehouse_names", [])
        if warehouse_names:
            lines.append(f"🏬 Склад: {', '.join(warehouse_names)}")
        elif warehouse_codes:
            lines.append(f"🏬 Код склада Smartup: {', '.join(warehouse_codes)}")

        if status_code == "C":
            lines.append("🚫 Заявка отменена")
            lines.append("➡️ Проверки WMS и бухгалтерии не требуются")
            cancelled = True

        elif status_code == "D":
            lines.append("⚠️ Заявка сохранена как черновик и ещё не запущена в работу")
            lines.append("➡️ Оператор должен завершить оформление заявки и перевести её в стадию «Новый»")
            problems += 1

        elif status_code == "B#N":
            lines.append("⚠️ Заявка ещё не передана на финансовую проверку")
            lines.append("➡️ Оператор должен перевести заявку в стадию «В обработке»")
            lines.append("➡️ После этого заявка будет передана в финансовый отдел")
            problems += 1

        elif status_code == "B#E":
            lines.append("⏳ Заявка передана в финансовый отдел")
            lines.append("➡️ Выполняется проверка в системе «Плюсовой баланс»")

        elif status_code in ("B#W", "B#S", "B#V"):
            need_assembly = status_code in ("B#S", "B#V")
            region_results = [item for item in results.get("regions", []) if item.get("selected", True)]
            matched_regions = [
                item for item in region_results
                if bool_value(item.get("wms", {}), "order_found", "document_found")
            ]

            if not matched_regions:
                if any(not item.get("wms", {}).get("_error") for item in region_results):
                    lines.append("❌ Расходный ордер не найден ни в одном WMS")
                    lines.append("➡️ Обратитесь на склад")
                    problems += 1
                else:
                    lines.append("❌ WMS проверить не удалось")
                    unavailable += 1
            else:
                for item in matched_regions:
                    lines.append("")
                    lines.append(f"📍 Регион: {item['name']}")
                    wms = item.get("wms", {})
                    if wms.get("_error"):
                        lines.append(f"❌ WMS недоступна: {describe_error(wms['_error'])}")
                        unavailable += 1
                    else:
                        order_found = bool_value(wms, "order_found", "document_found")
                        assembled = bool_value(wms, "assembly_completed", "assembled")
                        lines.append("✅ WMS: расходный ордер найден" if order_found else "❌ WMS: расходный ордер не найден")
                        if need_assembly and order_found:
                            lines.append("✅ WMS: сборка завершена" if assembled else "⚠️ WMS: сборка не завершена")
                        if not order_found or (need_assembly and not assembled):
                            lines.append("➡️ Обратитесь на склад")
                            problems += 1

        elif status_code == "A":
            accounting = results.get("accounting", {})
            if accounting.get("_error"):
                lines.append(f"❌ Бухгалтерия недоступна: {describe_error(accounting['_error'])}")
                lines.append("➡️ Не удалось проверить отправку ЭСФ")
                unavailable += 1
            else:
                document_found = bool_value(accounting, "document_found", "order_found")
                document_posted = bool_value(accounting, "document_posted")
                invoice_sent = bool_value(accounting, "invoice_sent")
                lines.append("✅ Бухгалтерия: документ найден" if document_found else "❌ Бухгалтерия: документ не найден")
                if document_found:
                    lines.append("✅ Документ проведён" if document_posted else "⚠️ Документ не проведён")
                lines.append("✅ ЭСФ отправлена" if invoice_sent else "❌ ЭСФ не отправлена")
                if invoice_sent:
                    lines.append("✅ Заявка готова: ЭСФ отправлена")
                else:
                    lines.append("➡️ Обратитесь в бухгалтерию")
                    problems += 1

        else:
            lines.append("⚠️ Для этой стадии правило проверки ещё не настроено")
            problems += 1

    elapsed = float(results.get("_elapsed", 0))
    lines.append("")
    if cancelled and unavailable == 0:
        lines.append("ℹ️ Проверка завершена: заявка отменена")
    elif problems == 0 and unavailable == 0:
        lines.append("✅ Всё в порядке")
    else:
        lines.append(f"Проблемы заявки: {problems}")
        lines.append(f"Недоступные системы: {unavailable}")
    lines.append(f"Проверено: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    lines.append(f"Время проверки: {elapsed:.1f} сек.")
    return "\n".join(lines)


def check_order(config, doc_number):
    cache_seconds = int(config.get("cache_seconds", 30))
    cached = CACHE.get(doc_number)
    if cached and time.time() - cached[0] < cache_seconds:
        return cached[1] + "\n♻️ Результат взят из кеша"

    started = time.perf_counter()
    timeout = int(config.get("request_timeout_seconds", 20))
    regions = config.get("regions", [])
    if not regions:
        raise RuntimeError("В config.json не заполнен список regions")

    smartup = request_smartup(config.get("smartup", {}), doc_number, timeout)
    results = {
        "smartup": smartup,
        "regions": [
            {
                "name": str(region.get("name", "Без названия")),
                "wms_responsible": region.get("wms_responsible", ""),
                "tms_responsible": region.get("tms_responsible", ""),
            }
            for region in regions
        ]
    }

    status_code = ""
    if not smartup.get("_error") and bool_value(smartup, "document_found"):
        status_code = str(smartup.get("status_code") or "").strip().upper()

    warehouse_codes = smartup_warehouse_codes(smartup)
    results["warehouse_codes"] = warehouse_codes

    warehouses = {}
    warehouse_catalog_error = ""
    if warehouse_codes:
        warehouses, warehouse_catalog_error = request_smartup_warehouses(
            config.get("smartup", {}), timeout
        )
    results["warehouse_catalog_error"] = warehouse_catalog_error

    matched_region_indexes = []
    warehouse_names = []
    warehouse_details = []

    for code in warehouse_codes:
        warehouse = warehouses.get(code)
        if warehouse is None:
            continue

        name = warehouse.get("name", "")
        if name and name not in warehouse_names:
            warehouse_names.append(name)
        warehouse_details.append(warehouse)

    dynamic_names_found = bool(warehouse_names)
    for index, region in enumerate(regions):
        if set(warehouse_codes) & configured_warehouse_codes(region):
            matched_region_indexes.append(index)
            if not dynamic_names_found:
                warehouse_name = str(region.get("warehouse_name") or region.get("name") or "").strip()
                if warehouse_name and warehouse_name not in warehouse_names:
                    warehouse_names.append(warehouse_name)

    results["warehouse_names"] = warehouse_names
    results["warehouse_details"] = warehouse_details

    indexes_to_check = list(matched_region_indexes) if matched_region_indexes else list(range(len(regions)))
    for index, item in enumerate(results["regions"]):
        item["selected"] = index in indexes_to_check

    need_accounting = status_code == "A"
    need_wms = status_code in ("B#W", "B#S", "B#V")

    max_workers = min(20, max(1, int(need_accounting) + len(regions) * int(need_wms)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        jobs = {}

        if need_accounting:
            jobs[pool.submit(request_json, config.get("accounting", {}), doc_number, timeout)] = ("common", "accounting", None)

        for index in indexes_to_check:
            region = regions[index]
            if need_wms:
                jobs[pool.submit(request_json, region.get("wms", {}), doc_number, timeout)] = ("region", "wms", index)

        for job in as_completed(jobs):
            scope, service_name, index = jobs[job]
            try:
                value = job.result()
            except Exception as error:
                value = {"_error": f"внутренняя ошибка: {error}"}

            if scope == "common":
                results[service_name] = value
            else:
                results["regions"][index][service_name] = value

    results["_elapsed"] = time.perf_counter() - started
    answer = format_result(doc_number, results)
    CACHE[doc_number] = (time.time(), answer)
    return answer


def health_status(name, result):
    if result.get("_error"):
        return f"❌ {name}: {describe_error(result['_error'])}", 1
    return f"✅ {name}", 0


def check_health(config):
    started = time.perf_counter()
    timeout = int(config.get("request_timeout_seconds", 20))
    health_doc = str(config.get("health_doc_number", "0"))
    regions = config.get("regions", [])
    tasks = []

    with ThreadPoolExecutor(max_workers=min(20, 2 + len(regions))) as pool:
        tasks.append(("Smartup", pool.submit(request_smartup, config.get("smartup", {}), health_doc, timeout)))
        tasks.append(("Бухгалтерия", pool.submit(request_json, config.get("accounting", {}), health_doc, timeout)))
        for region in regions:
            name = str(region.get("name", "Без названия"))
            tasks.append((f"WMS {name}", pool.submit(request_json, region.get("wms", {}), health_doc, timeout)))

        lines = ["Состояние интеграций", ""]
        failed = 0
        for name, job in tasks:
            try:
                result = job.result()
            except Exception as error:
                result = {"_error": f"внутренняя ошибка: {error}"}
            line, error_count = health_status(name, result)
            lines.append(line)
            failed += error_count

    lines.extend(["", f"Недоступно систем: {failed}", f"Время проверки: {time.perf_counter() - started:.1f} сек."])
    return "\n".join(lines)


def is_allowed(config, chat_id):
    allowed = {str(item) for item in config.get("allowed_chat_ids", [])}
    return str(chat_id) in allowed


def handle_message(config, message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user_id = message.get("from", {}).get("id", chat_id)
    state_key = (str(chat_id), str(user_id))
    text = str(message.get("text", "")).strip()
    if chat_id is None:
        return

    if text == "/chatid":
        answer = f"ID этого чата: {chat_id}\nДобавьте его в allowed_chat_ids в config.json и перезапустите бота."
    elif text == "/start":
        WAITING_FOR_DOC.discard(state_key)
        answer = "Бот контроля заявок запущен.\n\nНажмите «🔍 Проверить заявку» или просто отправьте её номер."
    elif text in ("/help", BUTTON_HELP):
        WAITING_FOR_DOC.discard(state_key)
        answer = help_text()
    elif not is_allowed(config, chat_id):
        answer = "Этот чат не имеет доступа. Выполните /chatid и добавьте ID в config.json."
    elif text in ("/health", BUTTON_HEALTH):
        WAITING_FOR_DOC.discard(state_key)
        answer = check_health(config)
    elif text == BUTTON_CHECK:
        WAITING_FOR_DOC.add(state_key)
        answer = "Введите ID заявки из Smartup.\nНапример: 4411939"
    elif text == BUTTON_CANCEL:
        WAITING_FOR_DOC.discard(state_key)
        answer = "Проверка отменена."
    else:
        doc_number = text[7:].strip() if text.startswith("/check ") else text
        if not DOC_RE.fullmatch(doc_number):
            if state_key in WAITING_FOR_DOC:
                answer = "Неверный номер. Отправьте только ID заявки.\nНапример: 4411939"
            else:
                answer = "Команда не распознана. Нажмите «ℹ️ Инструкция»."
        else:
            WAITING_FOR_DOC.discard(state_key)
            logging.info("Проверка заявки=%s chat_id=%s", doc_number, chat_id)
            answer = check_order(config, doc_number)

    telegram_call(
        config["telegram_bot_token"],
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": answer,
            "reply_markup": json.dumps(main_keyboard(), ensure_ascii=False),
        },
    )


def setup_logging():
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)


def main():
    setup_logging()
    try:
        config = load_config()
    except Exception as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 1

    token = config["telegram_bot_token"]
    offset = 0
    logging.info("Бот запущен")
    while True:
        try:
            updates = telegram_call(token, "getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                if "message" in update:
                    handle_message(config, update["message"])
        except KeyboardInterrupt:
            logging.info("Бот остановлен")
            return 0
        except Exception as error:
            logging.error("Ошибка цикла: %s", error)
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
