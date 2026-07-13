from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import traceback
import unicodedata
import webbrowser
import zipfile
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse


FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
STATIC_DIR = RESOURCE_DIR / "static"
USER_DIR = APP_DIR / "user_data"
BACKUP_DIR = USER_DIR / "backups"
SETTINGS_FILE = USER_DIR / "settings.json"
OVERRIDES_FILE = USER_DIR / "overrides.json"
HISTORY_FILE = USER_DIR / "history.jsonl"
LOG_FILE = USER_DIR / "app.log"
EXIT_PROCESS_ON_SHUTDOWN = False


CATEGORY_DEFS = [
    {"id": "all", "label": "全部"},
    {"id": "story", "label": "剧情"},
    {"id": "skill", "label": "技能"},
    {"id": "passive", "label": "被动"},
    {"id": "ego", "label": "E.G.O"},
    {"id": "voice", "label": "语音/台词"},
    {"id": "abnormality", "label": "异想体"},
    {"id": "enemy", "label": "敌人"},
    {"id": "keyword", "label": "关键词/状态"},
    {"id": "item", "label": "物品/饰品"},
    {"id": "ui", "label": "界面"},
    {"id": "other", "label": "其他"},
]
CATEGORY_LABELS = {item["id"]: item["label"] for item in CATEGORY_DEFS}


SINNER_DEFS = [
    {"id": "all", "label": "全部罪人", "aliases": []},
    {"id": "01", "label": "李箱", "aliases": ["yisang", "yi_sang", "yi-sang"]},
    {"id": "02", "label": "浮士德", "aliases": ["faust"]},
    {"id": "03", "label": "堂吉诃德", "aliases": ["donquixote", "don_quixote", "don-quixote"]},
    {"id": "04", "label": "良秀", "aliases": ["ryoshu", "ryosyu"]},
    {"id": "05", "label": "默尔索", "aliases": ["meursault", "merusault"]},
    {"id": "06", "label": "鸿璐", "aliases": ["honglu", "hong_lu", "hong-lu"]},
    {"id": "07", "label": "希斯克利夫", "aliases": ["heathcliff"]},
    {"id": "08", "label": "以实玛利", "aliases": ["ishmael"]},
    {"id": "09", "label": "罗佳", "aliases": ["rodion", "rodya"]},
    {"id": "10", "label": "辛克莱", "aliases": ["sinclair"]},
    {"id": "11", "label": "奥提斯", "aliases": ["outis"]},
    {"id": "12", "label": "格里高尔", "aliases": ["gregor"]},
]
SINNER_BY_ID = {item["id"]: item for item in SINNER_DEFS}
SINNER_ALIASES = {
    alias: item["id"]
    for item in SINNER_DEFS
    for alias in item["aliases"]
}


class UserFacingError(Exception):
    pass


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def local_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def ensure_user_dirs() -> None:
    USER_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)


def app_log(message: str) -> None:
    text = f"[{utc_now_iso()}] {message}"
    stream = getattr(sys, "stdout", None)
    if stream:
        try:
            stream.write(text + "\n")
            stream.flush()
            return
        except Exception:
            pass
    try:
        ensure_user_dirs()
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception:
        pass


def load_json_sidecar(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_sidecar(path: Path, data) -> None:
    ensure_user_dirs()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def read_json_document(path: Path):
    raw = path.read_bytes()
    encodings = ["utf-8-sig"] if raw.startswith(b"\xef\xbb\xbf") else ["utf-8", "gb18030"]
    last_error = None
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            data = json.loads(text)
            newline = "\r\n" if "\r\n" in text else "\n"
            return data, encoding, newline
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
    raise UserFacingError(f"无法解析 JSON：{path.name} ({last_error})")


def write_json_document(path: Path, data, encoding: str, newline: str) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    text = text.replace("\n", newline) + newline
    path.write_bytes(text.encode(encoding))


def count_json_files(path: Path) -> int:
    try:
        return sum(1 for item in path.rglob("*.json") if item.is_file())
    except OSError:
        return 0


def resolve_lang_dir(raw_path: str) -> Path:
    if not raw_path or not raw_path.strip():
        raise UserFacingError("请先输入汉化文件夹路径。")

    cleaned = raw_path.strip().strip('"').strip("'")
    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        path = (APP_DIR / path).resolve()
    if not path.exists() or not path.is_dir():
        raise UserFacingError("路径不存在，或不是文件夹。")

    candidates = []
    for language_dir in ("LLC_zh-CN", "zh-CN"):
        child = path / language_dir
        if child.is_dir():
            candidates.append(child)
    candidates.append(path)

    for candidate in candidates:
        if count_json_files(candidate) > 0:
            return candidate.resolve()

    raise UserFacingError("这个文件夹下没有找到 JSON 汉化文件。")


def sample_path() -> str:
    preferred = APP_DIR / "Lang" / "LLC_zh-CN"
    if preferred.is_dir():
        return str(preferred)
    fallback = APP_DIR / "Lang"
    if fallback.is_dir():
        return str(fallback)
    return ""


def pick_folder_dialog() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise UserFacingError(f"当前 Python 环境无法打开文件夹选择窗口：{exc}") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        initial = str(STATE.bound_path or sample_path() or APP_DIR)
        selected = filedialog.askdirectory(
            parent=root,
            title="选择零协汉化 JSON 文件夹",
            initialdir=initial if Path(initial).exists() else str(APP_DIR),
            mustexist=True,
        )
        return selected or ""
    finally:
        root.destroy()


def posix_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def normalise_rel(rel: str) -> str:
    rel = rel.replace("\\", "/").strip("/")
    parts = PurePosixPath(rel).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UserFacingError("文件路径不合法。")
    return PurePosixPath(*parts).as_posix()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def path_to_pointer(path: list) -> str:
    def escape(token) -> str:
        return str(token).replace("~", "~0").replace("/", "~1")

    return "/" + "/".join(escape(token) for token in path)


def path_to_display(path: list) -> str:
    display = "$"
    for token in path:
        if isinstance(token, int):
            display += f"[{token}]"
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(token)):
            display += f".{token}"
        else:
            display += f"[{json.dumps(str(token), ensure_ascii=False)}]"
    return display


INTERNAL_FIELD_NAMES = {
    "key",
    "code",
    "type",
    "subtype",
    "group",
    "sort",
    "order",
    "index",
    "filename",
    "filepath",
    "path",
    "url",
    "icon",
    "iconid",
    "image",
    "imageid",
    "sprite",
    "spriteid",
    "asset",
    "assetid",
    "resource",
    "resourceid",
    "modelid",
    "prefab",
    "prefabname",
    "sound",
    "soundid",
    "voice",
    "voiceid",
    "effect",
    "effectid",
    "animation",
    "animationid",
    "anim",
    "animid",
}
ALWAYS_INTERNAL_FIELD_NAMES = {"id", "ids", "uid", "uuid", "guid"}
INTERNAL_FIELD_HINTS = (
    "resource",
    "asset",
    "prefab",
    "sprite",
    "icon",
    "image",
    "model",
    "sound",
    "voice",
    "effect",
    "anim",
    "path",
    "file",
)
IDENTIFIER_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/\\#@+\-]+$")
REFERENCE_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_.:/\\#@+\-]{1,120})\]")


