#!/usr/bin/env python3
import base64
import html
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl, quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
# В Docker каталог состояния монтируется с хоста (ACCESS_PATH=/app/data/access.json).
ACCESS_PATH = Path(os.environ.get("ACCESS_PATH") or ROOT / "access.json")
DOC_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё._/-]{1,50}$")
CACHE = {}
WAREHOUSE_CACHE = {"loaded_at": 0.0, "items": {}, "error": ""}
WAITING_FOR_DOC = set()

BUTTON_CHECK = "🔍 Проверить заявку"
BUTTON_HELP = "ℹ️ Инструкция"
BUTTON_HEALTH = "🩺 Состояние систем"
BUTTON_CANCEL = "❌ Отмена"
BUTTON_REGISTER = "📝 Запросить доступ"
BUTTON_CHAT_ID = "🆔 ID чата"


def main_keyboard(allowed=False):
    if not allowed:
        return {
            "keyboard": [
                [{"text": BUTTON_REGISTER}],
                [{"text": BUTTON_HELP}, {"text": BUTTON_CHAT_ID}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Запросите доступ к проверке заявок",
        }
    return {
        "keyboard": [
            [{"text": BUTTON_CHECK}],
            [{"text": BUTTON_HELP}, {"text": BUTTON_HEALTH}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Введите номер заявки",
    }


def force_reply_keyboard():
    return {
        "force_reply": True,
        "selective": True,
        "input_field_placeholder": "Один или несколько ID заявок",
    }


def help_text():
    return (
        "Как пользоваться ботом:\n\n"
        "1. Нажмите «🔍 Проверить заявку».\n"
        "2. Отправьте один или несколько ID заявок из Smartup.\n"
        "3. Бот покажет текущую стадию, склад и найденные проблемы.\n\n"
        "Несколько номеров можно отправить через пробел, запятую или каждый с новой строки.\n"
        "Можно сразу использовать команду /check 4411939 4411940.\n\n"
        "Ответственные по стадиям:\n"
        "• Черновик — оператор завершает оформление.\n"
        "• Новый — оператор переводит заявку в обработку.\n"
        "• В обработке — проверка финансовым отделом.\n"
        "• В ожидании — проверяются ордер, БЯС, сборка, корзина и серии.\n"
        "• Отгружен или Доставлен — проверяются сборка, корзина и серии.\n"
        "• Архивирован — проверяются бухгалтерия и отправка ЭСФ.\n\n"
        "Если ордер или БЯС не найдены, сборка не завершена, в корзине остался товар "
        "или серии не совпали — обратитесь на склад.\n\n"
        "Проверка TMS временно отключена."
    )


def load_access_state():
    empty = {"approved_chat_ids": [], "pending": {}}
    if not ACCESS_PATH.exists():
        return empty
    try:
        with ACCESS_PATH.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        approved = data.get("approved_chat_ids", [])
        pending = data.get("pending", {})
        if not isinstance(approved, list) or not isinstance(pending, dict):
            raise ValueError("неверная структура")
        return {
            "approved_chat_ids": [str(item) for item in approved],
            "pending": pending,
        }
    except Exception:
        return empty


def save_access_state(state):
    ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ACCESS_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp_path.replace(ACCESS_PATH)


def admin_ids(config):
    configured = config.get("admin_chat_ids")
    if configured is None:
        configured = config.get("allowed_chat_ids", [])
    if isinstance(configured, (str, int)):
        configured = [configured]
    return {str(item) for item in configured}


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
        return {"_error": f"HTTP {error.code}", "_technical": error_body[:3000]}
    except URLError as error:
        return {"_error": f"нет соединения: {error.reason}"}
    except TimeoutError:
        return {"_error": "превышено время ожидания"}
    except Exception as error:
        return {"_error": f"ошибка запроса: {error}"}

    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            return {"_error": "ответ JSON не является объектом"}
        if data.get("error"):
            return {"_error": str(data["error"])}
        return data
    except json.JSONDecodeError:
        return {"_error": "база вернула не JSON"}



def normalize_wms(data):
    """Validate the service contract; never interpret missing fields as False."""
    if not isinstance(data, dict):
        return {"_error": "ответ WMS не является объектом"}
    if data.get("_error"):
        return data
    if data.get("error"):
        return {"_error": str(data["error"])}
    result = dict(data)
    fields = (
        ("document_found", ("order_found", "document_found")),
        ("assembly_found", ("assembly_found",)),
        ("assembled", ("assembly_completed", "assembled")),
    )
    for target, aliases in fields:
        value = next((data[key] for key in aliases if key in data), None)
        if not isinstance(value, bool):
            return {"_error": f"WMS не вернула логическое поле {target}"}
        result[target] = value
    quantity = data.get("basket_quantity")
    if (type(quantity) not in (int, float)
            or (isinstance(quantity, float) and not math.isfinite(quantity))
            or quantity < 0):
        return {"_error": "WMS не вернула корректное числовое поле basket_quantity"}
    flag = data.get("basket_has_items", False)
    if not isinstance(flag, bool):
        return {"_error": "WMS вернула неверный тип basket_has_items"}
    result["basket_has_items"] = flag or quantity > 0
    raw_finished = data.get("assembly_finished", result["assembled"])
    result["assembly_finished"] = raw_finished if isinstance(raw_finished, bool) else result["assembled"]
    result["assembled"] = (
        result["document_found"] and result["assembly_found"]
        and result["assembly_finished"] and not result["basket_has_items"]
    )

    result["assembly_items_available"] = "assembly_items" in data
    result["series_data_error"] = ""
    assembly_items = data.get("assembly_items", [])
    if not isinstance(assembly_items, list):
        result["assembly_items_available"] = False
        result["series_data_error"] = "WMS вернула неверный формат assembly_items"
        assembly_items = []
    elif any(not isinstance(item, dict) for item in assembly_items):
        result["assembly_items_available"] = False
        result["series_data_error"] = "WMS вернула некорректные строки assembly_items"
        assembly_items = []
    result["assembly_items"] = assembly_items
    # Keep supported aliases consistent with the calculated result.
    result["order_found"] = result["document_found"]
    result["assembly_completed"] = result["assembled"]
    return result


def request_wms(service, doc_number, timeout):
    return normalize_wms(request_json(service, doc_number, timeout))


def decimal_value(value):
    if isinstance(value, bool) or value is None:
        raise InvalidOperation
    text = str(value).strip().replace(" ", "").replace(",", ".")
    return Decimal(text)


def series_key(product_code, series):
    return str(product_code or "").strip().casefold(), str(series or "").strip().casefold()


def display_quantity(value):
    number = Decimal(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def compare_series(smartup, wms):
    if not wms.get("assembly_items_available"):
        return {
            "available": False,
            "matches": False,
            "issues": [wms.get("series_data_error") or "WMS не вернула состав сборки"],
        }

    order = smartup.get("order", {})
    products = order.get("order_products", []) if isinstance(order, dict) else []
    if not isinstance(products, list):
        return {"available": False, "matches": False, "issues": ["Smartup не вернул товары заявки"]}
    if not products:
        return {"available": False, "matches": False, "issues": ["Smartup вернул пустой список товаров"]}

    expected = {}
    actual = {}
    labels = {}
    data_issues = []

    for product in products:
        if not isinstance(product, dict):
            continue
        code = str(product.get("product_code") or "").strip()
        series = str(product.get("card_code") or "").strip()
        name = str(product.get("product_name") or product.get("name") or "").strip()
        quantity = next(
            (product.get(field) for field in ("sold_quant", "quantity", "order_quant") if product.get(field) is not None),
            None,
        )
        if not code:
            data_issues.append("В Smartup есть товар без product_code")
            continue
        if not series:
            data_issues.append(f"В Smartup не указана серия: {name or code}")
            continue
        try:
            number = decimal_value(quantity)
        except (InvalidOperation, ValueError):
            data_issues.append(f"В Smartup не указано количество: {name or code}, серия {series}")
            continue
        key = series_key(code, series)
        expected[key] = expected.get(key, Decimal(0)) + number
        labels[key] = (code, series, name)

    for item in wms.get("assembly_items", []):
        code = str(item.get("product_code") or "").strip()
        series = str(item.get("series") or "").strip()
        name = str(item.get("product_name") or "").strip()
        if not code:
            data_issues.append("В WMS есть строка сборки без product_code")
            continue
        if not series:
            data_issues.append(f"В WMS не указана серия: {name or code}")
            continue
        try:
            number = decimal_value(item.get("quantity"))
        except (InvalidOperation, ValueError):
            data_issues.append(f"В WMS не указано количество: {name or code}, серия {series}")
            continue
        key = series_key(code, series)
        actual[key] = actual.get(key, Decimal(0)) + number
        labels.setdefault(key, (code, series, name))

    issues = list(dict.fromkeys(data_issues))
    for key in sorted(set(expected) | set(actual)):
        code, series, name = labels[key]
        product = f"{name} ({code})" if name else code
        expected_quantity = expected.get(key, Decimal(0))
        actual_quantity = actual.get(key, Decimal(0))
        if key not in actual:
            issues.append(
                f"Нет в сборке: {product}, серия {series}, ожидалось {display_quantity(expected_quantity)}"
            )
        elif key not in expected:
            issues.append(
                f"Лишняя серия в сборке: {product}, серия {series}, количество {display_quantity(actual_quantity)}"
            )
        elif expected_quantity != actual_quantity:
            issues.append(
                f"Количество не совпало: {product}, серия {series}; "
                f"Smartup {display_quantity(expected_quantity)}, WMS {display_quantity(actual_quantity)}"
            )

    return {"available": True, "matches": not issues, "issues": issues}


def format_wms(wms, status_code, responsible="", series_check=None):
    wms = normalize_wms(wms)
    if wms.get("_error"):
        return [f"❌ Не удалось проверить WMS: {describe_error(wms['_error'])}"], 0, 1
    lines = []
    action = ""
    problems = 0
    warehouse_contact = False
    if not wms["document_found"]:
        lines.append("❌ WMS: расходный ордер не найден")
        action = "Обратитесь на склад"
        problems = 1
        warehouse_contact = True
    else:
        lines.append("✅ WMS: расходный ордер найден")
        if not wms["assembly_found"]:
            lines.append("⚠️ БЯС не найдена")
            action = "Обратитесь на склад"
            problems = 1
            warehouse_contact = True
        elif not wms["assembly_finished"]:
            lines.extend(["✅ БЯС найдена", "⚠️ Сборка товара не завершена"])
            action = "Обратитесь на склад"
            problems = 1
            warehouse_contact = True
        else:
            lines.extend(["✅ БЯС найдена", "✅ Сборка завершена"])
            if wms["basket_has_items"]:
                action = "Обратитесь на склад: товар необходимо убрать из корзины"
                problems = 1
                warehouse_contact = True
            elif series_check and not series_check.get("available"):
                lines.append("⚠️ Сверка серий не выполнена")
                for issue in series_check.get("issues", [])[:3]:
                    lines.append(f"   • {issue}")
                action = "Обратитесь на склад"
                problems = 1
                warehouse_contact = True
            elif series_check and not series_check.get("matches"):
                lines.append("❌ Серии Smartup и WMS не совпадают")
                for issue in series_check.get("issues", [])[:8]:
                    lines.append(f"   • {issue}")
                extra = len(series_check.get("issues", [])) - 8
                if extra > 0:
                    lines.append(f"   • Ещё расхождений: {extra}")
                action = "Обратитесь на склад: проверьте серии товара внутри сборки"
                problems = 1
                warehouse_contact = True
            elif series_check and series_check.get("matches"):
                lines.append("✅ Серии Smartup и WMS совпадают")
            else:
                lines.append("⚠️ Сверка серий не выполнена")
                action = "Обратитесь на склад"
                problems = 1
                warehouse_contact = True

            if not action and status_code == "B#W":
                action = "Заявка всё ещё находится в ожидании. Обратитесь на склад"
                problems = 1
                warehouse_contact = True
            elif not action and status_code == "B#S":
                action = "Обратитесь на склад для перевода заявки в статус «Доставлен»"
                problems = 1
                warehouse_contact = True
            elif not action and status_code == "B#V":
                action = "Передайте заявку в бухгалтерию для архивирования"
                problems = 1
    quantity = wms["basket_quantity"]
    if quantity > 0:
        lines.append(f"❌ В корзине осталось товара: {quantity:,}".replace(",", " ").removesuffix(".0"))
    return lines, problems, 0


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
        if not isinstance(response_data, dict) or response_data.get("error") or not isinstance(response_data.get("data"), list):
            return {}, "Smartup вернул некорректный справочник складов"
        raw_count = response_data.get("count")
        try:
            total_count = int(raw_count) if raw_count is not None else None
        except (TypeError, ValueError):
            total_count = None
        rows.extend(page_rows)

        if not page_rows or (total_count is not None and total_count > 0 and len(rows) >= total_count) or len(page_rows) < limit:
            break

        offset += limit

    warehouses = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue

        code = str(row[1] or "").strip()
        if not code:
            continue

        warehouses[code] = {
            "id": str(row[0] or "").strip(),
            "code": code,
            "name": str(row[2] or "").strip(),
            "region": str(row[3] or "").strip() if len(row) > 3 else "",
            "responsible": str(row[4] or "").strip() if len(row) > 4 else "",
            "active": str(row[6] or "").strip().upper() == "A" if len(row) > 6 else True,
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
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        # Telegram возвращает причину в теле ответа; без этого остаётся
        # бесполезное "HTTP Error 400: Bad Request".
        try:
            payload = json.loads(error.read().decode("utf-8"))
            description = payload.get("description") or str(error)
        except Exception:
            description = str(error)
        raise RuntimeError(f"{method}: {description}") from None
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', 'Ошибка Telegram API')}")
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


def format_region(region_name, wms, tms, wms_responsible="", tms_responsible="", series_check=None):
    lines = [f"📍 Регион: {region_name}"]
    problems = 0
    unavailable = 0

    wms_lines, wms_problems, wms_unavailable = format_wms(wms, "", wms_responsible, series_check)
    lines.extend(wms_lines)
    problems += wms_problems
    unavailable += wms_unavailable

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


def add_unique(items, value):
    value = str(value or "").strip()
    if value and value not in items:
        items.append(value)


def wms_summary(wms, status_code, series_check=None, responsible=""):
    normalized = normalize_wms(wms)
    owner = str(responsible or "склад").strip()
    if normalized.get("_error"):
        return "Не удалось получить данные из WMS", "Повторить проверку или обратиться на склад", owner
    if not normalized["document_found"]:
        return "Расходный ордер не найден в WMS", "Обратиться на склад", owner
    if not normalized["assembly_found"]:
        return "БЯС по заявке не найдена", "Обратиться на склад", owner
    if not normalized["assembly_finished"]:
        return "Сборка товара не завершена", "Склад должен завершить сборку", owner
    if normalized["basket_has_items"]:
        quantity = normalized.get("basket_quantity", 0)
        return f"В корзине осталось товара: {quantity:g}", "Склад должен убрать товар из корзины", owner
    if series_check and not series_check.get("available"):
        return "Не удалось проверить серии товара", "Обратиться на склад", owner
    if series_check and not series_check.get("matches"):
        return "Серии или количество товара в Smartup и WMS не совпадают", "Склад должен проверить товар внутри сборки", owner
    if series_check is None:
        return "Сверка серий не выполнена", "Обратиться на склад", owner
    if status_code == "B#W":
        return "Сборка завершена, но заявка всё ещё находится в ожидании", "Склад должен перевести заявку дальше", owner
    if status_code == "B#S":
        return "Заявка ещё не переведена в статус «Доставлен»", "Обратиться на склад", owner
    if status_code == "B#V":
        return "Доставленная заявка ещё не архивирована", "Передать заявку в бухгалтерию", "бухгалтерия"
    return "", "", ""


def format_result(doc_number, results):
    lines = [f"Заявка {doc_number}", ""]
    problems = 0
    unavailable = 0
    cancelled = False
    problem_reasons = []
    required_actions = []
    responsible_people = []
    unavailable_reasons = []

    smartup = results.get("smartup", {})
    if smartup.get("_error"):
        lines.append(f"❌ Smartup недоступен: {describe_error(smartup['_error'])}")
        unavailable = 1
        lines.append("➡️ Невозможно определить, какую систему нужно проверять")
        lines.append("👤 Ответственный: администратор интеграции")
        add_unique(unavailable_reasons, "Smartup недоступен")
        add_unique(required_actions, "Обратиться к администратору интеграции")
        add_unique(responsible_people, "администратор интеграции")
    elif not bool_value(smartup, "document_found"):
        lines.append("❌ Smartup: заявка не найдена")
        problems = 1
        lines.append("➡️ Проверьте правильность номера заявки")
        lines.append("👤 Если номер верный — обратитесь к оператору")
        add_unique(problem_reasons, "Заявка не найдена в Smartup")
        add_unique(required_actions, "Проверить номер заявки; если он верный — обратиться к оператору")
        add_unique(responsible_people, "оператор")
    else:
        status_code = str(smartup.get("status_code") or "").strip().upper()
        status_name = str(smartup.get("status") or "Статус не указан")
        lines.append("✅ Smartup: заявка найдена")
        lines.append(f"📌 Стадия заявки: {status_name}")
        order_date = str(smartup.get("order", {}).get("deal_time") or "").strip()
        if order_date:
            lines.append(f"📅 Дата заявки: {order_date}")

        warehouse_codes = results.get("warehouse_codes", [])
        warehouse_names = results.get("warehouse_names", [])
        if warehouse_names:
            lines.append(f"🏬 Склад: {', '.join(warehouse_names)}")
        elif warehouse_codes:
            lines.append(f"🏬 Код склада Smartup: {', '.join(warehouse_codes)}")

        if status_code == "C":
            lines.append("🚫 Заявка отменена")
            lines.append("➡️ Проверки WMS и бухгалтерии не требуются")
            lines.append("👤 По вопросам отмены обратитесь к оператору")
            cancelled = True

        elif status_code == "D":
            lines.append("⚠️ Заявка сохранена как черновик и ещё не запущена в работу")
            lines.append("➡️ Оператор должен завершить оформление заявки и перевести её в стадию «Новый»")
            lines.append("👤 Обратитесь к оператору")
            problems += 1
            add_unique(problem_reasons, "Заявка находится в черновике и ещё не запущена в работу")
            add_unique(required_actions, "Завершить оформление и перевести заявку в стадию «Новый»")
            add_unique(responsible_people, "оператор")

        elif status_code == "B#N":
            lines.append("⚠️ Заявка ещё не передана на финансовую проверку")
            lines.append("➡️ Оператор должен перевести заявку в стадию «В обработке»")
            lines.append("➡️ После этого заявка будет передана в финансовый отдел")
            lines.append("👤 Обратитесь к оператору")
            problems += 1
            add_unique(problem_reasons, "Заявка ещё не передана на финансовую проверку")
            add_unique(required_actions, "Перевести заявку в стадию «В обработке»")
            add_unique(responsible_people, "оператор")

        elif status_code == "B#E":
            lines.append("⏳ Заявка передана в финансовый отдел")
            lines.append("➡️ Выполняется проверка в системе «Плюсовой баланс»")
            lines.append("👤 Если стадия долго не меняется — обратитесь в финансовый отдел")

        elif status_code in ("B#W", "B#S", "B#V"):
            region_results = [item for item in results.get("regions", []) if item.get("selected", True)]
            if not results.get("warehouse_routing_exact", True):
                lines.append("ℹ️ Склад не сопоставлен с настройками: выполнен поиск по WMS")
                matched = [
                    item for item in region_results
                    if not item.get("wms", {}).get("_error")
                    and bool_value(item.get("wms", {}), "document_found", "order_found")
                ]
                if matched:
                    region_results = [
                        item for item in region_results
                        if item in matched or item.get("wms", {}).get("_error")
                    ]
            if not region_results:
                lines.append("❌ WMS проверить не удалось: нет настроенных складов")
                unavailable += 1
                add_unique(unavailable_reasons, "Для склада не настроена WMS")
                add_unique(required_actions, "Обратиться к администратору интеграции")
                add_unique(responsible_people, "администратор интеграции")
            for item in region_results:
                lines.extend(["", f"📍 Регион: {item['name']}"])
                details, issue_count, error_count = format_wms(
                    item.get("wms", {}), status_code, item.get("wms_responsible", ""),
                    item.get("series_check"),
                )
                lines.extend(details)
                problems += issue_count
                unavailable += error_count
                reason, action, owner = wms_summary(
                    item.get("wms", {}), status_code, item.get("series_check"),
                    item.get("wms_responsible", ""),
                )
                if error_count:
                    add_unique(unavailable_reasons, reason)
                elif issue_count:
                    add_unique(problem_reasons, reason)
                if issue_count or error_count:
                    add_unique(required_actions, action)
                    add_unique(responsible_people, owner)

        elif status_code == "A":
            accounting = results.get("accounting", {})
            if accounting.get("_error"):
                lines.append(f"❌ Бухгалтерия недоступна: {describe_error(accounting['_error'])}")
                lines.append("➡️ Не удалось проверить отправку ЭСФ")
                unavailable += 1
                add_unique(unavailable_reasons, "Бухгалтерия недоступна, статус ЭСФ не проверен")
                add_unique(required_actions, "Повторить проверку или обратиться в бухгалтерию")
                add_unique(responsible_people, "бухгалтерия")
            else:
                document_found = bool_value(accounting, "document_found", "order_found")
                document_posted = bool_value(accounting, "document_posted")
                invoice_sent = bool_value(accounting, "invoice_sent")
                lines.append("✅ Бухгалтерия: документ найден" if document_found else "❌ Бухгалтерия: документ не найден")
                if document_found:
                    lines.append("✅ Документ проведён" if document_posted else "⚠️ Документ не проведён")
                lines.append("✅ ЭСФ отправлена" if invoice_sent else "❌ ЭСФ не отправлена")
                if document_found and document_posted and invoice_sent:
                    lines.append("✅ Заявка готова: ЭСФ отправлена")
                else:
                    lines.append("➡️ Обратитесь в бухгалтерию")
                    problems += 1
                    if not document_found:
                        add_unique(problem_reasons, "Документ не найден в бухгалтерии")
                    elif not document_posted:
                        add_unique(problem_reasons, "Документ в бухгалтерии не проведён")
                    if not invoice_sent:
                        add_unique(problem_reasons, "ЭСФ не отправлена")
                    add_unique(required_actions, "Обратиться в бухгалтерию")
                    add_unique(responsible_people, "бухгалтерия")

        else:
            lines.append("⚠️ Для этой стадии правило проверки ещё не настроено")
            lines.append("👤 Обратитесь к администратору бота")
            problems += 1
            add_unique(problem_reasons, "Для текущей стадии правило проверки не настроено")
            add_unique(required_actions, "Обратиться к администратору бота")
            add_unique(responsible_people, "администратор бота")

    elapsed = float(results.get("_elapsed", 0))
    lines.extend(["", "━━━━━━━━━━━━━━"])
    if cancelled and unavailable == 0:
        lines.append("ℹ️ ИТОГ: ЗАЯВКА ОТМЕНЕНА")
    elif problems == 0 and unavailable == 0:
        lines.append("✅ ИТОГ: ОШИБОК НЕ НАЙДЕНО")
    else:
        if problems > 0:
            lines.append("❌ ИТОГ: ЕСТЬ ПРОБЛЕМА")
        else:
            lines.append("⚠️ ИТОГ: ПРОВЕРКА НЕ ЗАВЕРШЕНА")
        for reason in problem_reasons:
            lines.append(f"🔎 Причина: {reason}")
        for reason in unavailable_reasons:
            lines.append(f"⚠️ Не проверено: {reason}")
        for action in required_actions:
            lines.append(f"➡️ Что делать: {action}")
        for owner in responsible_people:
            lines.append(f"👤 Ответственный: {owner}")
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

    results["warehouse_routing_exact"] = bool(matched_region_indexes)
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
                jobs[pool.submit(request_wms, region.get("wms", {}), doc_number, timeout)] = ("region", "wms", index)

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

    if need_wms and not smartup.get("_error"):
        for item in results["regions"]:
            wms = item.get("wms")
            if not isinstance(wms, dict) or wms.get("_error"):
                continue
            if (
                wms.get("document_found")
                and wms.get("assembly_found")
                and wms.get("assembly_finished")
                and not wms.get("basket_has_items")
            ):
                item["series_check"] = compare_series(smartup, wms)

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
            tasks.append((f"WMS {name}", pool.submit(request_wms, region.get("wms", {}), health_doc, timeout)))

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
    configured = config.get("allowed_chat_ids", [])
    if isinstance(configured, (str, int)):
        configured = [configured]
    allowed = {str(item) for item in configured}
    approved = set(load_access_state().get("approved_chat_ids", []))
    return str(chat_id) in allowed or str(chat_id) in approved


def is_admin(config, user_id):
    return str(user_id) in admin_ids(config)


def chat_description(message):
    chat = message.get("chat", {})
    sender = message.get("from", {})
    title = str(chat.get("title") or "").strip()
    if not title:
        title = " ".join(
            part for part in (str(sender.get("first_name") or "").strip(), str(sender.get("last_name") or "").strip())
            if part
        )
    username = str(chat.get("username") or sender.get("username") or "").strip()
    return title or "Без названия", username


def request_access(config, message):
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id", chat_id)
    if is_allowed(config, chat_id):
        return "✅ У этого чата уже есть доступ. Можно проверять заявки."

    state = load_access_state()
    key = str(chat_id)
    title, username = chat_description(message)
    if key in state["pending"]:
        return "⏳ Заявка на доступ уже отправлена администратору."

    state["pending"][key] = {
        "chat_id": key,
        "chat_title": title,
        "chat_type": str(message.get("chat", {}).get("type") or ""),
        "username": username,
        "requester_id": str(user_id),
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_access_state(state)

    admins = admin_ids(config)
    if not admins:
        return (
            "⚠️ Заявка сохранена, но администратор не настроен. "
            "Добавьте admin_chat_ids в config.json."
        )

    username_line = f"\nUsername: @{username}" if username else ""
    admin_text = (
        "🔐 Запрос доступа к боту\n\n"
        f"Чат: {title}\n"
        f"Chat ID: {chat_id}\n"
        f"User ID: {user_id}{username_line}"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Разрешить", "callback_data": f"access:approve:{chat_id}"},
            {"text": "❌ Отклонить", "callback_data": f"access:reject:{chat_id}"},
        ]]
    }
    delivered = 0
    for admin_id in admins:
        try:
            telegram_call(
                config["telegram_bot_token"],
                "sendMessage",
                {
                    "chat_id": admin_id,
                    "text": admin_text,
                    "reply_markup": json.dumps(keyboard, ensure_ascii=False),
                },
            )
            delivered += 1
        except Exception as error:
            print(f"Не удалось уведомить администратора {admin_id}: {error}",
                  file=sys.stderr, flush=True)

    if delivered:
        return "✅ Заявка на доступ отправлена администратору. Бот сообщит о решении в этом чате."
    return "⚠️ Заявка сохранена, но уведомить администратора не удалось. Обратитесь к нему напрямую."


def handle_callback(config, callback):
    token = config["telegram_bot_token"]
    callback_id = callback.get("id")
    data = str(callback.get("data") or "")
    admin_user_id = callback.get("from", {}).get("id")
    message = callback.get("message", {})

    def acknowledge(text, alert=False):
        telegram_call(
            token,
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text, "show_alert": "true" if alert else "false"},
        )

    match = re.fullmatch(r"access:(approve|reject):(-?\d+)", data)
    if not match:
        acknowledge("Неизвестное действие", True)
        return
    if not is_admin(config, admin_user_id):
        acknowledge("У вас нет прав администратора", True)
        return

    action, target_chat_id = match.groups()
    state = load_access_state()
    request_info = state["pending"].get(target_chat_id)
    if request_info is None:
        acknowledge("Запрос уже обработан", True)
        return

    state["pending"].pop(target_chat_id, None)
    approved = set(state["approved_chat_ids"])
    if action == "approve":
        approved.add(target_chat_id)
    state["approved_chat_ids"] = sorted(approved)
    save_access_state(state)

    decision = "✅ Доступ разрешён" if action == "approve" else "❌ Доступ отклонён"
    acknowledge(decision)
    callback_chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if callback_chat_id is not None and message_id is not None:
        original = str(message.get("text") or "Запрос доступа")
        try:
            telegram_call(
                token,
                "editMessageText",
                {"chat_id": callback_chat_id, "message_id": message_id, "text": f"{original}\n\n{decision}"},
            )
        except Exception:
            pass

    if action == "approve":
        target_text = "✅ Доступ к боту разрешён. Теперь можно проверять заявки."
        target_keyboard = main_keyboard(True)
    else:
        target_text = "❌ Администратор отклонил заявку на доступ."
        target_keyboard = main_keyboard(False)
    try:
        telegram_call(
            token,
            "sendMessage",
            {
                "chat_id": target_chat_id,
                "text": target_text,
                "reply_markup": json.dumps(target_keyboard, ensure_ascii=False),
            },
        )
    except Exception:
        pass


def parse_document_numbers(text):
    value = str(text or "").strip()
    if value.startswith("/"):
        parts = value.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        if command == "/check":
            value = parts[1] if len(parts) > 1 else ""

    documents = []
    invalid = []
    for item in re.split(r"[\s,;]+", value):
        number = item.strip()
        if not number:
            continue
        if not DOC_RE.fullmatch(number):
            invalid.append(number)
        elif number not in documents:
            documents.append(number)
    return documents, invalid


def user_mention_html(message):
    sender = message.get("from", {})
    user_id = sender.get("id")
    username = str(sender.get("username") or "").strip()
    first_name = str(sender.get("first_name") or "").strip()
    last_name = str(sender.get("last_name") or "").strip()
    name = f"@{username}" if username else " ".join(part for part in (first_name, last_name) if part)
    name = name or "Пользователь"
    if user_id is None:
        return html.escape(name)
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def split_telegram_text(text, limit=4000):
    chunks = []
    current = ""
    for line in str(text).splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks or [""]


def check_orders(config, document_numbers):
    results = [None] * len(document_numbers)
    configured_workers = int(config.get("batch_workers", 3))
    workers = min(len(document_numbers), max(1, configured_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(check_order, config, number): index for index, number in enumerate(document_numbers)}
        for job in as_completed(jobs):
            index = jobs[job]
            try:
                results[index] = job.result()
            except Exception as error:
                results[index] = (
                    f"Заявка {document_numbers[index]}\n\n"
                    f"❌ Проверка завершилась ошибкой: {describe_error(error)}"
                )
    return results


def send_batch_check(config, message, document_numbers):
    token = config["telegram_bot_token"]
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    mention = user_mention_html(message)
    count = len(document_numbers)
    if count == 1:
        request_word = "заявку"
    elif 2 <= count <= 4:
        request_word = "заявки"
    else:
        request_word = "заявок"
    preview = ", ".join(html.escape(number) for number in document_numbers)
    reply_parameters = None
    if message_id is not None:
        reply_parameters = json.dumps({"message_id": message_id, "allow_sending_without_reply": True})

    progress_params = {
        "chat_id": chat_id,
        "text": f"🔎 {mention}, проверяю {count} {request_word}: {preview}",
        "parse_mode": "HTML",
    }
    if reply_parameters:
        progress_params["reply_parameters"] = reply_parameters
    telegram_call(token, "sendMessage", progress_params)

    results = check_orders(config, document_numbers)
    for index, result in enumerate(results, start=1):
        heading = f"Результат {index}/{count}\n\n" if count > 1 else ""
        chunks = split_telegram_text(heading + result)
        for chunk in chunks:
            params = {"chat_id": chat_id, "text": chunk}
            if reply_parameters:
                params["reply_parameters"] = reply_parameters
            telegram_call(token, "sendMessage", params)

    finished_params = {
        "chat_id": chat_id,
        "text": f"✅ {mention}, проверка завершена. Проверено заявок: {count}.",
        "parse_mode": "HTML",
        "reply_markup": json.dumps(main_keyboard(True), ensure_ascii=False),
    }
    if reply_parameters:
        finished_params["reply_parameters"] = reply_parameters
    telegram_call(token, "sendMessage", finished_params)


def handle_message(config, message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user_id = message.get("from", {}).get("id", chat_id)
    state_key = (str(chat_id), str(user_id))
    text = str(message.get("text", "")).strip()
    if chat_id is None:
        return

    reply_markup = main_keyboard(is_allowed(config, chat_id))
    reply_parameters = None
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    allowed = is_allowed(config, chat_id)

    if command == "/chatid" or text == BUTTON_CHAT_ID:
        answer = f"🆔 ID этого чата: {chat_id}"
    elif command == "/start":
        WAITING_FOR_DOC.discard(state_key)
        if allowed:
            answer = "👋 Бот контроля заявок готов.\n\nНажмите «🔍 Проверить заявку» или просто отправьте её номер."
        else:
            answer = "👋 Бот контроля заявок.\n\nЧтобы начать работу, нажмите «📝 Запросить доступ»."
    elif command == "/help" or text == BUTTON_HELP:
        WAITING_FOR_DOC.discard(state_key)
        answer = help_text()
    elif command == "/register" or text == BUTTON_REGISTER:
        WAITING_FOR_DOC.discard(state_key)
        answer = request_access(config, message)
        allowed = is_allowed(config, chat_id)
    elif not allowed:
        answer = "🔒 У этого чата нет доступа. Нажмите «📝 Запросить доступ»."
    elif command == "/health" or text == BUTTON_HEALTH:
        WAITING_FOR_DOC.discard(state_key)
        telegram_call(config["telegram_bot_token"], "sendChatAction", {"chat_id": chat_id, "action": "typing"})
        answer = check_health(config)
    elif text == BUTTON_CHECK:
        WAITING_FOR_DOC.add(state_key)
        answer = "Введите ID заявки из Smartup.\nНапример: 4411939"
        reply_markup = force_reply_keyboard()
        if message.get("message_id") is not None:
            reply_parameters = {"message_id": message["message_id"]}
    elif command == "/cancel" or text == BUTTON_CANCEL:
        WAITING_FOR_DOC.discard(state_key)
        answer = "Проверка отменена."
    else:
        documents, invalid = parse_document_numbers(text)
        max_batch = max(1, int(config.get("max_batch_documents", 10)))
        if command and command != "/check":
            answer = "Команда не распознана. Нажмите «ℹ️ Инструкция»."
        elif command == "/check" and not documents and not invalid:
            WAITING_FOR_DOC.add(state_key)
            answer = "Введите один или несколько ID заявок из Smartup."
            reply_markup = force_reply_keyboard()
            if message.get("message_id") is not None:
                reply_parameters = {"message_id": message["message_id"]}
        elif invalid or not documents:
            if state_key in WAITING_FOR_DOC:
                invalid_text = f"\nНекорректные значения: {', '.join(invalid[:5])}" if invalid else ""
                answer = (
                    "Неверный формат. Отправьте ID через пробел, запятую или с новой строки."
                    f"\nНапример: 4411939 4411940{invalid_text}"
                )
                reply_markup = force_reply_keyboard()
                if message.get("message_id") is not None:
                    reply_parameters = {"message_id": message["message_id"]}
            else:
                answer = "Команда не распознана. Нажмите «ℹ️ Инструкция»."
        elif len(documents) > max_batch:
            answer = f"⚠️ За один раз можно проверить не больше {max_batch} заявок. Получено: {len(documents)}."
            reply_markup = force_reply_keyboard()
            if message.get("message_id") is not None:
                reply_parameters = {"message_id": message["message_id"]}
        else:
            WAITING_FOR_DOC.discard(state_key)
            send_batch_check(config, message, documents)
            return

    if not reply_markup.get("force_reply"):
        reply_markup = main_keyboard(is_allowed(config, chat_id))

    params = {
        "chat_id": chat_id,
        "text": answer,
        "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
    }
    if reply_parameters is not None:
        params["reply_parameters"] = json.dumps(reply_parameters)
    telegram_call(config["telegram_bot_token"], "sendMessage", params)


def main():
    try:
        config = load_config()
    except Exception as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 1

    token = config["telegram_bot_token"]

    # Webhook и getUpdates взаимно исключают друг друга: если на токене висит
    # webhook, Telegram отвечает на getUpdates ошибкой 409 Conflict и бот не
    # получает ни одного обновления. Снимаем его перед началом опроса.
    try:
        telegram_call(token, "deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as error:
        print(f"Не удалось снять webhook: {error}", file=sys.stderr, flush=True)

    print("Бот запущен", flush=True)
    offset = 0
    failures = 0
    while True:
        try:
            updates = telegram_call(token, "getUpdates", {"offset": offset, "timeout": 30}, timeout=40)
            failures = 0
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                try:
                    if "message" in update:
                        handle_message(config, update["message"])
                    elif "callback_query" in update:
                        handle_callback(config, update["callback_query"])
                except Exception as error:
                    # Сбой на одном обновлении не должен останавливать опрос.
                    print(f"Ошибка обработки обновления {update.get('update_id')}: {error}",
                          file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            print("Бот остановлен", flush=True)
            return 0
        except Exception as error:
            failures += 1
            print(f"Ошибка опроса Telegram: {error}", file=sys.stderr, flush=True)
            time.sleep(min(3 * failures, 30))


if __name__ == "__main__":
    raise SystemExit(main())