def normalise_field_name(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def looks_like_identifier_value(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if len(text) > 180:
        return False
    return bool(IDENTIFIER_VALUE_RE.fullmatch(text) and re.search(r"[A-Za-z]", text))


def is_internal_string_field(path: list, value: str) -> bool:
    if not path:
        return False
    field = normalise_field_name(path[-1])
    if field in ALWAYS_INTERNAL_FIELD_NAMES:
        return True
    if field in INTERNAL_FIELD_NAMES and looks_like_identifier_value(value):
        return True
    if looks_like_identifier_value(value):
        if field.endswith(("id", "ids", "key", "code")):
            return True
        if any(hint in field for hint in INTERNAL_FIELD_HINTS):
            return True
    return False


def is_default_placeholder_field(path: list, value: str) -> bool:
    text = value.strip()
    if not text:
        return True
    field = normalise_field_name(path[-1]) if path else ""
    return field == "undefined" and text in {"", "-"}


def walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_dicts(value)


def collect_keyword_aliases(data, category: str) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    if category != "keyword":
        return aliases
    for item in walk_dicts(data):
        entry_id = item.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            continue
        for key in ("name", "title", "summary"):
            value = item.get(key)
            if isinstance(value, str) and value.strip() and not is_default_placeholder_field([key], value):
                aliases.setdefault(entry_id, set()).add(value.strip())
    return aliases


def referenced_aliases(value: str, keyword_aliases: dict[str, set[str]]) -> list[str]:
    if not keyword_aliases:
        return []
    aliases = []
    seen = set()
    for token in REFERENCE_TOKEN_RE.findall(value):
        for alias in keyword_aliases.get(token, ()):
            if alias not in seen:
                aliases.append(alias)
                seen.add(alias)
    return aliases


def walk_strings(obj, path=None, ancestors=None):
    path = [] if path is None else path
    ancestors = [] if ancestors is None else ancestors
    if isinstance(obj, str):
        yield path, obj, ancestors
        return
    if isinstance(obj, dict):
        next_ancestors = ancestors + [obj]
        for key, value in obj.items():
            yield from walk_strings(value, path + [key], next_ancestors)
        return
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from walk_strings(value, path + [index], ancestors)


def get_by_path(obj, path: list):
    current = obj
    for token in path:
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise UserFacingError("JSON 路径指向的内容已经不存在。")
    return current


def set_by_path(obj, path: list, value: str) -> None:
    if not path:
        raise UserFacingError("不能把整个 JSON 文件替换成字符串。")
    parent = get_by_path(obj, path[:-1])
    leaf = path[-1]
    if isinstance(parent, list):
        parent[int(leaf)] = value
    elif isinstance(parent, dict):
        parent[leaf] = value
    else:
        raise UserFacingError("JSON 路径指向的内容已经不存在。")


def clean_preview(value: str, limit: int = 180) -> str:
    value = value.replace("\r\n", "\\n").replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


FORMAT_TOKEN_PATTERNS = [
    ("brace", "占位符", re.compile(r"\{[A-Za-z0-9_.$:+#,\- ]{1,80}\}")),
    ("percent", "百分号格式符", re.compile(r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[bcdeEfFgGiosuxX]")),
    ("tag", "富文本标签", re.compile(r"</?[A-Za-z][A-Za-z0-9_:-]*(?:\s+[^<>\r\n]{0,180})?/?>")),
    ("square", "方括号标记", re.compile(r"\[[A-Za-z0-9_./:-]{1,80}\]")),
    ("escape", "反斜杠控制符", re.compile(r"\\[nrt]")),
]
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
COMPACT_SEARCH_MIN_LENGTH = 3
FORMAT_SEARCH_CHARS = set("{}<>[]%")


def counter_for_pattern(pattern: re.Pattern, value: str) -> Counter:
    return Counter(match.group(0) for match in pattern.finditer(value))


def token_sample(values: list[str], limit: int = 5) -> str:
    items = [clean_preview(item, 40) for item in values[:limit]]
    suffix = " 等" if len(values) > limit else ""
    return "、".join(items) + suffix


def compare_format_tokens(old_value: str, new_value: str) -> list[dict]:
    warnings = []
    for kind, label, pattern in FORMAT_TOKEN_PATTERNS:
        before = counter_for_pattern(pattern, old_value)
        after = counter_for_pattern(pattern, new_value)
        missing = list((before - after).elements())
        added = list((after - before).elements())
        if not missing and not added:
            continue
        parts = []
        if missing:
            parts.append(f"缺少 {token_sample(missing)}")
        if added:
            parts.append(f"新增 {token_sample(added)}")
        warnings.append(
            {
                "kind": kind,
                "label": label,
                "message": f"{label}变化：{'；'.join(parts)}。",
            }
        )
    return warnings


def text_safety_warnings(old_value: str, new_value: str) -> list[dict]:
    if old_value == new_value:
        return []

    warnings = compare_format_tokens(old_value, new_value)
    old_lines = old_value.count("\n")
    new_lines = new_value.count("\n")
    if old_lines != new_lines:
        warnings.append(
            {
                "kind": "newline",
                "label": "换行",
                "message": f"换行数量变化：原 {old_lines + 1} 行，新 {new_lines + 1} 行。",
            }
        )

    control_count = len(CONTROL_CHAR_RE.findall(new_value))
    if control_count:
        warnings.append(
            {
                "kind": "control",
                "label": "控制字符",
                "message": f"新文本包含 {control_count} 个不可见控制字符，可能导致游戏解析异常。",
            }
        )
    return warnings


def safety_warning_entry(file: str, field: str, path_display: str, old_value: str, new_value: str) -> dict | None:
    warnings = text_safety_warnings(old_value, new_value)
    if not warnings:
        return None
    return {
        "file": file,
        "field": field,
        "pathDisplay": path_display,
        "before": clean_preview(old_value, 140),
        "after": clean_preview(new_value, 140),
        "warnings": warnings,
    }


DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\ufe58": "-",
        "\ufe63": "-",
        "\uff0d": "-",
        "\\": "/",
    }
)


def normalize_search_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    return text.translate(DASH_TRANSLATION).casefold()


def compact_normalized_search_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def compact_search_text(value) -> str:
    return compact_normalized_search_text(normalize_search_text(value))


def prepare_search_query(query: str) -> tuple[str, str]:
    normalized = normalize_search_text(query).strip()
    if not normalized:
        return "", ""
    compact = compact_search_text(query)
    if len(compact) < COMPACT_SEARCH_MIN_LENGTH or any(char in normalized for char in FORMAT_SEARCH_CHARS):
        compact = ""
    return normalized, compact


def make_search_blob(parts: list) -> tuple[str, str]:
    normalized = "\n".join(normalize_search_text(part) for part in parts if part is not None)
    return normalized, compact_normalized_search_text(normalized)


def search_matches_prepared(prepared_query: tuple[str, str], normalized_haystack: str, compact_haystack: str) -> bool:
    normalized_query, compact_query = prepared_query
    if not normalized_query:
        return True
    if normalized_query in normalized_haystack:
        return True
    return bool(compact_query and compact_query in compact_haystack)


def search_matches(query: str, parts: list) -> bool:
    normalized_haystack, compact_haystack = make_search_blob(parts)
    return search_matches_prepared(prepare_search_query(query), normalized_haystack, compact_haystack)


def public_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def term_occurrences(value: str, old_text: str, match_case: bool) -> int:
    if not old_text:
        return 0
    if match_case:
        return value.count(old_text)
    return len(re.findall(re.escape(old_text), value, flags=re.IGNORECASE))


def replace_term(value: str, old_text: str, new_text: str, match_case: bool) -> str:
    if match_case:
        return value.replace(old_text, new_text)
    return re.sub(re.escape(old_text), lambda _match: new_text, value, flags=re.IGNORECASE)


def infer_category(rel: str) -> str:
    rel_lower = rel.lower()
    name = PurePosixPath(rel_lower).name
    if rel_lower.startswith("storydata/") or name.startswith("story") or "storytheater" in name:
        return "story"
    if "personalityvoicedlg/" in rel_lower or name.startswith("abdlg_") or name.startswith("voice_"):
        return "voice"
    if "abnormality" in name or name.startswith("abevents") or name.startswith("abnormalityguides"):
        return "abnormality"
    if name.startswith("enemies") or name.startswith("skills_enemy") or "enemy" in name:
        return "enemy"
    if name.startswith("skills_ego") or name.startswith("egos") or name.startswith("ego_get"):
        return "ego"
    if name.startswith("passive"):
        return "passive"
    if name == "skills.json" or name.startswith("skills-") or name.startswith("skills_") or name.startswith("skills"):
        return "skill"
    if (
        name.startswith("bufs")
        or name.startswith("battlekeywords")
        or name.startswith("keyword")
        or name.startswith("attribute")
        or name.startswith("mentalcondition")
        or name.startswith("panicinfo")
    ):
        return "keyword"
    if name.startswith("items") or name.startswith("egogift") or name.startswith("iap") or name.startswith("battlepass"):
        return "item"
    if "ui" in name or name.startswith(("main", "login", "filter", "formation", "coupon", "mission")):
        return "ui"
    return "other"


def code_from_numeric_id(entry_id) -> str | None:
    if entry_id is None:
        return None
    digits = re.sub(r"\D", "", str(entry_id))
    if len(digits) >= 3:
        code = digits[1:3]
        if code in SINNER_BY_ID:
            return code
    return None


def code_from_alias_text(*values) -> str | None:
    compact_text = "".join(re.sub(r"[^a-z0-9]", "", str(value).casefold()) for value in values if value is not None)
    if not compact_text:
        return None
    for alias, code in SINNER_ALIASES.items():
        compact_alias = re.sub(r"[^a-z0-9]", "", alias)
        if compact_alias and compact_alias in compact_text:
            return code
    return None


def infer_sinner(rel: str, entry_id) -> str | None:
    rel_lower = rel.lower().replace("\\", "/")
    match = re.search(r"(?:skills_ego_personality|skills_personality)-(\d{2})", rel_lower)
    if match and match.group(1) in SINNER_BY_ID:
        return match.group(1)

    return code_from_alias_text(rel_lower, entry_id) or code_from_numeric_id(entry_id)


def extract_entry_id(ancestors: list[dict]):
    entry_id = None
    for item in ancestors:
        if isinstance(item, dict) and "id" in item:
            entry_id = item.get("id")
    return entry_id


def extract_label(ancestors: list[dict], leaf_key, leaf_value: str) -> str:
    preferred_keys = ("nameWithTitle", "title", "name", "abName", "teller", "place", "model", "desc")
    for item in reversed(ancestors):
        if not isinstance(item, dict):
            continue
        for key in preferred_keys:
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if key == leaf_key and value == leaf_value and key not in {"nameWithTitle", "title", "name", "abName"}:
                continue
            return clean_preview(value, 80)
    entry_id = extract_entry_id(ancestors)
    return str(entry_id) if entry_id is not None else ""


def make_key(rel: str, path: list) -> str:
    return f"{normalise_rel(rel)}|{path_to_pointer(path)}"


def backup_file(source: Path, bound_root: Path, stamp: str | None = None) -> str:
    stamp = stamp or local_stamp()
    rel = posix_rel(source, bound_root)
    destination = BACKUP_DIR / stamp / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return str(destination)


def append_history(entry: dict) -> None:
    ensure_user_dirs()
    entry = {"time": utc_now_iso(), **entry}
    with HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def new_operation_id(prefix: str) -> str:
    return f"{prefix}_{local_stamp()}"


def read_history_entries() -> list[tuple[int, dict]]:
    if not HISTORY_FILE.exists():
        return []
    entries = []
    for index, line in enumerate(HISTORY_FILE.read_text(encoding="utf-8").splitlines()):
        try:
            entries.append((index, json.loads(line)))
        except json.JSONDecodeError:
            continue
    return entries


class AppState:
    def __init__(self):
        ensure_user_dirs()
        self.lock = threading.RLock()
        self.bound_path: Path | None = None
        self.input_path: str = ""
        self.index: list[dict] = []
        self.stats: dict = {}
        self.index_valid = False
        self.load_settings()

    def load_settings(self) -> None:
        settings = load_json_sidecar(SETTINGS_FILE, {})
        raw_bound = settings.get("boundPath")
        raw_input = settings.get("inputPath", "")
        if raw_bound:
            candidate = Path(raw_bound)
            if candidate.exists() and candidate.is_dir():
                self.bound_path = candidate.resolve()
                self.input_path = raw_input or str(candidate)

    def save_settings(self) -> None:
        save_json_sidecar(
            SETTINGS_FILE,
            {
                "inputPath": self.input_path,
                "boundPath": str(self.bound_path) if self.bound_path else "",
                "updatedAt": utc_now_iso(),
            },
        )

    def bind(self, raw_path: str) -> dict:
        lang_dir = resolve_lang_dir(raw_path)
        with self.lock:
            self.bound_path = lang_dir
            self.input_path = raw_path
            self.index_valid = False
            self.save_settings()
            self.rebuild_index_locked()
        return self.status()

    def require_bound(self) -> Path:
        if not self.bound_path:
            raise UserFacingError("请先绑定汉化 JSON 文件夹。")
        return self.bound_path

    def resolve_file(self, rel: str) -> Path:
        root = self.require_bound().resolve()
        clean_rel = normalise_rel(rel)
        target = (root / Path(*PurePosixPath(clean_rel).parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise UserFacingError("文件路径越界。") from exc
        if not target.exists() or not target.is_file():
            raise UserFacingError("目标 JSON 文件不存在。")
        return target

    def invalidate(self) -> None:
        with self.lock:
            self.index_valid = False

    def rebuild_index_locked(self) -> None:
        root = self.require_bound()
        start = time.perf_counter()
        records = []
        errors = []
        json_files = sorted(item for item in root.rglob("*.json") if item.is_file())
        overrides = load_json_sidecar(OVERRIDES_FILE, {})
        documents = []
        keyword_aliases: dict[str, set[str]] = {}
        for file_path in json_files:
            rel = posix_rel(file_path, root)
            try:
                data, encoding, _newline = read_json_document(file_path)
            except Exception as exc:
                errors.append({"file": rel, "error": str(exc)})
                continue

            category = infer_category(rel)
            documents.append((rel, data, encoding, category))
            for key, values in collect_keyword_aliases(data, category).items():
                keyword_aliases.setdefault(key, set()).update(values)

        for rel, data, encoding, category in documents:
            for json_path, value, ancestors in walk_strings(data):
                if is_internal_string_field(json_path, value):
                    continue
                pointer = path_to_pointer(json_path)
                key = f"{rel}|{pointer}"
                has_override = key in overrides
                if not has_override and is_default_placeholder_field(json_path, value):
                    continue
                leaf_key = json_path[-1] if json_path else ""
                entry_id = extract_entry_id(ancestors)
                sinner = infer_sinner(rel, entry_id)
                path_display = path_to_display(json_path)
                field = str(leaf_key)
                category_label = CATEGORY_LABELS.get(category, category)
                sinner_label = SINNER_BY_ID.get(sinner or "", {}).get("label", "")
                label = extract_label(ancestors, leaf_key, value)
                alias_terms = referenced_aliases(value, keyword_aliases)
                file_search, file_search_compact = make_search_blob([rel])
                field_search, field_search_compact = make_search_blob([field, path_display])
                full_search, full_search_compact = make_search_blob(
                    [value, label, rel, field, entry_id, path_display, category_label, sinner_label, *alias_terms]
                )
                direct_search, direct_search_compact = make_search_blob(
                    [value, label, rel, field, entry_id, path_display, category_label, sinner_label]
                )
                record = {
                    "key": key,
                    "file": rel,
                    "path": json_path,
                    "pointer": pointer,
                    "pathDisplay": path_display,
                    "field": field,
                    "value": value,
                    "preview": clean_preview(value),
                    "valueHash": sha1_text(value),
                    "category": category,
                    "categoryLabel": category_label,
                    "sinner": sinner,
                    "sinnerLabel": sinner_label,
                    "entryId": entry_id,
                    "label": label,
                    "encoding": encoding,
                    "hasOverride": has_override,
                    "_fileSearch": file_search,
                    "_fileSearchCompact": file_search_compact,
                    "_fieldSearch": field_search,
                    "_fieldSearchCompact": field_search_compact,
                    "_directSearch": direct_search,
                    "_directSearchCompact": direct_search_compact,
                    "_search": full_search,
                    "_searchCompact": full_search_compact,
                }
                records.append(record)

        keyword_targets: dict[str, list[dict]] = {}
        for record in records:
            if (
                record["category"] == "keyword"
                and normalise_field_name(record["field"]) in {"name", "title"}
                and isinstance(record.get("entryId"), str)
            ):
                keyword_targets.setdefault(record["entryId"], []).append(record)

        for record in records:
            references = []
            seen_tokens = set()
            for token in REFERENCE_TOKEN_RE.findall(record["value"]):
                if token in seen_tokens or token not in keyword_targets:
                    continue
                seen_tokens.add(token)
                aliases = sorted(keyword_aliases.get(token, ()))
                alias_search, alias_search_compact = make_search_blob(aliases)
                references.append(
                    {
                        "token": token,
                        "aliases": aliases,
                        "_search": alias_search,
                        "_searchCompact": alias_search_compact,
                        "targets": keyword_targets[token],
                    }
                )
            record["_aliasReferences"] = references

        category_counts = {}
        sinner_counts = {}
        for record in records:
            category_counts[record["category"]] = category_counts.get(record["category"], 0) + 1
            if record["sinner"]:
                sinner_counts[record["sinner"]] = sinner_counts.get(record["sinner"], 0) + 1

        self.index = records
        self.stats = {
            "files": len(json_files),
            "strings": len(records),
            "durationMs": round((time.perf_counter() - start) * 1000),
            "indexedAt": utc_now_iso(),
            "errors": errors[:30],
            "errorCount": len(errors),
            "categoryCounts": category_counts,
            "sinnerCounts": sinner_counts,
        }
        self.index_valid = True

    def ensure_index(self) -> None:
        with self.lock:
            if not self.index_valid:
                self.rebuild_index_locked()

    def status(self) -> dict:
        with self.lock:
            bound = self.bound_path is not None
            return {
                "bound": bound,
                "inputPath": self.input_path,
                "path": str(self.bound_path) if self.bound_path else "",
                "defaultPath": sample_path(),
                "stats": self.stats if self.index_valid else {},
                "categories": CATEGORY_DEFS,
                "sinners": SINNER_DEFS,
            }

    def search(self, payload: dict) -> dict:
        self.ensure_index()
        query = str(payload.get("query", "")).strip()
        category = payload.get("category", "all") or "all"
        sinner = payload.get("sinner", "all") or "all"
        file_query = str(payload.get("fileQuery", "")).strip()
        field_query = str(payload.get("fieldQuery", "")).strip()
        modified_only = bool(payload.get("modifiedOnly", False))
        limit = int(payload.get("limit", 250) or 250)
        limit = max(1, min(limit, 1000))
        query_prepared = prepare_search_query(query)
        file_prepared = prepare_search_query(file_query)
        field_prepared = prepare_search_query(field_query)

        matches = []
        seen_keys = set()
        total = 0

        def add_match(key: str, item: dict) -> None:
            nonlocal total
            if key in seen_keys:
                return
            seen_keys.add(key)
            total += 1
            if len(matches) < limit:
                matches.append(item)

        def add_record(record: dict) -> None:
            nonlocal total
            key = record["key"]
            if key in seen_keys:
                return
            seen_keys.add(key)
            total += 1
            if len(matches) < limit:
                matches.append(public_record(record))

        for record in self.index:
            if category != "all" and record["category"] != category:
                continue
            if sinner != "all" and record["sinner"] != sinner:
                continue
            if modified_only and not record.get("hasOverride"):
                continue
            if file_query and not search_matches_prepared(
                file_prepared, record["_fileSearch"], record["_fileSearchCompact"]
            ):
                continue
            if field_query and not search_matches_prepared(
                field_prepared, record["_fieldSearch"], record["_fieldSearchCompact"]
            ):
                continue
            if not query:
                add_record(record)
                continue

            if search_matches_prepared(query_prepared, record["_directSearch"], record["_directSearchCompact"]):
                add_record(record)
                continue

            if not search_matches_prepared(query_prepared, record["_search"], record["_searchCompact"]):
                continue

            # A localized keyword can be referenced by an internal token inside a
            # passive or skill. Surface the actual localized name as the editing
            # target, while keeping the source category/sinner filters meaningful.
            for reference in record.get("_aliasReferences", []):
                if not search_matches_prepared(
                    query_prepared, reference["_search"], reference["_searchCompact"]
                ):
                    continue
                targets_by_value: dict[str, list[dict]] = {}
                for target in reference["targets"]:
                    targets_by_value.setdefault(target["value"], []).append(target)
                for value, targets in targets_by_value.items():
                    first = targets[0]
                    linked_key = "linked|{}|{}|{}|{}".format(
                        record["category"], record.get("sinner") or "", reference["token"], sha1_text(value)
                    )
                    files = list(dict.fromkeys(target["file"] for target in targets))
                    linked = {
                        **public_record(first),
                        "key": linked_key,
                        "file": " + ".join(files),
                        "pathDisplay": f"同步到 {len(targets)} 个关联位置",
                        "value": value,
                        "preview": clean_preview(value),
                        "label": value,
                        "category": record["category"],
                        "categoryLabel": f"{record['categoryLabel']}关联词条",
                        "sinner": record.get("sinner"),
                        "sinnerLabel": record.get("sinnerLabel", ""),
                        "hasOverride": any(target.get("hasOverride") for target in targets),
                        "linkedReference": {
                            "token": reference["token"],
                            "expectedValue": value,
                            "copies": len(targets),
                        },
                    }
                    add_match(linked_key, linked)

        return {
            "results": matches,
            "total": total,
            "limited": total > limit,
            "stats": self.stats,
        }

    def record_in_scope(self, record: dict, scope: dict) -> bool:
        category = scope.get("category", "all") or "all"
        sinner = scope.get("sinner", "all") or "all"
        file_query = str(scope.get("fileQuery", "")).strip()
        field_query = str(scope.get("fieldQuery", "")).strip()
        modified_only = bool(scope.get("modifiedOnly", False))
        file_prepared = scope.get("_filePrepared") or prepare_search_query(file_query)
        field_prepared = scope.get("_fieldPrepared") or prepare_search_query(field_query)
        if category != "all" and record["category"] != category:
            return False
        if sinner != "all" and record["sinner"] != sinner:
            return False
        if modified_only and not record.get("hasOverride"):
            return False
        if file_query and not search_matches_prepared(file_prepared, record["_fileSearch"], record["_fileSearchCompact"]):
            return False
        if field_query and not search_matches_prepared(
            field_prepared, record["_fieldSearch"], record["_fieldSearchCompact"]
        ):
            return False
        return True

    def bulk_candidates(self, payload: dict) -> tuple[str, str, bool, list[dict]]:
        self.ensure_index()
        old_text = str(payload.get("oldText", ""))
        new_text = str(payload.get("newText", ""))
        match_case = bool(payload.get("matchCase", False))
        if old_text == "":
            raise UserFacingError("请输入要查找的原词。")
        if old_text == new_text:
            raise UserFacingError("原词和新译名相同，不需要批量替换。")
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        scope = {
            **scope,
            "_filePrepared": prepare_search_query(str(scope.get("fileQuery", "")).strip()),
            "_fieldPrepared": prepare_search_query(str(scope.get("fieldQuery", "")).strip()),
        }
        items = []
        for record in self.index:
            if not self.record_in_scope(record, scope):
                continue
            count = term_occurrences(record["value"], old_text, match_case)
            if count <= 0:
                continue
            items.append({**record, "occurrences": count})
        return old_text, new_text, match_case, items

    def bulk_preview(self, payload: dict) -> dict:
        old_text, new_text, match_case, items = self.bulk_candidates(payload)
        file_counts: dict[str, int] = {}
        occurrence_count = 0
        examples = []
        safety_examples = []
        unsafe_fields = 0
        for item in items:
            file_counts[item["file"]] = file_counts.get(item["file"], 0) + 1
            occurrence_count += item["occurrences"]
            next_value = replace_term(item["value"], old_text, new_text, match_case)
            warning = safety_warning_entry(item["file"], item["field"], item["pathDisplay"], item["value"], next_value)
            if warning:
                unsafe_fields += 1
                if len(safety_examples) < 8:
                    safety_examples.append(warning)
            if len(examples) < 10:
                examples.append(
                    {
                        "file": item["file"],
                        "field": item["field"],
                        "pathDisplay": item["pathDisplay"],
                        "categoryLabel": item["categoryLabel"],
                        "sinnerLabel": item["sinnerLabel"],
                        "before": clean_preview(item["value"], 140),
                        "after": clean_preview(next_value, 140),
                        "occurrences": item["occurrences"],
                    }
                )
        top_files = sorted(file_counts.items(), key=lambda pair: pair[1], reverse=True)[:12]
        return {
            "fields": len(items),
            "occurrences": occurrence_count,
            "files": len(file_counts),
            "topFiles": [{"file": file, "count": count} for file, count in top_files],
            "examples": examples,
            "unsafeFields": unsafe_fields,
            "safetyWarnings": safety_examples,
        }

    def bulk_replace(self, payload: dict) -> dict:
        old_text, new_text, match_case, items = self.bulk_candidates(payload)
        if not items:
            return {"changed": 0, "occurrences": 0, "files": 0, "missing": 0}
        force_safety = bool(payload.get("forceSafety", False))
        if not force_safety:
            safety_examples = []
            unsafe_fields = 0
            for item in items:
                next_value = replace_term(item["value"], old_text, new_text, match_case)
                warning = safety_warning_entry(
                    item["file"], item["field"], item["pathDisplay"], item["value"], next_value
                )
                if not warning:
                    continue
                unsafe_fields += 1
                if len(safety_examples) < 8:
                    safety_examples.append(warning)
            if unsafe_fields:
                return {
                    "changed": 0,
                    "occurrences": 0,
                    "files": 0,
                    "missing": 0,
                    "blockedBySafety": True,
                    "unsafeFields": unsafe_fields,
                    "warnings": safety_examples,
                }

        with self.lock:
            root = self.require_bound()
            stamp = local_stamp()
            operation_id = new_operation_id("bulk")
            overrides = load_json_sidecar(OVERRIDES_FILE, {})
            by_file: dict[str, list[dict]] = {}
            for item in items:
                by_file.setdefault(item["file"], []).append(item)

            changed = 0
            occurrence_count = 0
            missing = 0
            changed_files = 0
            for rel, file_items in by_file.items():
                try:
                    file_path = self.resolve_file(rel)
                    data, encoding, newline = read_json_document(file_path)
                except Exception:
                    missing += len(file_items)
                    continue

                touched = False
                file_backup = ""
                for item in file_items:
                    json_path = item["path"]
                    try:
                        current = get_by_path(data, json_path)
                    except Exception:
                        missing += 1
                        continue
                    if not isinstance(current, str):
                        missing += 1
                        continue
                    count = term_occurrences(current, old_text, match_case)
                    if count <= 0:
                        continue

                    next_value = replace_term(current, old_text, new_text, match_case)
                    if current == next_value:
                        continue
                    if not file_backup:
                        file_backup = backup_file(file_path, root, stamp)
                    set_by_path(data, json_path, next_value)
                    key = make_key(rel, json_path)
                    previous = overrides.get(key, {})
                    overrides[key] = {
                        **previous,
                        "key": key,
                        "file": rel,
                        "path": json_path,
                        "pointer": path_to_pointer(json_path),
                        "field": item["field"],
                        "category": item["category"],
                        "sinner": item.get("sinner"),
                        "entryId": item.get("entryId"),
                        "label": item.get("label", previous.get("label", "")),
                        "firstOriginal": previous.get("firstOriginal", current),
                        "lastSource": current,
                        "value": next_value,
                        "note": f"批量替换：{old_text} -> {new_text}",
                        "updatedAt": utc_now_iso(),
                        "updateCount": int(previous.get("updateCount", 0)) + 1,
                    }
                    append_history(
                        {
                            "action": "bulk_replace",
                            "operationId": operation_id,
                            "file": rel,
                            "path": json_path,
                            "pointer": path_to_pointer(json_path),
                            "field": item["field"],
                            "oldValue": current,
                            "newValue": next_value,
                            "note": f"批量替换 {count} 处：{old_text} -> {new_text}",
                            "backupPath": file_backup,
                        }
                    )
                    touched = True
                    changed += 1
                    occurrence_count += count

                if touched:
                    write_json_document(file_path, data, encoding, newline)
                    changed_files += 1

            if changed:
                save_json_sidecar(OVERRIDES_FILE, overrides)
                self.index_valid = False

        return {
            "changed": changed,
            "occurrences": occurrence_count,
            "files": changed_files,
            "missing": missing,
        }

    def undo_last_operation(self) -> dict:
        undoable_actions = {"update", "bulk_replace", "reapply"}
        with self.lock:
            entries = read_history_entries()
            undone_keys = {
                str(entry.get("undoneOperationId"))
                for _index, entry in entries
                if entry.get("action") == "undo" and entry.get("undoneOperationId")
            }

            candidate_key = None
            candidate_index = None
            candidate_entry = None
            for index, entry in reversed(entries):
                if entry.get("action") not in undoable_actions:
                    continue
                key = str(entry.get("operationId") or f"legacy:{index}")
                if key in undone_keys:
                    continue
                candidate_key = key
                candidate_index = index
                candidate_entry = entry
                break

            if candidate_key is None:
                raise UserFacingError("没有可撤销的写入操作。")

            if candidate_key.startswith("legacy:"):
                group = [(candidate_index, candidate_entry)]
            else:
                group = [
                    (index, entry)
                    for index, entry in entries
                    if str(entry.get("operationId", "")) == candidate_key and entry.get("action") in undoable_actions
                ]

            root = self.require_bound()
            stamp = local_stamp()
            overrides = load_json_sidecar(OVERRIDES_FILE, {})
            by_file: dict[str, list[tuple[int, dict]]] = {}
            for index, entry in group:
                if not entry.get("file") or not isinstance(entry.get("path"), list):
                    continue
                if "oldValue" not in entry or "newValue" not in entry:
                    continue
                by_file.setdefault(normalise_rel(str(entry["file"])), []).append((index, entry))

            changed = 0
            skipped = 0
            conflicts = 0
            missing = 0
            changed_files = 0
            for rel, file_entries in by_file.items():
                try:
                    file_path = self.resolve_file(rel)
                    data, encoding, newline = read_json_document(file_path)
                except Exception:
                    missing += len(file_entries)
                    continue

                touched = False
                file_backup = ""
                for _index, entry in reversed(file_entries):
                    json_path = entry["path"]
                    old_value = str(entry.get("oldValue", ""))
                    new_value = str(entry.get("newValue", ""))
                    try:
                        current = get_by_path(data, json_path)
                    except Exception:
                        missing += 1
                        continue
                    if not isinstance(current, str):
                        missing += 1
                        continue
                    if current == old_value:
                        skipped += 1
                        continue
                    if current != new_value:
                        conflicts += 1
                        continue
                    if not file_backup:
                        file_backup = backup_file(file_path, root, stamp)
                    set_by_path(data, json_path, old_value)

                    key = make_key(rel, json_path)
                    previous = overrides.get(key, {})
                    if previous and previous.get("firstOriginal") == old_value:
                        overrides.pop(key, None)
                    else:
                        overrides[key] = {
                            **previous,
                            "key": key,
                            "file": rel,
                            "path": json_path,
                            "pointer": path_to_pointer(json_path),
                            "field": entry.get("field", str(json_path[-1]) if json_path else ""),
                            "category": previous.get("category", infer_category(rel)),
                            "sinner": previous.get("sinner"),
                            "entryId": previous.get("entryId"),
                            "label": previous.get("label", ""),
                            "firstOriginal": previous.get("firstOriginal", old_value),
                            "lastSource": current,
                            "value": old_value,
                            "note": f"撤销操作：{candidate_key}",
                            "updatedAt": utc_now_iso(),
                            "updateCount": int(previous.get("updateCount", 0)) + 1,
                        }
                    touched = True
                    changed += 1

                if touched:
                    write_json_document(file_path, data, encoding, newline)
                    changed_files += 1

            if changed:
                save_json_sidecar(OVERRIDES_FILE, overrides)
                self.index_valid = False

            append_history(
                {
                    "action": "undo",
                    "operationId": new_operation_id("undo"),
                    "undoneOperationId": candidate_key,
                    "undoneAction": group[-1][1].get("action") if group else "",
                    "file": "",
                    "path": [],
                    "oldValue": "",
                    "newValue": "",
                    "note": f"撤销最近一次操作：回退 {changed} 个字段，冲突 {conflicts} 个",
                    "changed": changed,
                    "skipped": skipped,
                    "conflicts": conflicts,
                    "missing": missing,
                }
            )

        return {
            "changed": changed,
            "skipped": skipped,
            "conflicts": conflicts,
            "missing": missing,
            "files": changed_files,
            "operationId": candidate_key,
        }

    def update_value(self, payload: dict) -> dict:
        rel = normalise_rel(str(payload.get("file", "")))
        json_path = payload.get("path")
        if not isinstance(json_path, list):
            raise UserFacingError("缺少 JSON 路径。")
        new_value = str(payload.get("newValue", ""))
        note = str(payload.get("note", "")).strip()
        force_safety = bool(payload.get("forceSafety", False))

        with self.lock:
            root = self.require_bound()
            operation_id = new_operation_id("update")
            file_path = self.resolve_file(rel)
            data, encoding, newline = read_json_document(file_path)
            old_value = get_by_path(data, json_path)
            if not isinstance(old_value, str):
                raise UserFacingError("目标字段不是文本。")
            if old_value == new_value:
                return {"changed": False, "message": "文本没有变化。"}
            if not force_safety:
                warning = safety_warning_entry(
                    rel,
                    str(json_path[-1]) if json_path else "",
                    path_to_display(json_path),
                    old_value,
                    new_value,
                )
                if warning:
                    return {"changed": False, "blockedBySafety": True, "warnings": [warning]}

            backup_path = backup_file(file_path, root)
            set_by_path(data, json_path, new_value)
            write_json_document(file_path, data, encoding, newline)

            overrides = load_json_sidecar(OVERRIDES_FILE, {})
            key = make_key(rel, json_path)
            previous = overrides.get(key, {})
            entry_id = payload.get("entryId", previous.get("entryId"))
            override = {
                **previous,
                "key": key,
                "file": rel,
                "path": json_path,
                "pointer": path_to_pointer(json_path),
                "field": str(json_path[-1]) if json_path else "",
                "category": infer_category(rel),
                "sinner": infer_sinner(rel, entry_id),
                "entryId": entry_id,
                "label": payload.get("label", previous.get("label", "")),
                "firstOriginal": previous.get("firstOriginal", old_value),
                "lastSource": old_value,
                "value": new_value,
                "note": note,
                "updatedAt": utc_now_iso(),
                "updateCount": int(previous.get("updateCount", 0)) + 1,
            }
            overrides[key] = override
            save_json_sidecar(OVERRIDES_FILE, overrides)

            append_history(
                {
                    "action": "update",
                    "operationId": operation_id,
                    "file": rel,
                    "path": json_path,
                    "pointer": override["pointer"],
                    "field": override["field"],
                    "oldValue": old_value,
                    "newValue": new_value,
                    "note": note,
                    "backupPath": backup_path,
                }
            )
            self.index_valid = False

        return {"changed": True, "backupPath": backup_path}

    def update_reference(self, payload: dict) -> dict:
        token = str(payload.get("token", "")).strip()
        expected_value = str(payload.get("expectedValue", ""))
        new_value = str(payload.get("newValue", ""))
        note = str(payload.get("note", "")).strip()
        force_safety = bool(payload.get("forceSafety", False))
        if not token:
            raise UserFacingError("缺少关联词条 ID。")
        if expected_value == new_value:
            return {"changed": False, "message": "文本没有变化。"}

        with self.lock:
            self.ensure_index()
            targets = [
                record
                for record in self.index
                if record["category"] == "keyword"
                and record.get("entryId") == token
                and normalise_field_name(record["field"]) in {"name", "title"}
                and record["value"] == expected_value
            ]
            if not targets:
                raise UserFacingError("关联词条已变化，请重新检索后再修改。")

            if not force_safety:
                warnings = [
                    warning
                    for target in targets
                    if (
                        warning := safety_warning_entry(
                            target["file"],
                            target["field"],
                            target["pathDisplay"],
                            target["value"],
                            new_value,
                        )
                    )
                ]
                if warnings:
                    return {
                        "changed": False,
                        "blockedBySafety": True,
                        "unsafeFields": len(warnings),
                        "warnings": warnings[:8],
                    }

            root = self.require_bound()
            stamp = local_stamp()
            operation_id = new_operation_id("update_reference")
            overrides = load_json_sidecar(OVERRIDES_FILE, {})
            by_file: dict[str, list[dict]] = {}
            for target in targets:
                by_file.setdefault(target["file"], []).append(target)

            changed = 0
            conflicts = 0
            changed_files = 0
            for rel, file_targets in by_file.items():
                file_path = self.resolve_file(rel)
                data, encoding, newline = read_json_document(file_path)
                touched = False
                file_backup = ""
                for target in file_targets:
                    json_path = target["path"]
                    try:
                        current = get_by_path(data, json_path)
                    except Exception:
                        conflicts += 1
                        continue
                    if current == new_value:
                        continue
                    if current != expected_value:
                        conflicts += 1
                        continue
                    if not file_backup:
                        file_backup = backup_file(file_path, root, stamp)
                    set_by_path(data, json_path, new_value)
                    key = make_key(rel, json_path)
                    previous = overrides.get(key, {})
                    override = {
                        **previous,
                        "key": key,
                        "file": rel,
                        "path": json_path,
                        "pointer": path_to_pointer(json_path),
                        "field": target["field"],
                        "category": target["category"],
                        "sinner": target.get("sinner"),
                        "entryId": token,
                        "label": new_value,
                        "firstOriginal": previous.get("firstOriginal", current),
                        "lastSource": current,
                        "value": new_value,
                        "note": note or f"同步修改关联词条：{token}",
                        "updatedAt": utc_now_iso(),
                        "updateCount": int(previous.get("updateCount", 0)) + 1,
                    }
                    overrides[key] = override
                    append_history(
                        {
                            "action": "update",
                            "operationId": operation_id,
                            "file": rel,
                            "path": json_path,
                            "pointer": override["pointer"],
                            "field": target["field"],
                            "oldValue": current,
                            "newValue": new_value,
                            "note": note or f"同步修改关联词条：{token}",
                            "backupPath": file_backup,
                        }
                    )
                    touched = True
                    changed += 1
                if touched:
                    write_json_document(file_path, data, encoding, newline)
                    changed_files += 1

            if changed:
                save_json_sidecar(OVERRIDES_FILE, overrides)
                self.index_valid = False

        return {
            "changed": changed > 0,
            "fields": changed,
            "files": changed_files,
            "conflicts": conflicts,
            "message": "关联词条没有可写入的变化。" if not changed else "",
        }

    def reapply_overrides(self, payload: dict) -> dict:
        dry_run = bool(payload.get("dryRun", False))
        overrides = load_json_sidecar(OVERRIDES_FILE, {})
        if not overrides:
            return {"changed": 0, "skipped": 0, "missing": 0, "items": []}

        with self.lock:
            root = self.require_bound()
            stamp = local_stamp()
            operation_id = new_operation_id("reapply")
            changed = 0
            skipped = 0
            conflicts = 0
            missing = 0
            items = []
            by_file: dict[str, list[dict]] = {}
            for override in overrides.values():
                rel = normalise_rel(override.get("file", ""))
                by_file.setdefault(rel, []).append(override)

            for rel, file_overrides in by_file.items():
                try:
                    file_path = self.resolve_file(rel)
                    data, encoding, newline = read_json_document(file_path)
                except Exception as exc:
                    missing += len(file_overrides)
                    items.append({"file": rel, "status": "missing", "message": str(exc)})
                    continue

                touched = False
                file_backup = ""
                for override in file_overrides:
                    json_path = override.get("path")
                    desired = str(override.get("value", ""))
                    expected_sources = []
                    for key in ("lastSource", "firstOriginal"):
                        value = override.get(key)
                        if isinstance(value, str) and value not in expected_sources:
                            expected_sources.append(value)
                    try:
                        current = get_by_path(data, json_path)
                    except Exception as exc:
                        missing += 1
                        items.append({"file": rel, "path": json_path, "status": "missing", "message": str(exc)})
                        continue
                    if current == desired:
                        skipped += 1
                        continue
                    if not isinstance(current, str):
                        missing += 1
                        items.append({"file": rel, "path": json_path, "status": "not_text"})
                        continue
                    if expected_sources and current not in expected_sources:
                        conflicts += 1
                        items.append(
                            {
                                "file": rel,
                                "path": json_path,
                                "field": str(json_path[-1]) if json_path else "",
                                "status": "conflict",
                                "current": clean_preview(current, 140),
                                "expected": clean_preview(expected_sources[0], 140),
                                "desired": clean_preview(desired, 140),
                                "message": "当前源文本已变化，已跳过，避免把旧修改写入新版文本。",
                            }
                        )
                        continue

                    changed += 1
                    touched = True
                    if not dry_run:
                        if not file_backup:
                            file_backup = backup_file(file_path, root, stamp)
                        set_by_path(data, json_path, desired)
                        append_history(
                            {
                                "action": "reapply",
                                "operationId": operation_id,
                                "file": rel,
                                "path": json_path,
                                "pointer": path_to_pointer(json_path),
                                "field": str(json_path[-1]) if json_path else "",
                                "oldValue": current,
                                "newValue": desired,
                                "note": "重放历史修改",
                                "backupPath": file_backup,
                            }
                        )
                    items.append({"file": rel, "path": json_path, "status": "changed"})

                if touched and not dry_run:
                    write_json_document(file_path, data, encoding, newline)

            if changed and not dry_run:
                self.index_valid = False

        return {"changed": changed, "skipped": skipped, "missing": missing, "conflicts": conflicts, "items": items[:200]}

    def backup_all(self) -> dict:
        with self.lock:
            root = self.require_bound()
            stamp = local_stamp()
            archive = BACKUP_DIR / f"full_{stamp}.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in sorted(root.rglob("*.json")):
                    if file_path.is_file():
                        zf.write(file_path, posix_rel(file_path, root))
                        count += 1
            append_history(
                {
                    "action": "backup",
                    "file": "",
                    "path": [],
                    "oldValue": "",
                    "newValue": "",
                    "note": f"完整备份 {count} 个 JSON",
                    "backupPath": str(archive),
                }
            )
        return {"archive": str(archive), "files": count}


STATE = AppState()


class Handler(SimpleHTTPRequestHandler):
    server_version = "LimbusPatchTool/1.0"

    def log_message(self, format, *args):
        app_log(format % args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_error_json(self, exc: Exception):
        if isinstance(exc, UserFacingError):
            self.send_json({"error": str(exc)}, 400)
            return
        app_log(traceback.format_exc())
        self.send_json({"error": f"工具内部错误：{exc}"}, 500)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api_get(parsed)
            else:
                self.serve_static(parsed.path)
        except Exception as exc:
            self.send_error_json(exc)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                raise UserFacingError("未知接口。")
            payload = self.read_json()
            self.handle_api_post(parsed.path, payload)
        except Exception as exc:
            self.send_error_json(exc)

    def serve_static(self, path: str):
        if path in ("", "/", "/index.html"):
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            file_path = (RESOURCE_DIR / unquote(path).lstrip("/")).resolve()
            try:
                file_path.relative_to(STATIC_DIR.resolve())
            except ValueError as exc:
                raise UserFacingError("静态文件路径越界。") from exc
        else:
            self.send_response(404)
            self.end_headers()
            return

        if not file_path.exists() or not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_get(self, parsed):
        if parsed.path == "/api/status":
            self.send_json(STATE.status())
            return
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or ["80"])[0])
            self.send_json({"history": read_history(limit)})
            return
        if parsed.path == "/api/export-overrides":
            overrides = load_json_sidecar(OVERRIDES_FILE, {})
            payload = json.dumps(
                {"exportedAt": utc_now_iso(), "boundPath": STATE.status().get("path"), "overrides": overrides},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="limbus_translation_overrides.json"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        raise UserFacingError("未知接口。")

    def handle_api_post(self, path: str, payload: dict):
        if path == "/api/bind":
            self.send_json(STATE.bind(str(payload.get("path", ""))))
            return
        if path == "/api/search":
            self.send_json(STATE.search(payload))
            return
        if path == "/api/bulk-preview":
            self.send_json(STATE.bulk_preview(payload))
            return
        if path == "/api/bulk-replace":
            self.send_json(STATE.bulk_replace(payload))
            return
        if path == "/api/update":
            self.send_json(STATE.update_value(payload))
            return
        if path == "/api/update-reference":
            self.send_json(STATE.update_reference(payload))
            return
        if path == "/api/reapply":
            self.send_json(STATE.reapply_overrides(payload))
            return
        if path == "/api/undo-last":
            self.send_json(STATE.undo_last_operation())
            return
        if path == "/api/backup":
            self.send_json(STATE.backup_all())
            return
        if path == "/api/reindex":
            STATE.invalidate()
            STATE.ensure_index()
            self.send_json(STATE.status())
            return
        if path == "/api/pick-folder":
            self.send_json({"path": pick_folder_dialog()})
            return
        if path == "/api/shutdown":
            self.send_json({"ok": True})
            threading.Thread(target=schedule_shutdown, args=(self.server,), daemon=True).start()
            return
        raise UserFacingError("未知接口。")


def read_history(limit: int) -> list[dict]:
    limit = max(1, min(limit, 500))
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(items))


def make_server(host: str, port: int):
    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError:
        if port == 0:
            raise
        return ThreadingHTTPServer((host, 0), Handler)


def schedule_shutdown(server) -> None:
    def hard_exit():
        time.sleep(0.75)
        os._exit(0)

    threading.Thread(target=hard_exit, daemon=True).start()
    time.sleep(0.15)
    try:
        server.shutdown()
    except Exception:
        pass
    os._exit(0)


def run_browser_mode(server, url: str, open_browser: bool) -> None:
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app_log("shutdown requested")
    finally:
        server.server_close()


def run_native_window(server, url: str) -> None:
    global EXIT_PROCESS_ON_SHUTDOWN
    try:
        import webview
    except Exception as exc:
        app_log(f"native window unavailable, falling back to browser: {exc}")
        run_browser_mode(server, url, True)
        return

    EXIT_PROCESS_ON_SHUTDOWN = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        webview.create_window(
            "Limbus Translation Tool",
            url,
            width=1320,
            height=840,
            min_size=(980, 640),
        )
        webview.start()
    except Exception as exc:
        app_log(f"native window failed, falling back to browser: {exc}")
        webbrowser.open(url)
        try:
            while server_thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            app_log("shutdown requested")
    finally:
        EXIT_PROCESS_ON_SHUTDOWN = False
        try:
            server.shutdown()
        except Exception:
            pass
        server.server_close()
        server_thread.join(timeout=2)


def app_main() -> int:
    parser = argparse.ArgumentParser(description="Limbus translation JSON editor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()

    server = make_server(args.host, args.port)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    app_log(f"server started: {url}")
    if args.no_browser:
        run_browser_mode(server, url, False)
    elif args.browser:
        run_browser_mode(server, url, True)
    else:
        run_native_window(server, url)
    return 0


if __name__ == "__main__":
    raise SystemExit(app_main())
